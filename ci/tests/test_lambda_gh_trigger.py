import importlib
import json


def _reload_lambda(monkeypatch, allowed_push_branches=None):
    if allowed_push_branches is None:
        monkeypatch.delenv("ALLOWED_PUSH_BRANCHES", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_PUSH_BRANCHES", allowed_push_branches)
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


def test_external_pr_creates_gate_check_instead_of_enqueuing(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    payload = _pr_payload(external=True)
    gate_calls = []
    stored = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
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
        "workflow": _pr_payload(external=True)["pull_request"],
    }
    gate_calls = []
    stored = []
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_cancel_runs_before", lambda pr_number, event_ts: None)
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
    assert stored[0][3] == "auto"
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


def test_external_rerun_requires_maintainer(monkeypatch):
    mod = _reload_lambda(monkeypatch)
    workflow = {
        "repo": "owner/repo",
        "pr_number": 17,
        "head_sha": "d" * 40,
        "head_ref": "feature",
        "base_ref": "main",
        "type": "pull_request",
        "action": "rerequested",
        "event_ts": 123.0,
        "sender": "contributor",
        "title": "",
        "draft": False,
        "labels": [],
        "external_pr": False,
        "head_repo": "",
    }
    state = {
        "workflow": {"external_pr": True},
        "approval_check_id": 77,
        "head_sha": workflow["head_sha"],
    }
    enqueued = []

    monkeypatch.setattr(mod, "verify_github_signature", lambda event: None)
    monkeypatch.setattr(mod, "_build_rerun_workflow", lambda check_obj, payload, event_ts: (workflow, 17))
    monkeypatch.setattr(mod, "_load_approval_state", lambda repo, pr_number: state)
    monkeypatch.setattr(mod, "_get_github_token", lambda required_permissions=None: "tok")
    monkeypatch.setattr(mod, "_can_maintain_repo", lambda repo, login, token: False)
    monkeypatch.setattr(mod, "_enqueue", lambda workflow, delivery_id: enqueued.append((workflow, delivery_id)))

    mod.lambda_handler(
        {
            "headers": {
                "X-GitHub-Event": "check_run",
                "X-GitHub-Delivery": "d4",
            },
            "body": json.dumps(
                {
                    "action": "rerequested",
                    "sender": {"login": "contributor"},
                    "check_run": {"pull_requests": [{"number": 17}]},
                    "repository": {"full_name": "owner/repo"},
                }
            ),
        },
        None,
    )

    assert enqueued == []
