"""OpenAI-model-on-Bedrock provider (one-shot ``complete`` only).

Serves an OpenAI model (e.g. ``openai.gpt-oss-120b-1:0``) hosted on Amazon
Bedrock through the **Converse** API via ``boto3`` — no ``openai`` SDK and no
``ANTHROPIC_API_KEY``; auth is the standard AWS credential chain (env / shared
profile / instance role). ``boto3`` is a core Praktika dependency, so this
provider needs no extra install.

It implements the generic ``complete(system, user_content, tools, tool_executor)``
seam (the entry point ``praktika review`` uses) and runs its own tool-use loop
against Converse's ``toolUse`` / ``toolResult`` content blocks. It does **not**
implement the orchestrator lifecycle hooks — ``on_job_failure`` etc. stay the
inherited no-ops — so it is a review/standalone provider, not an advisor.

Region resolution mirrors the Anthropic ``BedrockProvider``: explicit
``aws_region`` arg → ``Settings.AWS_REGION`` → ``AWS_REGION`` /
``AWS_DEFAULT_REGION`` env. Bedrock Runtime has no region fallback, so a region
must be resolvable or ``complete`` raises (→ the caller's error handling).
"""
import os
import time

from .provider import AIProvider, Turn, Usage

# Per-1M-token (input, output) USD pricing, matched by longest substring in the
# model id (tolerant of version suffixes). Unknown ids price at zero so cost
# accounting degrades gracefully rather than guessing.
_PRICING = {
    "gpt-oss-120b": (0.15, 0.60),
    "gpt-oss-20b": (0.07, 0.30),
}

# Stop runaway tool loops: at most this many tool rounds before a final answer
# is forced from whatever the model has seen. Mirrors the Anthropic provider.
_MAX_TOOL_ROUNDS = 8


def _price_per_mtok(model):
    for key in sorted(_PRICING, key=len, reverse=True):
        if key in (model or ""):
            return _PRICING[key]
    return (0.0, 0.0)


def _to_tool_config(tools):
    """Translate neutral tool dicts (name/description/input_schema — the shape
    the Anthropic provider and the review job already build) into a Converse
    ``toolConfig``. Returns None when there are no tools."""
    if not tools:
        return None
    specs = []
    for t in tools:
        specs.append(
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "inputSchema": {"json": t.get("input_schema", {"type": "object"})},
                }
            }
        )
    return {"tools": specs}


class BedrockOpenAIProvider(AIProvider):
    name = "bedrock-openai"
    DEFAULT_MODEL = "openai.gpt-oss-120b-1:0"

    def __init__(self, model="", aws_region=""):
        super().__init__(model=model)
        self.aws_region = aws_region or ""
        self._client = None  # lazily constructed on first complete()

    def _region(self):
        if self.aws_region:
            return self.aws_region
        from praktika.settings import Settings

        return (
            getattr(Settings, "AWS_REGION", "")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or ""
        )

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as e:  # boto3 is a core dep; guard anyway
                raise RuntimeError(
                    "boto3 not installed; required for AI_PROVIDER='bedrock-openai'"
                ) from e
            region = self._region()
            if not region:
                raise RuntimeError(
                    "no AWS region for Bedrock; set Settings.AWS_REGION or AWS_REGION"
                )
            self._client = boto3.client("bedrock-runtime", region_name=region)
        return self._client

    def complete(
        self,
        system,
        user_content,
        tools=None,
        tool_executor=None,
        max_tokens=4000,
    ) -> Turn:
        client = self._get_client()
        model = self.resolved_model()
        tool_config = _to_tool_config(tools)
        messages = [{"role": "user", "content": [{"text": user_content}]}]
        totals = {"input": 0, "output": 0}
        tool_calls = 0

        t0 = time.time()
        text = ""
        for _ in range(_MAX_TOOL_ROUNDS + 1):
            kwargs = dict(
                modelId=model,
                system=[{"text": system}],
                messages=messages,
                inferenceConfig={"maxTokens": max_tokens},
            )
            if tool_config:
                kwargs["toolConfig"] = tool_config
            resp = client.converse(**kwargs)

            usage = resp.get("usage") or {}
            totals["input"] += usage.get("inputTokens", 0) or 0
            totals["output"] += usage.get("outputTokens", 0) or 0

            message = (resp.get("output") or {}).get("message") or {}
            content = message.get("content") or []
            text = next(
                (b["text"] for b in content if "text" in b),
                text,
            )
            tool_uses = [b["toolUse"] for b in content if "toolUse" in b]
            if not tool_uses:
                break

            # Echo the assistant turn back, then answer each tool call.
            messages.append({"role": "assistant", "content": content})
            results = []
            for tu in tool_uses:
                tool_calls += 1
                out = (
                    tool_executor(tu["name"], tu.get("input") or {})
                    if tool_executor is not None
                    else f"error: no tool executor for {tu['name']!r}"
                )
                results.append(
                    {
                        "toolResult": {
                            "toolUseId": tu["toolUseId"],
                            "content": [{"text": out}],
                        }
                    }
                )
            messages.append({"role": "user", "content": results})
        latency_ms = int((time.time() - t0) * 1000)

        usage = self._usage(model, totals, latency_ms)
        print(
            f"[AI {self.name}] complete: model={model} tool_calls={tool_calls} "
            f"tokens={usage.input_tokens}/{usage.output_tokens} "
            f"cost=${usage.cost_usd:.4f}"
        )
        return Turn(reasoning=text, usage=usage)

    def _usage(self, model, totals, latency_ms) -> Usage:
        inp = totals["input"]
        out = totals["output"]
        in_price, out_price = _price_per_mtok(model)
        cost = (inp * in_price + out * out_price) / 1_000_000
        return Usage(
            input_tokens=inp,
            output_tokens=out,
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
            provider=self.name,
            model=model,
        )
