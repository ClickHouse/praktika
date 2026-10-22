# GitHub App rate limits & CI check traffic

Praktika authenticates to GitHub as a **GitHub App installation** and reports CI
progress by creating and updating GitHub **check-runs** and **commit statuses**.
On a large CI — hundreds of concurrent PRs — this traffic can exhaust the App
installation's API rate limit, at which point GitHub returns `403`/`429` and
check updates start failing. This doc explains the limits, where praktika spends
API calls, and the two knobs that pace that spend.

## The limits (per App installation)

All orchestrators, lambdas and runners of one project authenticate as the **same
GitHub App installation**, so they draw from **one shared rate-limit bucket** —
not a per-run or per-repo budget.

**Primary (hourly):**

| Installation on… | Limit |
|---|---|
| Enterprise Cloud | 15,000 requests/hour |
| Non-Enterprise org | 5,000/hour base, +50/hr per repo and per user over 20, capped at 12,500/hour |

**Secondary (burst), the limit usually hit first:**

- **900 points/minute** for REST. A `GET`/`HEAD`/`OPTIONS` costs **1 point**; a
  **`POST`/`PATCH`/`PUT`/`DELETE` costs 5 points**. Every check create/update is
  mutative, so the effective ceiling is **~180 check writes/minute** across the
  whole installation.
- No more than **100 concurrent requests**.
- No more than **80 content-generating requests/minute and 500/hour** (whether
  check-runs count toward this is ambiguous; don't rely on it either way).

Because check writes are mutative (5 points), the **900 points/min secondary
limit is almost always what trips first**, long before the hourly cap.

## Where praktika spends check/status calls

There are four layers, each with its own client (see the orchestrator and
`praktika/gh.py`):

| Layer | Endpoint | Frequency |
|---|---|---|
| **Top-level workflow check** (`orchestrator/check_run.py` `CheckRun`) | `PATCH /check-runs/{id}` | re-rendered every `WAIT_POLL_INTERVAL_S` (10s) for the whole run — **the dominant cost** |
| **Per-job check** (`orchestrator/state.py` `JobCheckRun`) | `POST`/`PATCH /check-runs` | edge-triggered: ~2 calls/job (queue + complete), up to ~5 with running/stall/retry transitions |
| **Commit statuses** (`gh.py`, GH-Actions / custom engine path) | `POST /statuses/{sha}` | per-job and per-workflow, opt-in |
| **Gate / early checks** (controller + `lambda_gh_trigger.py`) | `POST`/`PATCH /check-runs` | once per workflow event / approval action |

The top-level check is the multiplier: a 10s poll held for a multi-hour run is
~360 PATCHes/hour **per concurrent workflow**, all against one bucket.

### Worked example

300 concurrent PRs, 200 jobs each, ~4h runtime, one installation:

- Top check: `6/min × 300 = 1,800/min ≈ 108,000/hour` (sustained).
- Per-job: `300 × 200 × ~2.5 ≈ 150,000` spread over the run.
- Total ≈ **~140,000 req/hour ≈ ~12,000 points/min** — roughly **9× the hourly
  primary limit and ~13× the 900 points/min secondary limit**.

## The two pacing knobs

Both are project-wide `Settings` (in `ci/settings/settings.py`), scaffolded by
`praktika init`. Defaults preserve historical behavior; raise them on large CI.

### `TOP_CHECK_MIN_PATCH_INTERVAL_S` (default `20`)

Minimum seconds between two PATCHes of the top-level workflow check.
Independently of this interval, the check is **only PATCHed when its rendered
body actually changed** (summary + text + details URL) — this change-detection
is the dominant saving, since a settling 200-job workflow's snapshot changes
only a few dozen times, not every 10s. The interval then coalesces whatever
remains: at most one PATCH per interval per run.

- One-shot transitions (startup phases, the terminal `complete()`) bypass the
  throttle and always land promptly, so progress and the final conclusion are
  never delayed.
- A change that goes quiet still flushes on the first poll after the interval —
  nothing is stranded at a stale body.
- On very large CI, raise to e.g. `180`+ to further cut top-check volume.

### `JOB_CHECKS_MODE` (default `"verbose"`)

Per-job check-run verbosity:

- **`"verbose"`** — every job gets a check-run driven through its full lifecycle
  (`queued → in_progress → success / skipped / failure`). ~2–5 calls/job.
- **`"minimal"`** — a per-job check-run is posted **only for jobs that end in
  failure** (a single, already-completed check). Passing, skipped, queued and
  in-progress jobs emit **no** per-job check-run. A passing job costs **0**
  calls; a failing job costs **1**.

In `"minimal"` the full per-job table is still shown in the **top-level workflow
check's summary** and in the CI report — only the individual per-job rows in the
PR's checks list are suppressed. This is the biggest lever for installations
where per-job checks dominate.

### Example effect

Same 300×200×4h run, with `JOB_CHECKS_MODE="minimal"`, `TOP_CHECK_MIN_PATCH_INTERVAL_S=180`,
and ~10% job failures:

- Top check: change-gated, ≤ `20/hour` per run → ≤ `6,000/hour` total.
- Per-job: `300 × 200 × 10% × 1 ≈ 6,000` over the run.
- Total drops to **~1/20th** of the verbose/10s baseline — from hopelessly over
  the limit to within it.

## Residual notes

- Even tuned, a large enough fleet or a failure spike can approach the limit;
  the remaining spend scales with failure count (minimal mode) and run count.
- The check-posting clients do **not** currently honor `Retry-After` /
  `x-ratelimit-reset`; a `429` is swallowed (top check, per-job check) or retried
  blindly (the `gh` CLI path). Adding backoff is a worthwhile follow-up.
- If you hit the `500/hour` content-generating limit specifically, no
  per-installation tuning helps — that requires sharding across multiple App
  installations. Confirm against a real `429`'s `x-ratelimit-resource` header
  before assuming check-runs are counted there.
