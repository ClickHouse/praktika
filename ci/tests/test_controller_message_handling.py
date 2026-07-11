import json
import sys
import types

import pytest

from praktika_controller import common, controller


class _Done(Exception):
    pass


class _Log:
    def __init__(self):
        self.events = []

    def info(self, *args, **_kwargs):
        self.events.append(("info", args))

    def warning(self, *args, **_kwargs):
        self.events.append(("warning", args))

    def error(self, *args, **_kwargs):
        self.events.append(("error", args))

    def exception(self, *args, **_kwargs):
        self.events.append(("exception", args))


class _NoopVisibilityHeartbeat:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_runner_cleanup_failure_after_receive_releases_message_and_terminates(
    monkeypatch,
):
    events = []

    class _SQS:
        def get_queue_url(self, QueueName):
            return {"QueueUrl": "queue-url"}

        def get_queue_attributes(self, QueueUrl, AttributeNames):
            return {"Attributes": {"VisibilityTimeout": "30"}}

        def receive_message(self, **kwargs):
            assert kwargs["AttributeNames"] == ["ApproximateReceiveCount"]
            events.append("receive")
            return {
                "Messages": [
                    {
                        "ReceiptHandle": "receipt",
                        "Body": json.dumps(
                            {
                                "type": "job_task",
                                "job_name": "Test",
                            }
                        ),
                        "Attributes": {"ApproximateReceiveCount": "1"},
                    }
                ]
            }

        def delete_message(self, **_kwargs):
            events.append("delete")

        def change_message_visibility(self, **kwargs):
            events.append(("visibility", kwargs["VisibilityTimeout"]))

    def fake_terminate(**kwargs):
        events.append(("terminate", kwargs["reason"]))

    sqs = _SQS()
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_args, **_kwargs: sqs),
    )
    monkeypatch.setattr(
        controller, "_resolve_role_and_queue", lambda: (controller.ROLE_RUNNER, "queue")
    )
    monkeypatch.setattr(controller, "configure_logging", lambda *_args: _Log())
    monkeypatch.setattr(controller, "VisibilityHeartbeat", _NoopVisibilityHeartbeat)
    monkeypatch.setattr(
        controller,
        "clean_work_root",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("still dirty")),
    )
    monkeypatch.setattr(
        controller, "terminate_instance_for_replacement", fake_terminate
    )

    controller.poll()

    assert "delete" not in events
    assert ("visibility", 0) in events
    assert events[-1][0] == "terminate"
    assert "workdir cleanup failed" in events[-1][1]


def test_poll_leaves_processing_exception_for_retry(monkeypatch):
    events = []

    class _SQS:
        def get_queue_url(self, QueueName):
            return {"QueueUrl": "queue-url"}

        def get_queue_attributes(self, QueueUrl, AttributeNames):
            return {"Attributes": {"VisibilityTimeout": "30"}}

        def receive_message(self, **_kwargs):
            events.append("receive")
            if events.count("receive") == 1:
                return {
                    "Messages": [
                        {
                            "ReceiptHandle": "receipt",
                            "Body": json.dumps(
                                {
                                    "type": "job_task",
                                    "job_name": "Test",
                                }
                            ),
                            "Attributes": {"ApproximateReceiveCount": "1"},
                        }
                    ]
                }
            raise _Done()

        def delete_message(self, **_kwargs):
            events.append("delete")

        def change_message_visibility(self, **kwargs):
            events.append(("visibility", kwargs["VisibilityTimeout"]))

    sqs = _SQS()
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_args, **_kwargs: sqs),
    )
    monkeypatch.setattr(
        controller, "_resolve_role_and_queue", lambda: (controller.ROLE_RUNNER, "queue")
    )
    monkeypatch.setattr(controller, "configure_logging", lambda *_args: _Log())
    monkeypatch.setattr(controller, "_prepare_runner_for_task", lambda *_args: "")
    monkeypatch.setattr(controller, "VisibilityHeartbeat", _NoopVisibilityHeartbeat)
    monkeypatch.setattr(
        controller,
        "handle_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(_Done):
        controller.poll()

    assert "delete" not in events
    assert ("visibility", 0) in events


def test_poll_retries_infra_failure_before_max_receives(monkeypatch):
    events = []

    class _SQS:
        def get_queue_url(self, QueueName):
            return {"QueueUrl": "queue-url"}

        def get_queue_attributes(self, QueueUrl, AttributeNames):
            return {"Attributes": {"VisibilityTimeout": "30"}}

        def receive_message(self, **_kwargs):
            events.append("receive")
            if events.count("receive") == 1:
                return {
                    "Messages": [
                        {
                            "ReceiptHandle": "receipt",
                            "Body": json.dumps(
                                {
                                    "type": "job_task",
                                    "job_name": "Test",
                                    "final_state_s3_bucket": "bucket",
                                    "final_state_s3_key": "runs/1/Test/final.json",
                                }
                            ),
                            "Attributes": {"ApproximateReceiveCount": "2"},
                        }
                    ]
                }
            raise _Done()

        def delete_message(self, **_kwargs):
            events.append("delete")

        def change_message_visibility(self, **kwargs):
            events.append(("visibility", kwargs["VisibilityTimeout"]))

    sqs = _SQS()
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_args, **_kwargs: sqs),
    )
    monkeypatch.setattr(
        controller, "_resolve_role_and_queue", lambda: (controller.ROLE_RUNNER, "queue")
    )
    monkeypatch.setattr(controller, "configure_logging", lambda *_args: _Log())
    monkeypatch.setattr(controller, "_prepare_runner_for_task", lambda *_args: "")
    monkeypatch.setattr(controller, "VisibilityHeartbeat", _NoopVisibilityHeartbeat)
    monkeypatch.setattr(
        controller,
        "_write_infra_failure_final",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("must not write final before max receives")
        ),
    )
    monkeypatch.setattr(
        controller,
        "handle_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(_Done):
        controller.poll()

    assert "delete" not in events
    assert ("visibility", 0) in events


def test_poll_deletes_after_infra_failure_final_state_at_max_receives(monkeypatch):
    events = []

    class _SQS:
        def get_queue_url(self, QueueName):
            return {"QueueUrl": "queue-url"}

        def get_queue_attributes(self, QueueUrl, AttributeNames):
            return {"Attributes": {"VisibilityTimeout": "30"}}

        def receive_message(self, **_kwargs):
            events.append("receive")
            if events.count("receive") == 1:
                return {
                    "Messages": [
                        {
                            "ReceiptHandle": "receipt",
                            "Body": json.dumps(
                                {
                                    "type": "job_task",
                                    "job_name": "Test",
                                    "final_state_s3_bucket": "bucket",
                                    "final_state_s3_key": "runs/1/Test/final.json",
                                }
                            ),
                            "Attributes": {"ApproximateReceiveCount": "3"},
                        }
                    ]
                }
            raise _Done()

        def delete_message(self, **_kwargs):
            events.append("delete")

    sqs = _SQS()
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_args, **_kwargs: sqs),
    )
    monkeypatch.setattr(
        controller, "_resolve_role_and_queue", lambda: (controller.ROLE_RUNNER, "queue")
    )
    monkeypatch.setattr(controller, "configure_logging", lambda *_args: _Log())
    monkeypatch.setattr(controller, "_prepare_runner_for_task", lambda *_args: "")
    monkeypatch.setattr(controller, "VisibilityHeartbeat", _NoopVisibilityHeartbeat)
    monkeypatch.setattr(controller, "_write_infra_failure_final", lambda *_args: True)
    monkeypatch.setattr(
        controller,
        "handle_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(_Done):
        controller.poll()

    assert "delete" in events


def _workflow_sqs(events, receive_count, stop_after_first):
    class _SQS:
        def get_queue_url(self, QueueName):
            return {"QueueUrl": "queue-url"}

        def get_queue_attributes(self, QueueUrl, AttributeNames):
            return {"Attributes": {"VisibilityTimeout": "30"}}

        def receive_message(self, **_kwargs):
            events.append("receive")
            if events.count("receive") == 1:
                return {
                    "Messages": [
                        {
                            "ReceiptHandle": "receipt",
                            "Body": json.dumps(
                                {"type": "pull_request", "pr_number": 130}
                            ),
                            "Attributes": {
                                "ApproximateReceiveCount": str(receive_count)
                            },
                        }
                    ]
                }
            if stop_after_first:
                raise _Done()
            return {"Messages": []}

        def delete_message(self, **_kwargs):
            events.append("delete")

        def change_message_visibility(self, **kwargs):
            events.append(("visibility", kwargs["VisibilityTimeout"]))

    return _SQS()


def _setup_workflow_poll(monkeypatch, sqs):
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_args, **_kwargs: sqs),
    )
    monkeypatch.setattr(
        controller,
        "_resolve_role_and_queue",
        lambda: (controller.ROLE_WORKFLOW, "workflow-orchestrator-base"),
    )
    monkeypatch.setattr(controller, "configure_logging", lambda *_args: _Log())
    monkeypatch.setattr(controller, "_prepare_runner_for_task", lambda *_args: "")
    monkeypatch.setattr(controller, "VisibilityHeartbeat", _NoopVisibilityHeartbeat)
    monkeypatch.setattr(
        controller,
        "handle_workflow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            controller.InfraOrchestrationError("infra")
        ),
    )


def test_poll_workflow_infra_failure_terminates_for_fresh_retry(monkeypatch):
    # Before the cap: release the message and replace this instance so a fresh
    # orchestrator retries.
    events = []
    sqs = _workflow_sqs(events, receive_count=2, stop_after_first=False)
    _setup_workflow_poll(monkeypatch, sqs)

    def fake_terminate(**kwargs):
        events.append(("terminate", kwargs["reason"]))

    monkeypatch.setattr(
        controller, "terminate_instance_for_replacement", fake_terminate
    )

    controller.poll()  # returns right after terminating

    assert "delete" not in events
    assert ("visibility", 0) in events
    assert events[-1][0] == "terminate"
    assert "workflow infra failure" in events[-1][1]


def test_poll_workflow_infra_failure_gives_up_at_max_receives(monkeypatch):
    # At the cap: drop the message (the orchestrator already finalized its own
    # check as failed on this attempt) and do NOT churn another instance.
    events = []
    sqs = _workflow_sqs(events, receive_count=3, stop_after_first=True)
    _setup_workflow_poll(monkeypatch, sqs)

    monkeypatch.setattr(
        controller,
        "terminate_instance_for_replacement",
        lambda **kwargs: events.append(("terminate", kwargs.get("reason"))),
    )

    with pytest.raises(_Done):
        controller.poll()

    assert "delete" in events
    assert not any(e[0] == "terminate" for e in events if isinstance(e, tuple))


def test_post_early_check_returns_id(monkeypatch):
    monkeypatch.setattr(
        common, "_github_api", lambda method, url, token, body=None, **_k: {"id": 42}
    )
    assert common.post_early_check("o/r", "deadbeef", "tok", "CI") == 42


def test_post_early_check_is_best_effort_on_error(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("api down")

    monkeypatch.setattr(common, "_github_api", boom)
    assert common.post_early_check("o/r", "deadbeef", "tok", "CI", log=_Log()) is None


def test_finalize_check_noop_on_missing_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        common, "_github_api", lambda *a, **_k: calls.append(a) or {}
    )
    common.finalize_check("o/r", None, "tok", "failure", "t", "s")
    assert calls == []


def _setup_handle_workflow(monkeypatch, tmp_path, clone_error=None):
    monkeypatch.setattr(controller, "get_github_token", lambda *_a, **_k: "tok")
    monkeypatch.setattr(controller, "venv_env", lambda *_a, **_k: {})
    monkeypatch.setattr(controller, "praktika_command", lambda *_a, **_k: ["orch"])
    monkeypatch.setattr(
        controller, "_resolve_runtime", lambda *_a, **_k: ("base", "/venv")
    )
    runs = []

    def fake_run(cmd, **kwargs):
        runs.append((cmd, kwargs))
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(controller.subprocess, "run", fake_run)

    def fake_clone(*_a, **_k):
        if clone_error is not None:
            raise clone_error
        return (str(tmp_path), "actualsha")

    monkeypatch.setattr(controller, "clone_repo", fake_clone)
    return runs


def test_handle_workflow_posts_early_check_before_clone_and_threads_id(
    monkeypatch, tmp_path
):
    order = []
    monkeypatch.setattr(
        controller,
        "post_early_check",
        lambda repo, sha, tok, name, **_k: order.append(("early", repo, sha, name))
        or 999,
    )
    finalize_calls = []
    monkeypatch.setattr(
        controller,
        "finalize_check",
        lambda *a, **_k: finalize_calls.append(a),
    )
    runs = _setup_handle_workflow(monkeypatch, tmp_path)
    orig_clone = controller.clone_repo

    def tracking_clone(*a, **k):
        order.append(("clone",))
        return orig_clone(*a, **k)

    monkeypatch.setattr(controller, "clone_repo", tracking_clone)

    event = {
        "type": "pull_request",
        "repo": "o/r",
        "pr_number": 130,
        "head_sha": "deadbeefcafe",
    }
    controller.handle_workflow(event, _Log(), "q", receive_count=1)

    # Early check posted before the clone.
    assert order[0] == ("early", "o/r", "deadbeefcafe", controller.EARLY_CHECK_NAME)
    assert ("clone",) in order and order.index(("early", "o/r", "deadbeefcafe", controller.EARLY_CHECK_NAME)) < order.index(("clone",))
    # Its id is threaded into the orchestrate subprocess env.
    orch = next(r for r in runs if r[0] == ["orch"])
    assert orch[1]["env"]["PRAKTIKA_BOOTSTRAP_CHECK_RUN_ID"] == "999"
    # Not finalized by the controller on success (orchestrator owns it).
    assert finalize_calls == []


def test_handle_workflow_finalizes_early_check_on_clone_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(controller, "post_early_check", lambda *_a, **_k: 555)
    finalize_calls = []
    monkeypatch.setattr(
        controller,
        "finalize_check",
        lambda repo, cid, tok, conclusion, *a, **_k: finalize_calls.append(
            (cid, conclusion)
        ),
    )
    _setup_handle_workflow(monkeypatch, tmp_path, clone_error=RuntimeError("clone boom"))

    with pytest.raises(RuntimeError, match="clone boom"):
        controller.handle_workflow(
            {"type": "pull_request", "repo": "o/r", "pr_number": 1, "head_sha": "sha"},
            _Log(),
            "q",
        )

    assert finalize_calls == [(555, "failure")]


def test_cancel_watchdog_terminates_process_group(monkeypatch):
    calls = []

    class _S3:
        def head_object(self, **_kwargs):
            return {}

    proc = types.SimpleNamespace(pid=123)
    monkeypatch.setattr(
        common,
        "terminate_process_group",
        lambda proc_arg, *_args, **_kwargs: calls.append(proc_arg),
    )

    watchdog = common.CancelWatchdog(
        _S3(), "bucket", "runs/1/cancel", proc, interval=0, log=_Log()
    )
    watchdog._stop = types.SimpleNamespace(wait=lambda _interval: False)

    watchdog._run()

    assert calls == [proc]
