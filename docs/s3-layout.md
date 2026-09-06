# S3 layout

The praktika artifact bucket (`Settings.S3_ARTIFACT_BUCKET`, e.g.
`praktika-artifacts-eu-north-1`) holds CI artifacts, reports, run state, the CI
cache, published wheels, and the report viewer. In this project the report bucket
is the same bucket (`S3_REPORT_BUCKET == S3_ARTIFACT_BUCKET`).

Below, variables are written as `<…>`; everything else is a fixed prefix. The
bucket is whole-bucket public-read (`s3:GetObject` on `*`), and lifecycle rules
expire the *ephemeral* prefixes (`PRs/`, `REFs/`, `runs/`, `ci_cache/`, the merge
snapshot tiers) after `retention_days`, while root objects and `packages/` are
kept forever (`praktika/infrastructure/storage.py`).

## Root

| Key | Purpose |
|---|---|
| `praktika.html` | Report viewer SPA (`Settings.HTML_PAGE_FILE`), gzipped, public. Loaded as `…/praktika.html?PR=<n>&sha=<sha>&name_0=<workflow>&name_1=<job>`. |
| `json.html` | Alternate/legacy report viewer (downstream-proxy variant). |

## Reports & artifacts (per run)

The report prefix is built by `_Environment.get_s3_prefix_static`:
`PRs/<pr>/<sha>/<norm_workflow>` (pull_request), `REFs/<branch>/<sha>/<norm_workflow>`
(push), or `…/latest/<norm_workflow>` (the `latest=True` alias).
`<norm_*>` = lowercased, spaces/slashes → underscores.

| Key | Purpose |
|---|---|
| `PRs/<pr>/<sha>/<norm_wf>/` | PR-event report tree root. |
| `REFs/<branch>/<sha>/<norm_wf>/` | Push-event report tree root. |
| `…/<norm_wf>/result_<norm_wf>.json` | Workflow-level `Result` (the report tree; versioned read-modify-write). |
| `…/<norm_wf>/result_<norm_job>.json` | Each job's trimmed `Result` node (top of the report). |
| `…/<norm_wf>/<norm_job>/job.log` | The job process's stdout (`Settings.RUN_LOG`), attached to the job result. |
| `…/<norm_wf>/<norm_job>/result_<norm_wf>.json` | Workflow report snapshot the job dumped. |
| `…/<norm_wf>/<norm_job>/workflow_config_<norm_wf>.json` | `RunConfig` snapshot for that job. |
| `…/<norm_wf>/<norm_job>/…` | The job's declared S3 artifacts (`Job.Config.provides`). |
| `…/<norm_wf>/praktika_controller.log` | Orchestrator instance's bootstrap-controller log — linked on the top-level result when `praktika_debug`. |
| `…/<norm_wf>/praktika_orchestrator.log` | The `orchestrate` process's stdout — linked on the top-level result when `praktika_debug`. |

## Orchestrator run state (ephemeral, per run)

`<run_id>` = the GitHub check-run id (a UUID in local mode).

| Key | Purpose |
|---|---|
| `runs/<run_id>/state.json` | DAG snapshot; its `finalized` flag routes reruns (live orchestrator vs resume). |
| `runs/<run_id>/cancel` | Kill flag the runners poll (cancel-on-new-push / explicit cancel). |
| `runs/<run_id>/cancel-request` | Cancel request the orchestrator observes. |
| `runs/<run_id>/resume.lock` | Rerun/resume mutex. |
| `runs/<run_id>/rerun-request/…` | Pending rerun requests. |
| `runs/<run_id>/<Job_Name>/heartbeat.json` | Per-job liveness (controller writes; orchestrator sweeps). |
| `runs/<run_id>/<Job_Name>/final.json` | Per-job completion (`rc`, environment, result) the orchestrator consumes. |
| `runs/<run_id>/<Job_Name>/praktika_controller.log` | Per-job bootstrap-controller log (clone/restore/dispatch); linked on that job's result when `praktika_debug`. |

Note `<Job_Name>` here keeps spaces replaced by underscores but is otherwise the
job name (distinct from the report tree's `<norm_job>`).

## CI cache (`Settings.CACHE_S3_PATH = ci_cache`, `Settings.CACHE_VERSION = 2`)

| Key | Purpose |
|---|---|
| `ci_cache/v<ver>/<norm_job>/<job_digest>/success` | Cache-hit record (`Cache.CacheRecord`); write-once on `pull_request` events, overwrite on trusted events. `v1/` is the previous cache version. |
| `ci_cache/submodules/<hash>.tar.zst` | Content-addressed submodule cache archive (write-once). Prefix exists in code; empty when the project has no submodules. |

## Repo snapshots (trust-tiered, content-addressed)

Written by the Config Workflow when `Settings.ENABLE_S3_REPO_SNAPSHOT` is set;
restored by every downstream job instead of cloning. For `pull_request` with
`Settings.ENABLE_PR_EPHEMERAL_MERGE_COMMIT` the snapshot is the ephemeral merge of
head into the target tip; otherwise (and for push) it is the plain head. The trust
tier is `PRs` (untrusted) vs `REFs` (trusted); IAM scopes them so `pr-*` pools are
denied *write* to `REFs/`, and trusted pools are denied *read+write* to `PRs/`.
See `docs/native-merge-commit.md`.

| Key | Purpose |
|---|---|
| `repo-snapshots/v1/PRs/<sha256>.tar.zst` | Snapshot for `pull_request` (fork-reachable) runs — the ephemeral merge, or the head. |
| `repo-snapshots/v1/REFs/<sha256>.tar.zst` | Snapshot for `push`/trusted runs (plain head, no merge). |

`<sha256>` is the content hash of the archive (tamper-evident: consumers re-hash
the download and reject a mismatch).

## Per-PR signals (`pr/<pr>/`)

| Key | Purpose |
|---|---|
| `pr/<pr>/cancel-before-<scope>` | Cancel-on-new-push markers (`<scope>` = `base` / `default`), gated by event timestamp. |
| `pr/<pr>/merge-base-pin.json` | Sticky merge base: `{base_sha, pinned_ts, base_branch}` — the pinned target-branch commit reused within `Settings.STICKY_MERGE_BASE_HOURS` of the PR's previous run. |

## AI orchestrator & external-PR gate

| Key | Purpose |
|---|---|
| `ai-sessions/pr/<pr>/…` | AI orchestrator session data per PR. |
| `external-pr-approvals/<owner>__<repo>/pr/<n>.json` | External/fork-PR approval-gate state (the "External PR Approval" check). |

## Packages

| Key | Purpose |
|---|---|
| `packages/<name>-<version>.whl` | Published `praktika` / `praktika_controller` wheels. |
| `packages/latest/<name>-0.0.0.whl` | The `latest` alias runners install on boot (`_PRAKTIKA_WHL`, `_PRAKTIKA_CONTROLLER_WHL`). |

## Configured but unused in this project

- `Settings.EVENT_FEED_S3_PATH` (Slack/event feed) — unset here, so no `event-feed/` prefix exists.
- `Settings.S3_UPSTREAM_REPORT_BUCKET` (fork/upstream report proxying) — unset here.
