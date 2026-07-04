from types import SimpleNamespace

import pytest

from praktika.orchestrator import ai
from praktika.orchestrator.ai import (
    Advisor,
    _apply_edits,
    _patch_commit_message,
    build_observation,
)
from praktika.orchestrator.ai.anthropic import (
    AnthropicProvider,
    BedrockProvider,
    _collect_log_urls,
    _execute_tool,
    _grep_log,
    _grep_repo,
    _parse,
    _price_per_mtok,
    _read_file,
    _safe_repo_path,
)
from praktika.orchestrator.ai.mock import MockProvider
from praktika.orchestrator.ai.provider import Observation, resolve, resolve_provider


def _job(name, status, started_at=None, finished_at=None, filter_reason=None):
    return SimpleNamespace(
        name=name,
        status=SimpleNamespace(value=status),
        started_at=started_at,
        finished_at=finished_at,
        filter_reason=filter_reason,
    )


def _state(jobs, summary="summary"):
    return SimpleNamespace(
        jobs={j.name: j for j in jobs},
        md_status_summary=lambda: summary,
    )


EVENT = {
    "type": "pull_request",
    "action": "synchronize",
    "pr_number": 7,
    "head_sha": "deadbeef",
    "head_ref": "feature",
}


# --------------------------------------------------------------- registry


def test_registry_resolves_mock():
    assert resolve("mock") is MockProvider


def test_registry_unknown_raises():
    with pytest.raises(KeyError):
        resolve("does-not-exist")


# --------------------------------------------------------------- factory


def test_maybe_create_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", False, raising=False)
    assert Advisor.maybe_create(run_id="r1") is None


def test_maybe_create_enabled_returns_advisor(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)
    assert isinstance(advisor, Advisor)
    assert isinstance(advisor._provider, MockProvider)


def test_maybe_create_unknown_provider_disables(monkeypatch):
    # An older runtime that doesn't register the configured provider must
    # disable the advisor, not crash orchestration (see PR #130 hang).
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "bedrock-from-the-future", raising=False)
    assert Advisor.maybe_create(run_id="r1", local_mode=True) is None


# --------------------------------------------------------------- mock provider


def test_mock_provider_other_hooks_are_noop():
    # Only on_job_failure is implemented; the rest stay the inherited no-ops
    # (return None), so green/run-level events never reach the model.
    p = MockProvider(model="m")
    obs = Observation(event=EVENT, jobs=[], changed=[], summary="")
    assert p.on_job_success(obs) is None
    assert p.on_run_start(obs) is None
    assert p.on_run_finish(obs) is None


def test_mock_provider_proposes_fix_on_failure():
    obs = Observation(
        event=EVENT, jobs=[], changed=[{"name": "Style check", "status": "failure"}], summary=""
    )
    turn = MockProvider(model="m").on_job_failure(obs)
    types = [d["type"] for d in turn.decision]
    assert "propose_fix" in types
    # Still non-actionable and free.
    assert turn.usage.cost_usd == 0.0


# --------------------------------------------------------------- observation


def test_build_observation_snapshot():
    state = _state(
        [
            _job("A", "success", started_at=100.0, finished_at=105.0),
            _job("B", "running"),
        ]
    )
    obs = build_observation(state, EVENT, changed=[{"name": "A", "status": "success"}])
    assert obs.event["pr_number"] == 7
    assert obs.summary == "summary"
    by_name = {j["name"]: j for j in obs.jobs}
    assert by_name["A"]["duration_s"] == 5.0
    assert "duration_s" not in by_name["B"]


def test_failed_change_carries_result_digest():
    """A job that failed this turn gets a compact, failure-only Result digest
    attached to its `changed` entry — passing sub-results are dropped."""
    job = _job("Tests", "failure")
    job.result = {
        "name": "Tests",
        "status": "FAIL",
        "info": "Failures: 1/2",
        "results": [
            {"name": "test_ok", "status": "OK", "info": ""},
            {"name": "test_bad", "status": "FAIL", "info": "assert 1 == 2"},
        ],
        "ext": {"errors": ["segfault in server log"]},
    }
    state = _state([job])

    obs = build_observation(
        state, EVENT, changed=[{"name": "Tests", "status": "failure"}]
    )

    digest = obs.changed[0]["result"]
    assert digest["status"] == "FAIL"
    assert digest["info"] == "Failures: 1/2"
    assert digest["failed"] == [
        {"name": "test_bad", "status": "FAIL", "info": "assert 1 == 2"}
    ]
    assert digest["errors"] == ["segfault in server log"]


def test_successful_change_carries_no_result_digest():
    """Green jobs add no Result detail to the prompt, even if a Result exists."""
    job = _job("Build", "success")
    job.result = {"name": "Build", "status": "OK", "results": []}
    state = _state([job])

    obs = build_observation(
        state, EVENT, changed=[{"name": "Build", "status": "success"}]
    )

    assert "result" not in obs.changed[0]


def test_build_observation_does_not_mutate_changed():
    """Enrichment works on a copy — the caller's `changed` list is untouched."""
    job = _job("Tests", "failure")
    job.result = {"name": "Tests", "status": "FAIL", "results": []}
    state = _state([job])
    changed = [{"name": "Tests", "status": "failure"}]

    build_observation(state, EVENT, changed=changed)

    assert changed == [{"name": "Tests", "status": "failure"}]


# --------------------------------------------------------------- advisor flow


def test_turn_fires_only_on_new_failure(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    a = _job("A", "running")
    b = _job("B", "pending")
    state = _state([a, b])

    # Nothing terminal yet -> no turn.
    assert advisor.on_workflow_update(state, EVENT) is None

    # A succeeds -> on_job_success is a no-op, so no turn and no cost.
    a.status = SimpleNamespace(value="success")
    assert advisor.on_workflow_update(state, EVENT) is None
    assert advisor._ledger.turns == 0

    # B fails -> on_job_failure fires exactly one turn.
    b.status = SimpleNamespace(value="failure")
    turn = advisor.on_workflow_update(state, EVENT)
    assert turn is not None
    assert advisor._ledger.turns == 1

    # No further change -> no new turn.
    assert advisor.on_workflow_update(state, EVENT) is None
    assert advisor._ledger.turns == 1


def test_skipped_and_cancelled_do_not_fire(monkeypatch):
    # Skipped/cancelled are terminal but route to no hook — they carry no
    # problem to act on, so they never consult the model.
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    a = _job("A", "skipped")
    b = _job("B", "cancelled")
    state = _state([a, b])
    assert advisor.on_workflow_update(state, EVENT) is None
    assert advisor._ledger.turns == 0


def test_advisory_only_no_mutation_and_zero_cost(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    a = _job("A", "failure", started_at=1.0, finished_at=2.0)
    state = _state([a])
    turn = advisor.on_workflow_update(state, EVENT)

    # Mock's decision is non-actionable, the job state is untouched, free.
    assert all(d["type"] in ("note", "propose_fix") for d in turn.decision)
    assert a.status.value == "failure"
    assert advisor._ledger.cost_usd == 0.0


def test_provider_error_does_not_raise(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    def boom(observation):
        raise RuntimeError("provider exploded")

    advisor._provider.on_job_failure = boom

    a = _job("A", "failure")
    turn = advisor.on_workflow_update(_state([a]), EVENT)
    assert turn.error is not None
    assert "provider exploded" in turn.error


# --------------------------------------------------------------- provider resolution


def test_registry_resolves_anthropic_and_bedrock():
    assert resolve("anthropic") is AnthropicProvider
    assert resolve("bedrock") is BedrockProvider


def test_resolve_provider_by_name():
    p = resolve_provider("anthropic", model="m")
    assert isinstance(p, AnthropicProvider)
    assert p.model == "m"


def test_resolve_provider_by_class():
    p = resolve_provider(AnthropicProvider, model="x")
    assert isinstance(p, AnthropicProvider)
    assert p.model == "x"


def test_resolve_provider_instance_passthrough():
    inst = AnthropicProvider(model="z")
    assert resolve_provider(inst) is inst


# --------------------------------------------------------------- reply parsing


@pytest.mark.parametrize(
    "text,reasoning,types",
    [
        # plain JSON
        ('{"reasoning": "r", "decision": [{"type": "t", "detail": "d"}]}', "r", ["t"]),
        # markdown-fenced JSON
        ('```json\n{"reasoning": "r2", "decision": []}\n```', "r2", []),
        # prose preamble before the JSON object
        (
            'Sure:\n{"reasoning": "r3", "decision": [{"type": "x", "detail": "y"}]}',
            "r3",
            ["x"],
        ),
    ],
)
def test_parse_json_variants(text, reasoning, types):
    r, d = _parse(text)
    assert r == reasoning
    assert [x["type"] for x in d] == types


def test_parse_non_json_falls_back_to_reasoning():
    r, d = _parse("the build is fine, nothing to do")
    assert r == "the build is fine, nothing to do"
    assert d == []


def test_parse_non_list_decision_coerced_empty():
    r, d = _parse('{"reasoning": "r", "decision": "oops"}')
    assert d == []


# --------------------------------------------------------------- pricing


def test_price_per_mtok_prefix_tolerant():
    assert _price_per_mtok("claude-opus-4-8") == (5.0, 25.0)
    assert _price_per_mtok("anthropic.claude-opus-4-8") == (5.0, 25.0)
    assert _price_per_mtok("eu.anthropic.claude-sonnet-5") == (3.0, 15.0)
    assert _price_per_mtok("unknown-model") == (0.0, 0.0)


# --------------------------------------------------------------- anthropic / bedrock decide


def _fake_resp(text, **usage):
    counts = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    counts.update(usage)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(**counts),
    )


class _FakeClient:
    """Stands in for anthropic.Anthropic / AnthropicBedrockMantle."""

    def __init__(self, resp):
        self.calls = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return resp

        self.messages = _Messages()


def test_anthropic_decide_parses_and_costs():
    p = AnthropicProvider(model="claude-haiku-4-5")
    p._client = _FakeClient(
        _fake_resp(
            '{"reasoning": "build broke", "decision": [{"type": "inspect_logs", "detail": "look"}]}',
            input_tokens=100,
            output_tokens=50,
        )
    )
    turn = p.on_job_failure(
        Observation(event=EVENT, changed=[{"name": "B", "status": "failure"}], summary="s")
    )
    assert turn.error is None
    assert turn.reasoning == "build broke"
    assert [d["type"] for d in turn.decision] == ["inspect_logs"]
    assert turn.usage.provider == "anthropic"
    assert turn.usage.model == "claude-haiku-4-5"
    assert turn.usage.input_tokens == 100
    assert turn.usage.output_tokens == 50
    # haiku: (100 * 1.0 + 50 * 5.0) / 1e6
    assert turn.usage.cost_usd == round((100 * 1.0 + 50 * 5.0) / 1_000_000, 6)


def test_on_job_failure_does_not_send_output_config():
    # output_config is rejected on Bedrock; the provider must stay portable.
    p = AnthropicProvider(model="claude-haiku-4-5")
    p._client = _FakeClient(_fake_resp('{"reasoning": "ok", "decision": []}'))
    p.on_job_failure(Observation(changed=[{"name": "B", "status": "failure"}]))
    assert "output_config" not in p._client.calls[0]


def test_on_job_failure_offers_repo_tools_without_links():
    # A failure with no log links still gets the repo-read tools, but not
    # fetch_log (nothing to fetch).
    p = AnthropicProvider(model="claude-haiku-4-5")
    p._client = _FakeClient(_fake_resp('{"reasoning": "ok", "decision": []}'))
    p.on_job_failure(Observation(changed=[{"name": "B", "status": "failure"}]))
    tool_names = {t["name"] for t in p._client.calls[0].get("tools", [])}
    assert "read_file" in tool_names
    assert "grep_repo" in tool_names
    assert "fetch_log" not in tool_names


def test_on_job_failure_offers_fetch_log_when_links_present():
    p = AnthropicProvider(model="claude-haiku-4-5")
    p._client = _FakeClient(_fake_resp('{"reasoning": "ok", "decision": []}'))
    obs = Observation(
        changed=[
            {
                "name": "B",
                "status": "failure",
                "result": {"status": "FAIL", "links": ["https://s3/b.log"]},
            }
        ]
    )
    p.on_job_failure(obs)
    tool_names = {t["name"] for t in p._client.calls[0].get("tools", [])}
    assert {"read_file", "grep_repo", "fetch_log"} <= tool_names


# --------------------------------------------------------------- log fetch tool


def test_collect_log_urls_from_failed_digests():
    obs = Observation(
        changed=[
            {
                "name": "Tests",
                "status": "failure",
                "result": {
                    "status": "FAIL",
                    "links": ["https://s3/job.log"],
                    "failed": [{"name": "t", "status": "FAIL", "links": ["https://s3/t.log"]}],
                },
            },
            {"name": "OK", "status": "success"},  # no digest -> contributes nothing
        ]
    )
    assert _collect_log_urls(obs) == {"https://s3/job.log", "https://s3/t.log"}


def test_execute_tool_rejects_url_outside_allowlist():
    out = _execute_tool("fetch_log", {"url": "https://evil/x"}, {"https://s3/ok.log"})
    assert "not in the allowed set" in out


def test_execute_tool_unknown_tool():
    assert "unknown tool" in _execute_tool("nope", {}, set())


def test_execute_tool_fetches_allowed_url(monkeypatch):
    import praktika.orchestrator.ai.anthropic as anth

    seen = {}

    def fake_fetch(url, grep=None, max_bytes=None, from_end=True):
        seen["url"] = url
        seen["grep"] = grep
        return "LOG BODY"

    monkeypatch.setattr(anth, "_fetch_log", fake_fetch)
    out = _execute_tool(
        "fetch_log", {"url": "https://s3/ok.log", "grep": "Error"}, {"https://s3/ok.log"}
    )
    assert out == "LOG BODY"
    assert seen == {"url": "https://s3/ok.log", "grep": "Error"}


def test_grep_log_matches_with_context_and_separators():
    # ERROR matches at indexes 3 and 9; with ±2 context the two windows
    # (1..5 and 7..10) don't touch, so a `--` separator appears and the
    # out-of-window lines L0 and L6 are excluded.
    text = "\n".join(
        ["L0", "L1", "L2", "ERROR one", "L4", "L5", "L6", "L7", "L8", "ERROR two", "L10"]
    )
    out = _grep_log(text, "error")
    assert "ERROR one" in out
    assert "ERROR two" in out
    assert "--" in out  # non-adjacent hunks separated
    assert "L0" not in out  # before the first context window
    assert "L6" not in out  # in the gap between windows


def test_grep_log_no_match():
    assert _grep_log("nothing to see", "boom") == "(no lines matching 'boom')"


# --------------------------------------------------------------- repo-read tools


def test_safe_repo_path_rejects_escape(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _safe_repo_path("../../etc/passwd") is None
    assert _safe_repo_path("/etc/passwd") is None
    inside = _safe_repo_path("sub/file.py")
    assert inside is not None and inside.startswith(str(tmp_path))


def test_read_file_returns_numbered_lines(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mod.py").write_text("alpha\nbravo\ncharlie\n")
    out = _read_file("mod.py", start_line=2, max_lines=1)
    assert out == "2: bravo"


def test_read_file_escape_and_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert "escapes the repository" in _read_file("../secret")
    assert "not a file" in _read_file("does_not_exist.py")


def test_grep_repo_finds_match(monkeypatch, tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("def boom():\n    raise RuntimeError('kaboom')\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    out = _grep_repo("kaboom")
    assert "a.py:2:" in out
    assert "(no matches" in _grep_repo("zzz-not-present-zzz")


def test_execute_tool_dispatches_read_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.py").write_text("one\ntwo\n")
    out = _execute_tool("read_file", {"path": "x.py"}, set())
    assert "1: one" in out and "2: two" in out


# --------------------------------------------------------------- tool-use loop


def _text_resp(text, **usage):
    return _fake_resp(text, **usage)


def _tool_use_resp(tool_id, name, tool_input, **usage):
    counts = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    counts.update(usage)
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)],
        usage=SimpleNamespace(**counts),
    )


class _SeqFakeClient:
    """Returns a queued sequence of responses, one per create() call."""

    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return outer._responses.pop(0)

        self.messages = _Messages()


def test_decide_runs_tool_loop_and_accumulates_usage(monkeypatch):
    import praktika.orchestrator.ai.anthropic as anth

    monkeypatch.setattr(anth, "_fetch_log", lambda url, **kw: "FATAL: boom at line 9")

    obs = Observation(
        changed=[
            {
                "name": "Build",
                "status": "failure",
                "result": {"status": "FAIL", "links": ["https://s3/build.log"]},
            }
        ]
    )
    p = AnthropicProvider(model="claude-haiku-4-5")
    p._client = _SeqFakeClient(
        [
            _tool_use_resp(
                "tu1", "fetch_log", {"url": "https://s3/build.log", "grep": "FATAL"},
                input_tokens=100, output_tokens=20,
            ),
            _text_resp(
                '{"reasoning": "linker error", "root_cause": "missing symbol", '
                '"decision": [{"type": "cancel_run", "detail": "build is fundamentally broken"}]}',
                input_tokens=130, output_tokens=40,
            ),
        ]
    )

    turn = p.on_job_failure(obs)

    # Two round-trips: the tool_use round and the final answer.
    assert len(p._client.calls) == 2
    # First call offered the tool; second call carried the tool_result back.
    assert p._client.calls[0]["tools"]
    tool_result_sent = any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for m in p._client.calls[1]["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
    )
    assert tool_result_sent
    # Decision + root cause folded into reasoning.
    assert [d["type"] for d in turn.decision] == ["cancel_run"]
    assert "Root cause: missing symbol" in turn.reasoning
    # Usage summed across both calls.
    assert turn.usage.input_tokens == 230
    assert turn.usage.output_tokens == 60


def test_parse_folds_root_cause_into_reasoning():
    r, d = _parse('{"reasoning": "it broke", "root_cause": "OOM", "decision": []}')
    assert r == "it broke\n\nRoot cause: OOM"
    assert d == []


def test_parse_empty_root_cause_left_out():
    r, _ = _parse('{"reasoning": "fine", "root_cause": "", "decision": []}')
    assert r == "fine"


# --------------------------------------------------------------- cancel_run dispatch


def test_cancel_run_decision_cancels_the_run(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    state = _state([_job("Build", "failure")])
    state.cancelled = False

    advisor._provider.on_job_failure = lambda obs: ai.Turn(
        reasoning="broken", decision=[{"type": "cancel_run", "detail": "build broke"}]
    )

    advisor.on_workflow_update(state, EVENT)
    assert state.cancelled is True


def test_non_cancel_decision_leaves_run_alone(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    state = _state([_job("Build", "failure")])
    state.cancelled = False

    advisor._provider.on_job_failure = lambda obs: ai.Turn(
        reasoning="flaky", decision=[{"type": "continue", "detail": "retry-friendly"}]
    )

    advisor.on_workflow_update(state, EVENT)
    assert state.cancelled is False


def test_error_turn_does_not_cancel(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    state = _state([_job("Build", "failure")])
    state.cancelled = False

    # A provider that errors must never trigger a destructive action.
    advisor._provider.on_job_failure = lambda obs: ai.Turn(
        error="boom", decision=[{"type": "cancel_run", "detail": "x"}]
    )

    advisor.on_workflow_update(state, EVENT)
    assert state.cancelled is False


def test_bedrock_defaults_and_region(monkeypatch):
    import praktika.settings as psettings

    monkeypatch.setattr(psettings.Settings, "AWS_REGION", "eu-test-1", raising=False)
    p = BedrockProvider()
    assert p.name == "bedrock"
    assert p.resolved_model() == "anthropic.claude-opus-4-8"
    assert p._region() == "eu-test-1"


def test_bedrock_explicit_region_wins():
    assert BedrockProvider(aws_region="us-west-2")._region() == "us-west-2"


def test_bedrock_decide_costs_with_prefixed_model():
    p = BedrockProvider(model="anthropic.claude-opus-4-8")
    p._client = _FakeClient(
        _fake_resp('{"reasoning": "r", "decision": []}', input_tokens=10, output_tokens=4)
    )
    turn = p.on_job_failure(Observation())
    assert turn.error is None
    assert turn.usage.provider == "bedrock"
    assert turn.usage.model == "anthropic.claude-opus-4-8"
    # opus pricing resolved through the anthropic. prefix
    assert turn.usage.cost_usd == round((10 * 5.0 + 4 * 25.0) / 1_000_000, 6)


def test_error_turn_stamps_resolved_model(monkeypatch):
    # A provider that fails before a call still names the model it would use.
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "bedrock", raising=False)
    monkeypatch.setattr(ai.Settings, "AI_MODEL", "", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    def boom(observation):
        raise RuntimeError("no creds")

    advisor._provider.on_job_failure = boom

    turn = advisor.on_workflow_update(_state([_job("A", "failure")]), EVENT)
    assert turn.error is not None
    assert turn.usage.provider == "bedrock"
    assert turn.usage.model == "anthropic.claude-opus-4-8"


# ---------------------------------------------- cancel_and_patch: edit application


def _write(root, name, text):
    f = root / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def test_apply_edits_single(tmp_path):
    _write(tmp_path, "a.py", "x = '0.1.5'\n")
    ok, files, patch, err = _apply_edits(
        [{"path": "a.py", "search": "'0.1.5'", "replace": "'0.1.6'"}], root=str(tmp_path)
    )
    assert ok and files == ["a.py"] and err == ""
    assert (tmp_path / "a.py").read_text() == "x = '0.1.6'\n"
    assert "-x = '0.1.5'" in patch and "+x = '0.1.6'" in patch


def test_apply_edits_multi_file_and_same_file(tmp_path):
    _write(tmp_path, "a.py", "A\nB\n")
    _write(tmp_path, "b.py", "C\n")
    ok, files, _, err = _apply_edits(
        [
            {"path": "a.py", "search": "A", "replace": "A1"},
            {"path": "a.py", "search": "B", "replace": "B1"},
            {"path": "b.py", "search": "C", "replace": "C1"},
        ],
        root=str(tmp_path),
    )
    assert ok and files == ["a.py", "b.py"] and err == ""
    assert (tmp_path / "a.py").read_text() == "A1\nB1\n"
    assert (tmp_path / "b.py").read_text() == "C1\n"


def test_apply_edits_non_unique_aborts_all(tmp_path):
    _write(tmp_path, "a.py", "dup\ndup\n")
    _write(tmp_path, "b.py", "keep\n")
    ok, _, _, err = _apply_edits(
        [
            {"path": "b.py", "search": "keep", "replace": "changed"},
            {"path": "a.py", "search": "dup", "replace": "x"},
        ],
        root=str(tmp_path),
    )
    assert not ok and "exactly once" in err
    # All-or-nothing: b.py stays untouched even though its edit was valid + first.
    assert (tmp_path / "b.py").read_text() == "keep\n"


def test_apply_edits_zero_match(tmp_path):
    _write(tmp_path, "a.py", "hello\n")
    ok, _, _, err = _apply_edits(
        [{"path": "a.py", "search": "nope", "replace": "x"}], root=str(tmp_path)
    )
    assert not ok and "found 0" in err


def test_apply_edits_path_escape(tmp_path):
    ok, _, _, err = _apply_edits(
        [{"path": "../evil", "search": "a", "replace": "b"}], root=str(tmp_path)
    )
    assert not ok and "escapes repo" in err


def test_apply_edits_missing_file(tmp_path):
    ok, _, _, err = _apply_edits(
        [{"path": "nope.py", "search": "a", "replace": "b"}], root=str(tmp_path)
    )
    assert not ok and "no such file" in err


def test_apply_edits_empty(tmp_path):
    ok, _, _, err = _apply_edits([], root=str(tmp_path))
    assert not ok and "no edits" in err


def test_apply_edits_noop_change(tmp_path):
    _write(tmp_path, "a.py", "same\n")
    ok, _, _, err = _apply_edits(
        [{"path": "a.py", "search": "same", "replace": "same"}], root=str(tmp_path)
    )
    assert not ok and "no change" in err


def test_patch_commit_message():
    m = _patch_commit_message(
        "Bump version from 0.1.5 to 0.1.6.\nMatches pyproject.", round_id="rd-9"
    )
    assert m.splitlines()[0].startswith("AI fix: Bump version")
    assert "AI-Session-Round: rd-9" in m
    # No Co-Authored-By: the app bot authors the commit via the Git Data API.
    assert "Co-Authored-By" not in m


# ---------------------------------------------- cancel_and_patch: dispatch


class _FakeSession:
    def __init__(self, can_continue=(True, ""), round_id="rd-1"):
        self._can = can_continue
        self._round_id = round_id
        self.edits = []

    def can_continue_round(self):
        return self._can

    def current_round_id(self):
        return self._round_id

    def record_edit(self, patch_text, commit_sha="", files=None):
        self.edits.append({"patch": patch_text, "commit_sha": commit_sha, "files": files})


def _patch_advisor(monkeypatch, patcher, session):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)
    advisor._patcher = patcher
    advisor._session = session
    return advisor


def _patch_turn(edits, detail="fix it"):
    return ai.Turn(decision=[{"type": "cancel_and_patch", "detail": detail, "edits": edits}])


_ONE_EDIT = [{"path": "v.py", "search": "'0.1.5'", "replace": "'0.1.6'"}]


def test_cancel_and_patch_applies_commits_cancels(monkeypatch, tmp_path):
    _write(tmp_path, "v.py", "V = '0.1.5'\n")
    monkeypatch.chdir(tmp_path)
    calls = {}

    def patcher(files, message):
        calls["files"] = files
        calls["message"] = message
        return "abc123def4567890"

    session = _FakeSession()
    advisor = _patch_advisor(monkeypatch, patcher, session)
    state = _state([_job("Version Check", "failure")])
    state.cancelled = False

    advisor._dispatch(state, _patch_turn(_ONE_EDIT, detail="bump version"))

    assert state.cancelled is True
    assert (tmp_path / "v.py").read_text() == "V = '0.1.6'\n"
    assert calls["files"] == ["v.py"]
    assert "AI-Session-Round: rd-1" in calls["message"]
    assert session.edits and session.edits[0]["commit_sha"] == "abc123def4567890"
    assert session.edits[0]["files"] == ["v.py"]


def test_cancel_and_patch_no_patcher_is_advisory(monkeypatch, tmp_path):
    _write(tmp_path, "v.py", "V = '0.1.5'\n")
    monkeypatch.chdir(tmp_path)
    advisor = _patch_advisor(monkeypatch, None, _FakeSession())
    state = _state([_job("V", "failure")])
    state.cancelled = False
    advisor._dispatch(state, _patch_turn(_ONE_EDIT))
    assert state.cancelled is False
    assert (tmp_path / "v.py").read_text() == "V = '0.1.5'\n"  # untouched


def test_cancel_and_patch_budget_blocks(monkeypatch, tmp_path):
    _write(tmp_path, "v.py", "V = '0.1.5'\n")
    monkeypatch.chdir(tmp_path)
    called = {"n": 0}

    def patcher(files, message):
        called["n"] += 1
        return "x"

    advisor = _patch_advisor(monkeypatch, patcher, _FakeSession(can_continue=(False, "cap")))
    state = _state([_job("V", "failure")])
    state.cancelled = False
    advisor._dispatch(state, _patch_turn(_ONE_EDIT))
    assert state.cancelled is False and called["n"] == 0
    assert (tmp_path / "v.py").read_text() == "V = '0.1.5'\n"  # not even applied


def test_cancel_and_patch_bad_edit_advisory(monkeypatch, tmp_path):
    _write(tmp_path, "v.py", "V = '0.1.5'\n")
    monkeypatch.chdir(tmp_path)
    called = {"n": 0}

    def patcher(files, message):
        called["n"] += 1
        return "x"

    advisor = _patch_advisor(monkeypatch, patcher, _FakeSession())
    state = _state([_job("V", "failure")])
    state.cancelled = False
    advisor._dispatch(state, _patch_turn([{"path": "v.py", "search": "NOPE", "replace": "x"}]))
    assert state.cancelled is False and called["n"] == 0


def test_cancel_and_patch_push_noop_not_cancelled(monkeypatch, tmp_path):
    _write(tmp_path, "v.py", "V = '0.1.5'\n")
    monkeypatch.chdir(tmp_path)
    session = _FakeSession()
    advisor = _patch_advisor(monkeypatch, lambda f, m: "", session)
    state = _state([_job("V", "failure")])
    state.cancelled = False
    advisor._dispatch(state, _patch_turn(_ONE_EDIT))
    assert state.cancelled is False
    assert session.edits == []  # nothing recorded when nothing pushed


def test_cancel_and_patch_push_error_advisory(monkeypatch, tmp_path):
    _write(tmp_path, "v.py", "V = '0.1.5'\n")
    monkeypatch.chdir(tmp_path)

    def patcher(files, message):
        raise RuntimeError("non-fast-forward push rejected")

    session = _FakeSession()
    advisor = _patch_advisor(monkeypatch, patcher, session)
    state = _state([_job("V", "failure")])
    state.cancelled = False
    advisor._dispatch(state, _patch_turn(_ONE_EDIT))
    assert state.cancelled is False and session.edits == []


def test_cancel_and_patch_without_session_still_pushes(monkeypatch, tmp_path):
    _write(tmp_path, "v.py", "V = '0.1.5'\n")
    monkeypatch.chdir(tmp_path)
    advisor = _patch_advisor(monkeypatch, lambda f, m: "sha999", None)
    state = _state([_job("V", "failure")])
    state.cancelled = False
    advisor._dispatch(state, _patch_turn(_ONE_EDIT))
    assert state.cancelled is True
    assert (tmp_path / "v.py").read_text() == "V = '0.1.6'\n"


# ---------------------------------------------- gh token resolution (patcher)


def test_resolve_gh_token_from_provider_and_string():
    from praktika.orchestrator import _resolve_gh_token

    class _Provider:
        def get(self):
            return "ghs_fresh_token"

    # CI passes a GHTokenProvider (has .get()); a raw string passes through.
    assert _resolve_gh_token(_Provider()) == "ghs_fresh_token"
    assert _resolve_gh_token("ghs_raw") == "ghs_raw"


def test_make_ai_patcher_none_when_inputs_missing():
    from praktika.orchestrator import _make_ai_patcher

    assert _make_ai_patcher("", "ref", "sha", "tok") is None
    assert _make_ai_patcher("repo", "", "sha", "tok") is None
    assert _make_ai_patcher("repo", "ref", "sha", None) is None
    assert callable(_make_ai_patcher("repo", "ref", "sha", "tok"))


# ---------------------------------------------- provider consult + check tracking


def test_consult_tracks_observation_turn_and_updates_check():
    calls = []
    p = MockProvider(model="m")
    p.attach_check_updater(lambda status, summary: calls.append((status, summary)))
    obs = Observation(changed=[{"name": "Build", "status": "failure"}], summary="s")

    turn = p.consult("job_failure", obs)

    assert turn is not None
    assert len(p.observations) == 1 and len(p.turns) == 1
    # check went in-progress (observation sent) then neutral (turn received)
    assert [s for s, _ in calls] == ["in_progress", "neutral"]
    # the neutral table carries the triggering event + the decision
    summary = calls[-1][1]
    assert "Build: failure" in summary
    assert "propose_fix" in summary


def test_consult_noop_hook_skips_tracking_and_check():
    calls = []
    p = MockProvider(model="m")
    p.attach_check_updater(lambda s, x: calls.append(s))
    # on_job_success is the inherited no-op -> nothing sent to the model
    assert p.consult("job_success", Observation(changed=[{"name": "A", "status": "success"}])) is None
    assert calls == []
    assert p.observations == [] and p.turns == []


def test_consult_exception_becomes_error_turn_and_closes_check():
    calls = []
    p = MockProvider(model="m")
    p.attach_check_updater(lambda s, x: calls.append(s))
    p.on_job_failure = lambda obs: (_ for _ in ()).throw(RuntimeError("kaboom"))

    turn = p.consult("job_failure", Observation(changed=[{"name": "A", "status": "failure"}]))

    assert turn.error and "kaboom" in turn.error
    assert calls == ["in_progress", "neutral"]  # never left spinning


def test_finalize_check_closes_a_mid_flight_check():
    calls = []
    p = MockProvider(model="m")
    p.attach_check_updater(lambda s, x: calls.append(s))
    p.track_observation(Observation(changed=[{"name": "A", "status": "failure"}]))
    assert calls == ["in_progress"]
    p.finalize_check()
    assert calls == ["in_progress", "neutral"]
    p.finalize_check()  # idempotent — already closed
    assert calls == ["in_progress", "neutral"]


def test_check_updater_errors_never_break_consult():
    def boom(status, summary):
        raise RuntimeError("check api down")

    p = MockProvider(model="m")
    p.attach_check_updater(boom)
    # A failing check update must not stop the turn from being produced.
    turn = p.consult("job_failure", Observation(changed=[{"name": "A", "status": "failure"}]))
    assert turn is not None and turn.error is None
