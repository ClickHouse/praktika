import importlib


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
