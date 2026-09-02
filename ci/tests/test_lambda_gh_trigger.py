import importlib
import json


def _reload_lambda(monkeypatch, allowed_push_branches=None, allowed_users=None):
    if allowed_push_branches is None:
        monkeypatch.delenv("ALLOWED_PUSH_BRANCHES", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_PUSH_BRANCHES", allowed_push_branches)
    if allowed_users is None:
        monkeypatch.delenv("ALLOWED_USERS_JSON", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_USERS_JSON", json.dumps(allowed_users))
    mod = importlib.import_module("praktika.infrastructure.native.lambda_gh_trigger")
    return importlib.reload(mod)


def _push_payload(ref):
    return {
        "ref": ref,
        "after": "a" * 40,
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "octocat"},
    }


def _pr_payload(external=True, action="opened", head_sha=None):
    repo = "owner/repo"
    head_repo = "fork/repo" if external else repo
    return {
        "action": action,
        "repository": {"full_name": repo},
        "sender": {"login": "contributor"},
        "pull_request": {
            "number": 17,
            "title": "Test PR",
            "draft": False,
            "labels": [{"name": "ci"}],
            "head": {
                "sha": head_sha or ("b" * 40),
                "ref": "feature",
                "repo": {
                    "full_name": head_repo,
                    "fork": external,
                },
            },
            "base": {
                "ref": "main",
                "repo": {"full_name": repo},
            },
        },
    }


def test_cancel_before_key_uses_default_scope(monkeypatch):
    monkeypatch.setenv("SQS_QUEUE_NAME", "praktika-workflow-orchestrator")
    mod = importlib.import_module("praktika.infrastructure.native.lambda_gh_trigger")
    mod = importlib.reload(mod)
    assert mod._cancel_before_key(124) == "pr/124/cancel-before-default"


def test_cancel_before_key_uses_base_scope(monkeypatch):
    monkeypatch.setenv("SQS_QUEUE_NAME", "praktika-workflow-orchestrator-base")
    mod = importlib.import_module("praktika.infrastructure.native.lambda_gh_trigger")
    mod = importlib.reload(mod)
    assert mod._cancel_before_key(124) == "pr/124/cancel-before-base"


def test_push_branches_default_to_main(monkeypatch):
    mod = _reload_lambda(monkeypatch)

    assert mod._build_push_workflow(_push_payload("refs/heads/main"), 123.0)[
        "head_ref"
    ] == "main"
    assert mod._build_push_workflow(_push_payload("refs/heads/feature"), 123.0) is None


def test_push_branches_can_be_redefined_from_env(monkeypatch):
    mod = _reload_lambda(monkeypatch, "release/1.0,develop")

    assert mod._build_push_workflow(_push_payload("refs/heads/release/1.0"), 123.0)[
        "head_ref"
    ] == "release/1.0"
    assert mod._build_push_workflow(_push_payload("refs/heads/develop"), 123.0)[
        "head_ref"
    ] == "develop"
    assert mod._build_push_workflow(_push_payload("refs/heads/main"), 123.0) is None


def test_build_workflow_marks_external_pr(monkeypatch):
    mod = _reload_lambda(monkeypatch)

    workflow = mod._build_workflow("opened", _pr_payload(external=True), 123.0)

    assert workflow["external_pr"] is True
    assert workflow["head_repo"] == "fork/repo"
    # Metadata the DAG is filtered on must survive the shared builder.
    assert workflow["labels"] == ["ci"]
    assert workflow["title"] == "Test PR"
    assert workflow["head_ref"] == "feature"


def _rerun_meta(external, labels=None):
    return {
        "head_ref": "feature",
        "base_ref": "main",
        "title": "Test PR",
        "draft": False,
        "labels": labels if labels is not None else ["ci"],
        "external_pr": external,
        "head_repo": "fork/repo" if external else "owner/repo",
    }


def test_pull_request_sender_can_be_restricted_by_allowed_users(monkeypatch):
    mod = _reload_lambda(monkeypatch, allowed_users=["trusted"])
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(
        mod,
        "_enqueue",
        lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)),
    )

    mod.lambda_handler(
        {
            "headers": {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d-allowed-user",
            },
            "body": json.dumps(_pr_payload(external=False)),
        },
        None,
    )

    assert enqueued == []


def test_pull_request_sender_allowed_by_allowed_users_is_enqueued(monkeypatch):
    mod = _reload_lambda(monkeypatch, allowed_users=["contributor"])
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(
        mod,
        "_enqueue",
        lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)),
    )

    mod.lambda_handler(
        {
            "headers": {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d-allowed-user",
            },
            "body": json.dumps(_pr_payload(external=False)),
        },
        None,
    )

    assert len(enqueued) == 1
    assert enqueued[0][0]["sender"] == "contributor"


def test_pull_request_allowed_users_match_is_case_insensitive(monkeypatch):
    # GitHub logins are case-insensitive: a differently-cased allow-list entry
    # ("Contributor") must still match sender "contributor".
    mod = _reload_lambda(monkeypatch, allowed_users=["Contributor"])
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(
        mod,
        "_enqueue",
        lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)),
    )

    mod.lambda_handler(
        {
            "headers": {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d-allowed-user-case",
            },
            "body": json.dumps(_pr_payload(external=False)),
        },
        None,
    )

    assert len(enqueued) == 1
    assert enqueued[0][0]["sender"] == "contributor"


def test_pull_request_stale_event_is_dropped(monkeypatch):
    # A delayed/redelivered event whose head no longer matches the live PR must
    # not enqueue or write a cancel-before marker that would kill the newer run.
    mod = _reload_lambda(monkeypatch)
    enqueued = []
    cancels = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    # Payload head is A ("b"*40); the live PR has advanced to B ("e"*40).
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("e" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(mod, "_cancel_runs_before", lambda *a, **k: cancels.append((a, k)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(
        {
            "headers": {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-stale"},
            "body": json.dumps(_pr_payload(external=False, action="synchronize")),
        },
        None,
    )

    assert enqueued == []
    assert cancels == []


def test_pull_request_uses_fetched_metadata(monkeypatch):
    # The enqueued workflow carries the LIVE labels (from the refetch), not the
    # possibly-stale labels embedded in the webhook payload.
    mod = _reload_lambda(monkeypatch)
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(
        mod,
        "_fetch_pr",
        lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False, labels=["live-label"])),
    )
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(
        {
            "headers": {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-meta"},
            "body": json.dumps(_pr_payload(external=False)),  # payload labels=["ci"]
        },
        None,
    )

    assert len(enqueued) == 1
    assert enqueued[0][0]["labels"] == ["live-label"]


def test_pull_request_falls_back_to_payload_when_fetch_fails(monkeypatch):
    # A refetch error must not drop a legitimate run: fall back to the payload.
    mod = _reload_lambda(monkeypatch)
    enqueued = []

    def _boom(repo, pr_number):
        raise RuntimeError("GitHub API down")

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", _boom)
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(
        {
            "headers": {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-fallback"},
            "body": json.dumps(_pr_payload(external=False)),
        },
        None,
    )

    assert len(enqueued) == 1
    assert enqueued[0][0]["labels"] == ["ci"]  # payload metadata preserved


def test_cancel_runs_before_stores_head_sha(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    captured = {}

    class _FakeS3:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(mod, "S3_BUCKET", "bucket")
    monkeypatch.setattr(mod, "_s3", lambda: _FakeS3())

    mod._cancel_runs_before(17, 123.5, "a" * 40)

    assert captured["Bucket"] == "bucket"
    assert captured["Key"] == "pr/17/cancel-before-default"
    assert json.loads(captured["Body"].decode()) == {
        "ts": 123.5,
        "head_sha": "a" * 40,
    }


def test_external_pr_creates_gate_check_instead_of_enqueuing(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    payload = _pr_payload(external=True)
    gate_calls = []
    stored = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=True)))
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: None)
    monkeypatch.setattr(mod, "_create_gate_check", lambda *args, **kwargs: gate_calls.append((args, kwargs)) or {"id": 101})
    monkeypatch.setattr(mod, "_store_gate_state", lambda workflow, check_id, status, approved_by="": stored.append((workflow, check_id, status, approved_by)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    response = mod.lambda_handler(
        {
            "headers": {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d1",
            },
            "body": json.dumps(payload),
        },
        None,
    )

    assert response["statusCode"] == 200
    assert len(gate_calls) == 1
    assert gate_calls[0][1]["status"] == "in_progress"
    assert stored[0][2] == "awaiting"
    assert enqueued == []


def test_external_pr_autoapproves_after_safe_path_change(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    payload = _pr_payload(external=True, action="synchronize", head_sha="c" * 40)
    previous_state = {
        "repo": "owner/repo",
        "pr_number": 17,
        "head_sha": "b" * 40,
        "approval_check_id": 7,
        "status": "approved",
        "approved_by": "maintainer",
        "workflow": _pr_payload(external=True)["pull_request"],
    }
    gate_calls = []
    stored = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("c" * 40, _rerun_meta(external=True)))
    monkeypatch.setattr(
        mod, "_cancel_runs_before", lambda pr_number, event_ts, head_sha="": None
    )
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: previous_state)
    monkeypatch.setattr(mod, "_supersede_previous_gate", lambda state, token: None)
    monkeypatch.setattr(mod, "_changes_are_autoapprovable", lambda repo, base_sha, head_sha, token: True)
    monkeypatch.setattr(mod, "_create_gate_check", lambda *args, **kwargs: gate_calls.append((args, kwargs)) or {"id": 102})
    monkeypatch.setattr(mod, "_store_gate_state", lambda workflow, check_id, status, approved_by="": stored.append((workflow, check_id, status, approved_by)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(
        {
            "headers": {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d2",
            },
            "body": json.dumps(payload),
        },
        None,
    )

    assert gate_calls[0][1]["status"] == "completed"
    assert gate_calls[0][1]["conclusion"] == "success"
    assert stored[0][2] == "approved"
    assert stored[0][3] == "maintainer"
    assert len(enqueued) == 1


def test_gate_approve_action_enqueues_saved_workflow(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    workflow = mod._build_workflow("opened", _pr_payload(external=True), 123.0)
    state = {
        "repo": workflow["repo"],
        "pr_number": workflow["pr_number"],
        "head_sha": workflow["head_sha"],
        "approval_check_id": 55,
        "status": "awaiting",
        "workflow": workflow,
    }
    updates = []
    stored = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_can_maintain_repo", lambda repo, login, token: True)
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: state)
    monkeypatch.setattr(mod, "_update_gate_check", lambda *args, **kwargs: updates.append((args, kwargs)))
    monkeypatch.setattr(mod, "_store_gate_state", lambda workflow, check_id, status, approved_by="": stored.append((workflow, check_id, status, approved_by)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(
        {
            "headers": {
                "X-GitHub-Event": "check_run",
                "X-GitHub-Delivery": "d3",
            },
            "body": json.dumps(
                {
                    "action": "requested_action",
                    "sender": {"login": "maintainer"},
                    "requested_action": {"identifier": "approve"},
                    "check_run": {
                        "id": 55,
                        "external_id": mod._approval_external_id(
                            workflow["repo"], workflow["pr_number"], workflow["head_sha"]
                        ),
                    },
                }
            ),
        },
        None,
    )

    assert updates[0][1]["conclusion"] == "success"
    assert stored[0][2] == "approved"
    assert stored[0][3] == "maintainer"
    assert enqueued == [(workflow, "d3")]


def _rerun_event(delivery, sender="maintainer", head_sha="b" * 40):
    return {
        "headers": {"X-GitHub-Event": "check_run", "X-GitHub-Delivery": delivery},
        "body": json.dumps(
            {
                "action": "rerequested",
                "sender": {"login": sender},
                "check_run": {
                    "head_sha": head_sha,
                    "pull_requests": [{"number": 17}],
                },
                "repository": {"full_name": "owner/repo"},
            }
        ),
    }


def test_external_rerun_requires_maintainer(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    state = {"approval_check_id": 77, "head_sha": "b" * 40}
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=True)))
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: state)
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_can_maintain_repo", lambda repo, login, token: False)
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(_rerun_event("d4", sender="contributor"), None)

    assert enqueued == []


def test_internal_rerun_fetches_metadata_and_enqueues(monkeypatch):
    # An internal (same-repo) rerun enqueues directly, and the fetched PR
    # metadata (labels/title/draft) rides along so the DAG matches the live PR.
    mod = _reload_lambda(monkeypatch)
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(
        mod,
        "_fetch_pr",
        lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False, labels=["ci", "release"])),
    )
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: None)
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(_rerun_event("d-internal", sender="contributor"), None)

    assert len(enqueued) == 1
    workflow = enqueued[0][0]
    assert workflow["external_pr"] is False
    assert workflow["action"] == "rerequested"
    assert workflow["head_sha"] == "b" * 40
    assert workflow["labels"] == ["ci", "release"]
    assert workflow["title"] == "Test PR"


def test_rerun_fails_closed_when_pr_fetch_fails(monkeypatch):
    # If the PR refetch fails we cannot tell internal from external, so the
    # rerun must be dropped rather than run down the ungated internal path.
    mod = _reload_lambda(monkeypatch)
    enqueued = []

    def _boom(repo, pr_number):
        raise RuntimeError("GitHub API down")

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", _boom)
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(_rerun_event("d-fail", sender="contributor"), None)

    assert enqueued == []


def _partial_rerun_event(delivery, run_id="run42", job="Style check"):
    external_id = json.dumps(
        {"kind": "praktika_job_check", "run_id": run_id, "job": job}, sort_keys=True
    )
    return {
        "headers": {"X-GitHub-Event": "check_run", "X-GitHub-Delivery": delivery},
        "body": json.dumps(
            {
                "action": "rerequested",
                "sender": {"login": "maxknv"},
                "check_run": {
                    "head_sha": "b" * 40,
                    "external_id": external_id,
                    "pull_requests": [{"number": 17}],
                },
                "repository": {"full_name": "owner/repo"},
            }
        ),
    }


def test_partial_rerun_running_writes_request(monkeypatch):
    # Single-check re-run while the run is live (snapshot not finalized):
    # drop a rerun-request for the live orchestrator; do not enqueue.
    mod = _reload_lambda(monkeypatch)
    requests = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    # Live PR head still matches the check's sha ("b"*40) -> not stale.
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(mod, "_load_run_snapshot", lambda run_id: {"finalized": False})
    monkeypatch.setattr(mod, "_write_rerun_request", lambda run_id, jobs, delivery_id: requests.append((run_id, jobs)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append(workflow))

    mod.lambda_handler(_partial_rerun_event("dp1"), None)

    assert requests == [("run42", ["Style check"])]
    assert enqueued == []


def test_partial_rerun_stale_head_is_skipped(monkeypatch):
    # The PR head advanced past the check's sha -> re-running would run the new
    # code; must be dropped (runners clone the live head).
    mod = _reload_lambda(monkeypatch)
    requests = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("e" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(mod, "_load_run_snapshot", lambda run_id: {"finalized": True})
    monkeypatch.setattr(mod, "_write_rerun_request", lambda run_id, jobs, delivery_id: requests.append((run_id, jobs)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append(workflow))

    mod.lambda_handler(_partial_rerun_event("dp-stale"), None)  # check sha "b"*40 != head "e"*40

    assert requests == []
    assert enqueued == []


def test_partial_rerun_external_non_maintainer_skipped(monkeypatch):
    # A fork-PR partial rerun by a non-maintainer is rejected.
    mod = _reload_lambda(monkeypatch)
    requests = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=True)))
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_can_maintain_repo", lambda repo, login, token: False)
    monkeypatch.setattr(mod, "_load_run_snapshot", lambda run_id: {"finalized": True})
    monkeypatch.setattr(mod, "_write_rerun_request", lambda run_id, jobs, delivery_id: requests.append((run_id, jobs)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append(workflow))

    mod.lambda_handler(_partial_rerun_event("dp-ext"), None)

    assert requests == []
    assert enqueued == []


def test_partial_rerun_finished_enqueues_resume(monkeypatch):
    # Single-check re-run on a finished run (snapshot finalized): enqueue a
    # `rerun` resume message targeting just that job.
    mod = _reload_lambda(monkeypatch)
    requests = []
    enqueued = []

    snap = {"finalized": True, "repo": "owner/repo", "head_sha": "b" * 40, "pr_number": 17}
    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(mod, "_load_run_snapshot", lambda run_id: snap)
    monkeypatch.setattr(mod, "_write_rerun_request", lambda run_id, jobs, delivery_id: requests.append((run_id, jobs)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append(workflow))

    mod.lambda_handler(_partial_rerun_event("dp2"), None)

    assert requests == []
    assert len(enqueued) == 1
    wf = enqueued[0]
    assert wf["type"] == "rerun"
    assert wf["run_id"] == "run42"
    assert wf["rerun_jobs"] == ["Style check"]
    assert wf["head_sha"] == "b" * 40


def test_rerun_without_external_id_uses_full_path(monkeypatch):
    # No per-job external_id (e.g. check_suite "re-run all") → full-workflow rerun.
    mod = _reload_lambda(monkeypatch)
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append(workflow))

    mod.lambda_handler(_rerun_event("d-full", sender="contributor"), None)

    assert len(enqueued) == 1
    assert enqueued[0]["type"] == "pull_request"
    assert enqueued[0]["action"] == "rerequested"


def test_external_rerun_of_stale_head_is_not_rebound_to_current(monkeypatch):
    # Security: a maintainer rerun of a STALE check (head A) must not approve or
    # enqueue the current head (B). The rerun carries the stale sha A; state has
    # advanced to B. A must not be silently rebound to B.
    mod = _reload_lambda(monkeypatch)
    state = {"approval_check_id": 77, "head_sha": "b" * 40}  # PR head advanced to B
    enqueued = []
    gate_updates = []
    stored = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=True)))
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: state)
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_can_maintain_repo", lambda repo, login, token: True)
    monkeypatch.setattr(mod, "_update_gate_check", lambda *a, **k: gate_updates.append((a, k)))
    monkeypatch.setattr(mod, "_store_gate_state", lambda *a, **k: stored.append((a, k)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(_rerun_event("d5", head_sha="a" * 40), None)  # stale check A

    assert enqueued == []
    assert gate_updates == []
    assert stored == []


def test_external_rerun_of_current_head_approves_and_enqueues(monkeypatch):
    # A maintainer rerun of the CURRENT head (sha matches state) is an explicit
    # approval of that exact commit: gate approved, workflow enqueued with the
    # authoritative fetched metadata.
    mod = _reload_lambda(monkeypatch)
    state = {"approval_check_id": 77, "head_sha": "b" * 40}
    enqueued = []
    stored = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=True)))
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: state)
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_can_maintain_repo", lambda repo, login, token: True)
    monkeypatch.setattr(mod, "_update_gate_check", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_store_gate_state", lambda *a, **k: stored.append((a, k)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(_rerun_event("d6", head_sha="b" * 40), None)

    assert len(enqueued) == 1
    assert enqueued[0][0]["head_sha"] == "b" * 40
    assert enqueued[0][0]["external_pr"] is True
    assert stored  # gate marked approved for the current head


class _ThrottleS3:
    """Fake S3 supporting the conditional put (IfNoneMatch) the rerun throttle uses."""

    def __init__(self):
        self.store = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, ContentType=None):  # noqa: N803
        if IfNoneMatch == "*" and Key in self.store:
            err = Exception("exists")
            err.response = {"Error": {"Code": "PreconditionFailed"}}
            raise err
        self.store[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        import io
        if Key not in self.store:
            err = Exception("missing")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": io.BytesIO(self.store[Key])}


def test_rerun_throttle_blocks_second_within_window(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    monkeypatch.setattr(mod, "S3_BUCKET", "bucket")
    monkeypatch.setattr(mod, "RERUN_MIN_INTERVAL_S", 120)
    monkeypatch.setattr(mod, "_s3", lambda: _shared_s3, raising=False)
    global _shared_s3
    _shared_s3 = _ThrottleS3()

    assert mod._rerun_throttled(17, 1000.0) is False   # first: claims the window
    assert mod._rerun_throttled(17, 1030.0) is True    # +30s: throttled
    assert mod._rerun_throttled(17, 1200.0) is False   # +200s: window elapsed, allowed
    # A different PR is independent.
    assert mod._rerun_throttled(18, 1030.0) is False


def test_rerun_throttle_disabled_when_interval_zero(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    monkeypatch.setattr(mod, "S3_BUCKET", "bucket")
    monkeypatch.setattr(mod, "RERUN_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(mod, "_s3", lambda: _ThrottleS3())
    assert mod._rerun_throttled(17, 1000.0) is False


def test_second_rapid_partial_rerun_is_throttled(monkeypatch):
    # Two near-simultaneous partial-rerun clicks: only the first is honored.
    mod = _reload_lambda(monkeypatch)
    monkeypatch.setattr(mod, "S3_BUCKET", "bucket")
    monkeypatch.setattr(mod, "RERUN_MIN_INTERVAL_S", 120)
    s3 = _ThrottleS3()
    monkeypatch.setattr(mod, "_s3", lambda: s3)
    requests = []
    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_fetch_pr", lambda repo, pr_number: ("b" * 40, _rerun_meta(external=False)))
    monkeypatch.setattr(mod, "_load_run_snapshot", lambda run_id: {"finalized": False})
    monkeypatch.setattr(mod, "_write_rerun_request", lambda run_id, jobs, delivery_id: requests.append((run_id, jobs)))
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: None)

    mod.lambda_handler(_partial_rerun_event("t1", job="Test"), None)
    mod.lambda_handler(_partial_rerun_event("t2", job="Style Check"), None)

    assert requests == [("run42", ["Test"])]  # second click throttled
