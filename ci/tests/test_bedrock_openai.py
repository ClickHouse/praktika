"""Unit tests for the bedrock-openai provider's two-phase structured output,
using a fake bedrock-runtime client (no network / boto3 calls)."""
import json

from praktika.orchestrator.ai.bedrock_openai import BedrockOpenAIProvider


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


_SCHEMA = {
    "type": "object",
    "properties": {"summary_md": {"type": "string"}},
    "required": ["summary_md"],
}


def _make_provider(responses):
    p = BedrockOpenAIProvider(model="openai.gpt-oss-120b-1:0", aws_region="eu-north-1")
    p._client = _FakeClient(responses)
    return p


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
