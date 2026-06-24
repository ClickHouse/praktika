"""Tracing and cost accounting for AI advisor turns.

Two concerns, kept deliberately small:

* ``UsageLedger`` accumulates token/cost totals across a run — the cost-tracking
  seam. ``summary()`` prints the run total at the end.
* ``TraceLogger`` records each turn: a compact one-liner to stdout (so it shows
  up live in ``journalctl -fu praktika-controller``) plus a full JSON record
  appended to ``TEMP_DIR/ai/turns.jsonl`` for after-the-fact replay.

No S3 / network here yet (see DESIGN.md roadmap) — local + stdout only.
"""
import json
import os
import time

from praktika.settings import Settings

from .provider import Usage


class UsageLedger:
    """Running totals of AI usage across a workflow run."""

    def __init__(self):
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    def add(self, usage: Usage):
        self.turns += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cost_usd += usage.cost_usd

    def to_dict(self):
        return {
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }


class TraceLogger:
    """Stdout + local-JSONL sink for advisor turns."""

    def __init__(self, run_id, local_mode=False):
        self.run_id = run_id
        self.local_mode = local_mode
        self._turn_no = 0
        self._path = os.path.join(Settings.TEMP_DIR, "ai", "turns.jsonl")
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
        except Exception as e:
            print(f"  [warn] could not create AI trace dir: {type(e).__name__}: {e}")

    def record(self, observation, turn, ledger=None):
        """Print a one-liner and append the full turn record to JSONL.

        Wrapped so a logging failure never takes down the orchestrator loop.
        """
        self._turn_no += 1
        changed = ",".join(
            f"{c.get('name')}:{c.get('status')}" for c in observation.changed
        )
        u = turn.usage
        reasoning = (turn.reasoning or "").replace("\n", " ")[:120]
        err = f" error={turn.error}" if turn.error else ""
        print(
            f"[AI   ] turn={self._turn_no} changed=[{changed}] "
            f"provider={u.provider or '?'} model={u.model or '?'} "
            f"reasoning='{reasoning}' tokens={u.input_tokens}/{u.output_tokens} "
            f"cost=${u.cost_usd:.4f}{err}"
        )

        record = {
            "turn": self._turn_no,
            "ts": self._now(),
            "run_id": self.run_id,
            "changed": observation.changed,
            "observation": observation.to_dict(),
            **turn.to_dict(),
        }
        if ledger is not None:
            record["ledger"] = ledger.to_dict()
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            print(f"  [warn] could not append AI trace: {type(e).__name__}: {e}")

    def summary(self, ledger: UsageLedger):
        print(
            f"[AI   ] run total: turns={ledger.turns} "
            f"tokens={ledger.input_tokens}/{ledger.output_tokens} "
            f"cost=${ledger.cost_usd:.4f}"
        )

    @staticmethod
    def _now():
        # time.time() is allowed here (orchestrator runtime, not a workflow script).
        return time.time()
