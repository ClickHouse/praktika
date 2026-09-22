# Deduplicate triggering events: fail hard on a double-triggered run

**Goal.** Make it impossible for a single logical GitHub event to mint two
concurrent workflow runs — and when a duplicate slips through, **fail the run
loudly** instead of letting two runs proceed invisibly. Today nothing on the
trigger path guards against a second identical delivery: each accepted message
mints a fresh `run_id` and runs to completion, so two runs for the same
(`repo`, `type`, `head_sha`) coexist with no error anywhere.

## Problem

The trigger path is at-least-once with a non-deterministic run identity:

- GitHub → Lambda (`infrastructure/native/lambda_gh_trigger.py`) → **standard**
  (non-FIFO) SQS `praktika_clickhouse_workflows` → baked `praktika-controller`
  poller → `orchestrator/__init__.py::_orchestrate_event`.
- The queue has no dedup: `infrastructure/sqs_queue.py:52` creates it with only
  `VisibilityTimeout` / `MessageRetentionPeriod` / a DLQ redrive policy — no
  `.fifo` name, no `ContentBasedDeduplication`, no `MessageDeduplicationId`.
- Each accepted message mints a **fresh `run_id` = GitHub check-run id**, which
  is server-assigned and therefore non-deterministic:

  ```python
  # orchestrator/__init__.py:506
  check = CheckRun.start(gh_token, repo, head_sha, workflow.name, …)
  run_id = str(check.id) if check is not None else None   # :512
  ```

  `CheckRun.start` POSTs `/repos/{repo}/check-runs` and returns the
  server-assigned `data["id"]` (`orchestrator/check_run.py:30-53`).

So two identical deliveries → two SQS messages → two `_orchestrate_event` calls
→ two `CheckRun.start` POSTs → **two different `run_id`s → two fully independent
runs** of the same workflow on the same commit. All S3 state is keyed by
`run_id` (`runs/<run_id>/…`, `LIFECYCLE.md:40`), so the two runs never touch a
shared key and never notice each other. `PROTOCOL.md:69` already documents the
adjacent case for re-runs: *"Repeated full re-runs are not deduplicated … each
run's checks carry a different `run_id` … so GitHub does not collapse them."*

The `X-GitHub-Delivery` UUID *is* captured at the entry point
(`lambda_gh_trigger.py:1232`) but only used as an SQS `MessageAttribute`
(`_enqueue`, `:742`) and as a rerun-request filename. It is **not** put in the
message body and **never read on the consume side** (grep for `delivery_id`
outside the Lambda returns nothing).

### Observed incident

After migrating to a new GitHub App while the **legacy App was still installed
with its webhook configured**, both Apps delivered the same underlying GitHub
event. Two webhooks arrived, two SQS messages were enqueued, two orchestrators
started **two workflow runs at the same time for the same event**.

This was completely invisible — GitHub showed two check runs but nobody was
watching for that. It surfaced only by accident downstream: two **sibling jobs**
(one per run) wrote the *same* artifact to the *same* S3 key with a slight
delay, changing its `ETag`, and a consumer job in the other run failed its
download because the `ETag` no longer matched what it had resolved. A confusing,
seemingly-random artifact-download failure was the only symptom of a duplicated
run.

**Key subtlety for the fix:** because the duplication came from *two different
App installations*, the two deliveries had **different `X-GitHub-Delivery`
UUIDs**. Deduping on the delivery UUID alone would **not** have caught this.
The dedup key must be **content identity** — `(repo, type, action, head_sha,
pr_number/head_ref)` — not the delivery UUID.

## Why the current code is this way

The trigger path was built for a single, correctly-configured webhook source
where at-least-once redelivery of *the same* message is the only expected
duplication, and even that is rare. Identity is intentionally content-only:
messages carry `type`, `head_sha`, `pr_number`/`head_ref`, `repo`, `sender`
and a wall-clock `event_ts = time.time()` (`lambda_gh_trigger.py:1228`), with no
event id in the body. The only staleness guard is head-advancement: the PR path
refetches the live PR and drops events where `head_sha != current_sha`
(`:1385-1390`) — which rejects *outdated* events but happily accepts a *second
identical* one.

Exactly-once primitives already exist in the codebase, just not on the initial
trigger: the finished-run resume path claims a write-once S3 lock
`runs/<run_id>/resume.lock` via `put_object(..., IfNoneMatch="*")`
(`_claim_resume_lock`, `lambda_gh_trigger.py:227-258`), and the cron proposal
(`cron_scheduled_workflows.md:66`) reuses the same `PutObject(IfNoneMatch="*")`
pattern for exactly-once firing. There is simply **no event-identity-keyed S3
marker** — every key is scoped by `run_id` or PR number, both minted *after* a
run already exists.

## Direction to consider

Gate on **content identity** with a write-once S3 marker, claimed *before* a run
is minted, and **fail hard** when the claim loses.

1. **Compute a content key** for every trigger, independent of the delivery
   UUID and of `event_ts`:

   ```python
   # e.g. in the Lambda, from the built workflow message
   key_parts = [repo, wf_type, action or "", head_sha, str(pr_number or head_ref)]
   event_key = hashlib.sha256("\x00".join(key_parts).encode()).hexdigest()
   ```

   This collapses both failure modes into one: SQS redelivery of the same
   message *and* two different Apps delivering the same logical event both
   produce the same `event_key`. Put `event_key` in the message body so the
   consume side can see it (delivery UUID stays as the MessageAttribute for
   tracing).

2. **Claim once, in the orchestrator, before `CheckRun.start`.** Write a
   write-once marker keyed by content identity, reusing the existing primitive:

   ```python
   # events/<event_key>.json, IfNoneMatch="*"  — the winner records its run_id
   try:
       s3.put_object(Bucket=..., Key=f"events/{event_key}.json",
                     Body=json.dumps({"event_ts": event_ts}),
                     IfNoneMatch="*")
   except PreconditionFailed:
       existing = load(f"events/{event_key}.json")
       # a run already exists for this exact event → this is a duplicate
   ```

   The winner proceeds and, once it has a `run_id`, records it back into the
   marker (`events/<event_key>.json` → `{run_id, event_ts}`), so the marker
   doubles as an event→run index.

3. **Fail loud on the loser.** The goal is visibility, so a duplicate must *not*
   silently no-op. When the claim loses, the second orchestrator run should:
   - **Not** call `CheckRun.start` (do not mint a second run / second checks).
   - Emit a clearly-worded `ERROR` — CloudWatch log **and**, ideally, a check-run
     message on the *existing* run's report pointing at the duplicate
     (`"Duplicate trigger for <event_key>; already handled by run <run_id>.
     Check for a second webhook source (stale GitHub App / duplicate
     installation)."`). This is the "fail hardly, the run is fine with the
     error" the incident calls for: the real run continues; the duplicate is
     surfaced instead of running in parallel.
   - Delete the duplicate SQS message (it is handled — do not let it redrive to
     the DLQ, which would look like an infra failure).

4. **Bound the marker with a TTL / dedup window**, not forever. A legitimate
   re-trigger of the same commit much later (manual re-run, force-push back to an
   old sha) must still be allowed. Options: an S3 lifecycle rule expiring
   `events/` after N minutes/hours, or storing `event_ts` in the marker and
   treating a claim as lost only when `now - existing.event_ts < WINDOW`. The
   window only needs to cover the near-simultaneous-delivery case the incident
   showed (seconds to minutes), so a short TTL keeps the re-trigger path open.
   The dedicated re-run path already has its own idempotency
   (`runs/<run_id>/resume.lock`, rerun-request-per-delivery) and is unaffected.

### Where dedup should live: Lambda vs. orchestrator

Prefer the **orchestrator**, not the Lambda. Two Apps hit the same Lambda, so a
Lambda-side claim would work for the incident — but the orchestrator is the sole
place that mints `run_id` (`__init__.py:512`) and owns all `runs/<run_id>/`
state, so gating there keeps "one event → at most one run" as a single
invariant at the point of run creation, and covers any future producer that
enqueues to the queue (cron, CLI) without each re-implementing the check. The
Lambda may additionally keep a cheap delivery-UUID guard for pure SQS
redelivery, but it is the content-key claim in the orchestrator that closes the
two-Apps hole.

### Alternative considered: FIFO queue with content-based dedup

A FIFO `praktika_clickhouse_workflows.fifo` with `ContentBasedDeduplication` (or
an explicit `MessageDeduplicationId = event_key`) would dedup *at enqueue* — but
only within SQS's fixed **5-minute** dedup interval, only for messages that hash
identically (so `event_ts` / delivery attributes must be excluded from the
hash), and it silently drops the duplicate — the opposite of the "make it
visible / fail hard" goal. It also reduces throughput and reorders nothing we
need ordered. The write-once S3 marker gives an explicit, inspectable record and
lets us choose the loud-failure behavior, so it is preferred; FIFO could be a
belt-and-suspenders addition but is not the primary fix.

## Caveats

- **Two different `head_sha`.** If the duplicate carries a genuinely different
  commit (e.g. a fast follow-up push), it is *not* a duplicate and must run —
  the content key correctly distinguishes them. This design only collapses
  byte-for-byte-identical logical events.
- **S3 delete/ordering isn't transactional.** The winner records its `run_id`
  into the marker *after* claiming and *after* `CheckRun.start`; a loser that
  reads the marker in that gap sees the claim but no `run_id` yet — it should
  still refuse to mint a second run, and may retry the read briefly to attach
  its error to the right report.
- **Root cause is operational.** The dedup gate is a safety net; the actual
  incident is a misconfiguration (a stale GitHub App still delivering). The
  loud-failure message should name that hypothesis so the operator removes the
  duplicate webhook source, rather than treating the guard as the fix.
