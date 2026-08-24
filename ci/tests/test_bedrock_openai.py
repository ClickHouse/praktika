"""Unit tests for the bedrock-openai provider's two-phase structured output,
using a fake bedrock-runtime client (no network / boto3 calls)."""
import json

from praktika.orchestrator.ai.bedrock_openai import (
    _MAX_TOOL_ROUNDS,
    _STOP_AND_WRITE_UP,
    BedrockOpenAIProvider,
)


class _FakeClient:
    """Returns queued converse responses and records each request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        resp = self._responses.pop(0)
        return resp


def _msg(*blocks):
    return {"output": {"message": {"content": list(blocks)}}, "usage": {"inputTokens": 1, "outputTokens": 1}}


def _tool_msg(uid="t"):
    return _msg({"toolUse": {"toolUseId": uid, "name": "grep_repo", "input": {}}})


_SCHEMA = {
    "type": "object",
    "properties": {"summary_md": {"type": "string"}},
    "required": ["summary_md"],
}


def _make_provider(responses):
    p = BedrockOpenAIProvider(model="openai.gpt-oss-120b-1:0", aws_region="eu-north-1")
    p._client = _FakeClient(responses)
    return p


def test_reasoning_fields_shape_per_model_family():
    # gpt-5.x: nested reasoning.effort, supports xhigh.
    p5 = BedrockOpenAIProvider(model="global.openai.gpt-5.6-sol")
    assert p5._reasoning_fields("xhigh") == {"reasoning": {"effort": "xhigh"}}
    # gpt-oss: flat reasoning_effort, xhigh clamped to high.
    poss = BedrockOpenAIProvider(model="openai.gpt-oss-120b-1:0")
    assert poss._reasoning_fields("xhigh") == {"reasoning_effort": "high"}
    assert poss._reasoning_fields("medium") == {"reasoning_effort": "medium"}


def test_two_phase_structured_output_returns_submit_json():
    result = {"summary_md": "looks good", "inline_findings": [], "thread_actions": []}
    p = _make_provider(
        [
            # phase 1, round 1: reasoning + a tool call
            _msg(
                {"reasoningContent": {"reasoningText": {"text": "let me look"}}},
                {"toolUse": {"toolUseId": "t1", "name": "grep_repo", "input": {"pattern": "x"}}},
            ),
            # phase 1, round 2: model stops with free-text findings
            _msg({"text": "Finding: foo.py:1 is wrong"}),
            # phase 2: forced submit tool call carries the structured result
            _msg({"toolUse": {"toolUseId": "s1", "name": "submit_result", "input": result}}),
        ]
    )
    calls = []
    turn = p.complete(
        system="sys",
        user_content="review this",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: calls.append((name, inp)) or "tool output",
        response_schema=_SCHEMA,
    )

    assert calls == [("grep_repo", {"pattern": "x"})]
    assert json.loads(turn.reasoning) == result


def test_echoed_assistant_turn_strips_reasoning_content():
    p = _make_provider(
        [
            _msg(
                {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
                {"toolUse": {"toolUseId": "t1", "name": "grep_repo", "input": {}}},
            ),
            _msg({"text": "done"}),
            _msg({"toolUse": {"toolUseId": "s1", "name": "submit_result", "input": {"summary_md": "ok"}}}),
        ]
    )
    p.complete(
        system="sys",
        user_content="go",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: "out",
        response_schema=_SCHEMA,
    )

    # The 2nd investigation request echoes the assistant tool-use turn back;
    # it must not contain any reasoningContent block.
    second_request = p._client.requests[1]
    assistant_turns = [m for m in second_request["messages"] if m["role"] == "assistant"]
    assert assistant_turns, "expected an echoed assistant turn"
    for turn in assistant_turns:
        assert all("reasoningContent" not in b for b in turn["content"])


def test_phase2_forces_submit_toolchoice():
    p = _make_provider(
        [
            _msg({"text": "no issues found"}),  # phase 1: immediate free-text answer
            _msg({"toolUse": {"toolUseId": "s1", "name": "submit_result", "input": {"summary_md": "clean"}}}),
        ]
    )
    p.complete(
        system="sys",
        user_content="go",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: "out",
        response_schema=_SCHEMA,
    )
    # The last request is the structuring call: it must force the submit tool.
    structuring = p._client.requests[-1]
    assert structuring["toolConfig"]["toolChoice"] == {"tool": {"name": "submit_result"}}
    assert [t["toolSpec"]["name"] for t in structuring["toolConfig"]["tools"]] == ["submit_result"]


def test_interim_text_survives_when_model_stops_without_text():
    # Round 1 emits a text block alongside a tool call; round 2 stops with only
    # reasoning (no text). The interim write-up must survive into phase 2.
    p = _make_provider(
        [
            _msg(
                {"text": "partial finding: foo.py:1"},
                {"toolUse": {"toolUseId": "t1", "name": "grep_repo", "input": {}}},
            ),
            _msg({"reasoningContent": {"reasoningText": {"text": "done thinking"}}}),
            _msg({"toolUse": {"toolUseId": "s1", "name": "submit_result", "input": {"summary_md": "ok"}}}),
        ]
    )
    turn = p.complete(
        system="sys",
        user_content="go",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: "out",
        response_schema=_SCHEMA,
    )
    # Phase 2's structuring prompt carries the interim text verbatim.
    structuring_user = p._client.requests[-1]["messages"][0]["content"][0]["text"]
    assert "partial finding: foo.py:1" in structuring_user
    assert turn.error is None


def test_per_round_budget_note_in_system():
    # Each investigation turn tells the model where it is in its tool budget.
    p = _make_provider(
        [
            _tool_msg("t1"),
            _msg({"text": "done"}),
            _msg({"toolUse": {"toolUseId": "s1", "name": "submit_result", "input": {"summary_md": "ok"}}}),
        ]
    )
    turn = p.complete(
        system="sys",
        user_content="go",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: "out",
        response_schema=_SCHEMA,
    )
    first_system = p._client.requests[0]["system"][0]["text"]
    assert f"round 1 of {_MAX_TOOL_ROUNDS + 1}" in first_system
    # One tool-issuing round ran before the model stopped with a clean answer;
    # the terminating text round is not counted and the budget is not exhausted.
    assert turn.usage.tool_rounds == 1
    assert turn.usage.max_tool_rounds == _MAX_TOOL_ROUNDS + 1
    assert turn.usage.tool_calls == 1
    assert turn.usage.exhausted is False


def test_exhausted_budget_forces_write_up_with_stop_directive():
    # Every round calls a tool up to the cap, then the forced fallback writes up.
    responses = [_tool_msg(f"t{i}") for i in range(_MAX_TOOL_ROUNDS + 1)]
    responses.append(_msg({"text": "Finding: bar.py:2 leaks"}))  # forced write-up
    responses.append(
        _msg({"toolUse": {"toolUseId": "s1", "name": "submit_result", "input": {"summary_md": "found it"}}})
    )
    p = _make_provider(responses)
    turn = p.complete(
        system="sys",
        user_content="go",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: "out",
        response_schema=_SCHEMA,
    )
    # The write-up request (right before phase 2) appends the stop directive.
    write_up = p._client.requests[-2]
    assert write_up["system"][0]["text"].endswith(_STOP_AND_WRITE_UP)
    assert json.loads(turn.reasoning) == {"summary_md": "found it"}
    assert turn.error is None
    # Round accounting reflects the exhausted budget.
    assert turn.usage.tool_rounds == _MAX_TOOL_ROUNDS + 1
    assert turn.usage.max_tool_rounds == _MAX_TOOL_ROUNDS + 1
    assert turn.usage.exhausted is True


def test_interim_text_accumulates_across_rounds():
    # Round 1 emits a real finding + tool call; round 2 emits chatter + tool
    # call; the model then stops with no text. Both interim texts must reach
    # phase 2 - an early finding is not overwritten by later progress chatter.
    p = _make_provider(
        [
            _msg(
                {"text": "Finding A: foo.py is broken"},
                {"toolUse": {"toolUseId": "t1", "name": "grep_repo", "input": {}}},
            ),
            _msg(
                {"text": "now checking bar.py"},
                {"toolUse": {"toolUseId": "t2", "name": "grep_repo", "input": {}}},
            ),
            _msg({"reasoningContent": {"reasoningText": {"text": "hmm"}}}),
            _msg({"toolUse": {"toolUseId": "s1", "name": "submit_result", "input": {"summary_md": "ok"}}}),
        ]
    )
    p.complete(
        system="sys",
        user_content="go",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: "out",
        response_schema=_SCHEMA,
    )
    structuring_user = p._client.requests[-1]["messages"][0]["content"][0]["text"]
    assert "Finding A: foo.py is broken" in structuring_user
    assert "now checking bar.py" in structuring_user


def test_blank_write_up_aborts_before_structuring():
    # The model burns the whole budget on tools and the forced write-up still
    # returns no text: complete() must abort with an error and never structure.
    responses = [_tool_msg(f"t{i}") for i in range(_MAX_TOOL_ROUNDS + 1)]
    responses.append(_msg({"reasoningContent": {"reasoningText": {"text": "still thinking"}}}))
    p = _make_provider(responses)
    turn = p.complete(
        system="sys",
        user_content="go",
        tools=[{"name": "grep_repo", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=lambda name, inp: "out",
        response_schema=_SCHEMA,
    )
    assert turn.reasoning == ""
    assert turn.error == "model produced no review text after investigation"
    # No structuring call was made (the queue was not drained past the write-up).
    assert not p._client._responses  # all consumed; no extra phase-2 pop
    assert len(p._client.requests) == _MAX_TOOL_ROUNDS + 2  # cap rounds + write-up
