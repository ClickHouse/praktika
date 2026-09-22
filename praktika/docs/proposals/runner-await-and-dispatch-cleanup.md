# Runner-await symmetry and orphaned-dispatch cleanup

Two liveness gaps that surface once the runner pool runs at max size and jobs
back up in the SQS queue (high CI load). Both stem from the same root: the
`job_task` SQS message lifecycle is not fully coupled to the orchestrator's
per-job state, so recovery timing is asymmetric (gap 1) and giving up leaves an
orphaned message behind (gap 2).

Background: the job lifecycle has two phases — **phase 1, "awaiting a runner"**
(`QUEUED`, bounded by `RUNNER_PICKUP_TIMEOUT_S`, `settings.py`) and **phase 2,
the multi-phase heartbeat** (`RUNNING`: `picked_up → cloning → resolving_runtime
→ writing_task → running_job`, bounded by `HEARTBEAT_STALL_S` /
`HEARTBEAT_TIMEOUT_S`). The orchestrator applies both rules in `sweep_liveness`
(`orchestrator/state.py`); the runner-side controller writes `heartbeat.json`
and drives the phases (baked `praktika-controller`, `Heartbeat` /
`VisibilityHeartbeat` in `common.py`). Queue config: `visibility_timeout=600`,
`message_retention=86400` (24h), DLQ `maxReceiveCount=3` (`sqs_queue.py`).

---

## Gap 1: dead-runner redelivery must wait the same as a never-picked-up job

**Problem.** A job that never got a runner waits the full
`RUNNER_PICKUP_TIMEOUT_S` (phase 1) before `fail_dead`. But a job whose runner
died **mid-job** is recovered by SQS redelivery, and the orchestrator declares it
dead at `HEARTBEAT_TIMEOUT_S` (currently 900s) — a threshold sized against the
queue's `visibility_timeout` (600s), **not** against how deep the queue is. Under
a saturated pool the redelivered `job_task` goes to the back of the backlog, so
re-pickup by a fresh runner can take far longer than 900s, and the job is wrongly
declared dead even though it is fully recoverable.

This is an asymmetry: the redelivered task faces the *exact same queue* as a
fresh dispatch, yet a never-started job is given (e.g.) 1h to find a runner while
a job that lost its runner is given only 15min. Under load that is backwards.

The signal to act on is clean. The S3 heartbeat goes stale only if the whole
controller/instance died (a live controller keeps writing `phase=running_job`
every `HEARTBEAT_INTERVAL_S` even while the job subprocess is busy or hung; a
hung-but-alive runner does **not** go stale). And if the controller died, its
`VisibilityHeartbeat` thread died too, so the message becomes visible again after
`visibility_timeout` and is redelivered. Therefore **S3 heartbeat stale ⟹
controller dead ⟹ the message will be (or already was) redelivered** — so
extending the wait for re-pickup is safe, not a way to sit on a genuinely stuck
job.

**Status.** Open. `sweep_liveness` hard-fails a `RUNNING` job at
`HEARTBEAT_TIMEOUT_S` regardless of pool saturation. The two-stage `RUNNING` path
(`HEARTBEAT_STALL_S` flags "unresponsive, retry pending"; `HEARTBEAT_TIMEOUT_S`
kills) already exists but the kill threshold is queue-depth-blind.

**Direction to consider.** Make the dead-runner wait symmetric with the
never-picked-up wait. Once a `RUNNING` job's heartbeat is stale past a "runner
presumed dead" threshold (a small multiple of `HEARTBEAT_INTERVAL_S` is enough —
the controller is confirmed gone), reclassify the job as **awaiting re-pickup**
and restart the phase-1 clock: give it up to `RUNNER_PICKUP_TIMEOUT_S` from the
moment of loss for a fresh runner to bump `attempt`
(SQS `ApproximateReceiveCount`) and re-heartbeat. Only declare it truly dead
when:

1. that await budget elapses with no fresh `attempt` (redelivery isn't
   producing a new runner), or
2. redelivery has stopped because the message hit the DLQ — i.e. `attempt`
   reached `maxReceiveCount`.

Caveats: the effective ceiling is `maxReceiveCount × per-attempt work`, and the
total wait must stay under `MessageRetentionPeriod` (24h) so the message still
exists to be redelivered. Surface the awaiting-re-pickup state on the check the
same way phase 1 does, so a long wait is visible rather than silent.

---

## Gap 2: clean up the orphaned message on a missed pickup

**Problem.** When the orchestrator gives up on a `QUEUED` job at
`RUNNER_PICKUP_TIMEOUT_S` and `fail_dead`s it, the `job_task` SQS message is
still sitting in the queue — never received, never deleted. Two bad outcomes
once a runner later frees up or the ASG scales out:

1. A runner picks up the orphaned message and runs an **already-failed job**:
   wasted compute, and it writes `heartbeat.json` / `final.json` for a job the
   orchestrator considers terminal. The controller's pre-clone guards catch only
   the *run*-level cases — cancel key present, or `state.json.finalized`
   (`controller.py`) — not a *single* per-job dead state within a run that is
   still progressing, so it does the full pointless clone-and-run.
2. Even when harmless, it burns a runner slot under exactly the high-load
   conditions we are trying to serve.

The message otherwise lingers until `MessageRetentionPeriod` (24h), so the
window for a stale late pickup is long.

**Status.** Open. `fail_dead` mutates only orchestrator state; nothing reconciles
the still-queued message, and the controller has no per-job abandoned guard.

**Direction to consider.** Two options; prefer (b).

- **(a) Delete the un-delivered message.** Not feasible directly: SQS has no API
  to delete a specific message you have not received (no receipt handle), and
  there is no content-addressed delete. Purging the whole queue would kill other
  pending jobs. So the orchestrator cannot target the orphan from its side.

- **(b) Quick-exit on late delivery, without processing.** Add a per-job
  terminal signal the controller checks in its pre-clone guard, alongside the
  existing cancel + finalized checks. The orchestrator already writes
  `state.json` every loop with each job's status, and the controller already
  reads `state.json` for the finalized guard — so extend that guard: if this
  job's status in `state.json` is terminal (dead/abandoned/failed), the runner
  **acks (deletes) the message immediately and exits** before cloning. This both
  removes the orphan (delete on receipt) and avoids the wasted clone-and-run. It
  is the fail-close path: the runner does no consequential work on a job the
  orchestrator has already given up on.

Both gaps are the same underlying fix in two places: couple the SQS message
lifecycle to the orchestrator's per-job state — so a lost runner's message is
awaited as long as a fresh dispatch (gap 1), and an abandoned job's message is
discarded rather than run late (gap 2).
