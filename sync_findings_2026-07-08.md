# Praktika Sync Findings

Compared local `praktika/` with `../ClickHouse/ci/praktika` for the window `2026-05-08` through `2026-07-08`.

## Recommended And Synced

| Area | Upstream commits | Why it matters here | Status |
|---|---|---|---|
| `Job.Config.allow_failure` / `force_success` | `6ea4d9a5530`, later uses in current upstream | Local tree was missing the flags and still used `allow_merge_on_failure`. This affects API shape and final merge gating. | Synced |
| Digest ignores failure-policy-only config | `6ea4d9a5530` | Flipping `allow_failure` / `force_success` should not invalidate job cache keys. | Synced |
| Runner applies `force_success` | current upstream behavior | `force_success=True` must convert failing job result to green before report / pipeline status / exit handling. | Synced |
| Final workflow gating uses `allow_failure` | current upstream behavior | Ready-for-merge calculation must ignore non-blocking failed jobs. | Synced |
| Secret-safe `data=` output and emit only job-added KV data | `d0f4b91950e`, `c3abd50edc2`, `7c3f0414cd7` | Prevents GitHub Actions from suppressing `data` output when user-authored text matches secret patterns; avoids duplicating inherited KV data in every job output. | Synced |
| Restore masked output in `_Environment.from_workflow_data` | same series as above | Downstream jobs still need decoded `JOB_KV_DATA` plus restored PR body/title and commit message. | Synced |
| Timeout watchdog fix for docker cleanup hangs | `cfb2ac8ff0c` | Local `TeePopen` could hang if timeout cleanup wedged or returned without terminating the child process group. | Synced |
| Scheduled workflow manual inputs in YAML generator | `70d35822aee` from PR `#108355` | Lets `event=SCHEDULE` workflows also declare `workflow_dispatch` inputs and populate the workflow-inputs file on manual runs. | Synced |

## Reviewed But Not Synced

| Area | Upstream commits | Reason not synced |
|---|---|---|
| `CH Inc sync` / `Code Review` status handling | `f9b8664226b`, `38b73a81d7a` | ClickHouse-specific GitHub status flow. |
| Coverage profdata artifact optionality | `58920805e88`, `814773d74ce`, `cf8cb51b83c` | Coverage-workflow-specific, not core Praktika behavior here. |
| macOS `runner-init` / infra bootstrap changes | multiple commits in June-July 2026 | Upstream infrastructure-specific. |
| Auto-regenerate workflow YAML on PRs | `17f36ccfcdc` and follow-ups | Repo workflow-management feature, not a core sync target. |
| Review-thread GraphQL / PR comment rendering polish | `755b70a9b9c`, `563a3c5f342`, `8099a7ece74` | Most of this was already present locally. |
| UI/report polish changes | `fa832eedb58`, `56dfd8da0f6`, `8c323402be4`, `2d61a48a25c` | Useful but non-blocking; mostly presentation. |

## Local Notes

- Kept local `always_run` semantics intact. Upstream renamed some scheduling behavior to `run_unless_cancelled`, but this repo already uses `always_run` across orchestrator code and tests.
- Kept `set_allow_merge_on_failure()` as a compatibility helper that forwards to `set_allow_failure()`.
