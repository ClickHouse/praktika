import importlib


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
