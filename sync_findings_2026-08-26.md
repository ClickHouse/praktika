# Praktika Sync Findings — 2026-08-26

Direction this round: **imported upstream `../ClickHouse/ci/praktika` wholesale into `./praktika`, then restored every overwritten local change.** Next step (per plan): verify in CI here, then reverse-sync local → upstream.

Method: `rsync` upstream over `praktika/` (no `--delete`, so local-only files survived), then 10 parallel per-file analyses of `git show HEAD:<f>` (old local) vs working tree (upstream), classified RESTORE / SAFE / UNSURE against local git history.

## Local-only files (never in upstream; preserved untouched)
`ai_review.py`, `execution/`, `orchestrator/ai/bedrock_openai.py`, `infrastructure/native/s3_proxy.py` + `s3_proxy_signer.py` + `s3_proxy_user_data.sh`, `infrastructure/pics/`, `json.html.gz` (now stale — see below).

## Upstream files ADOPTED (kept as-is; genuine upstream gains, no local loss)
`result.py` (chain rendering, PipelineUtilization), `cidb.py` (`insert_workflow_usage` + `attributes` JSON col — **CI-DB schema change**), `gh_auth.py` + `git.py` (superset; lambda auth already ours), `usage.py` (PipelineUtilization), `native_jobs.py`, `docker.py` (layer compression, pull retries), `cache.py`/`event.py`/`hook_cache.py` (event trust-boundary), `info.py` (merge-queue bugfix), `job.py`, `utils.py`, `workflow.py`, `yaml_generator.py`, `hook_html.py`, `host_metrics.py`/`.md` (**new**), `infrastructure/native/lambda_gh_trigger.py` (rename-aware diff, 300-file autoapprove cap; **your `allowed-users` feature was intact upstream**), `infrastructure/runner/runner-init.py` (macOS re-provision).

## Local changes RESTORED (fork ahead of upstream)
- **Whole files reverted to local** (upstream was an older snapshot): `orchestrator/state.py`, `orchestrator/ai/{provider,anthropic,mock,__init__}.py`, `orchestrator/PROTOCOL.md` (#137 heartbeat/SQS-redelivery, #140/#142 AI review + `bedrock-anthropic`/`bedrock-openai` providers), `infrastructure/cloud.py` (S3Proxy wiring + alphanumeric slug), `infrastructure/storage.py` (#138 per-prefix retention), `infrastructure/native/{image_builder,runner_pool,lambda_pool_autoscaler,user_data,orchestrator_pool,configs,__init__,iam_scope}.py` + `README.md` (#138/#139/#141, bedrock IAM, system-logs streamer, S3-proxy scaffold), `project_init.py` (S3-proxy scaffold, **controller wheel 0.1.9** [upstream downgraded to 0.1.4], region-suffixed artifact bucket, `provider="bedrock-anthropic"`, `allowed_push_branches`).
- **Surgical merges** (upstream base + local patch): `runner.py` (removed re-added post-`_post_run` `result.dump()` — reinstates 9fd3a2e), `gh.py` (`publish_gh_pages` Pages-root clean #141 + `viewerDidAuthor` GraphQL field for AI review), `settings.py` (`HEARTBEAT_STALL_S`, `HEARTBEAT_TIMEOUT_S=900`, `DEAD_JOB_MAX_REDISPATCH`), `__main__.py` (`review` subcommand), `validator.py` (dash-separator + alphanumeric PROJECT_SLUG check), `_environment.py` (unconditional `<workflow>/` S3 segment), `json.html` + `praktika.html` (matching `<workflow>/` fetch segment).

## Conflict decisions (user)
1. **Slug policy** → keep local **alphanumeric-only** (0e2f068) across cloud/project_init/validator/iam_scope; dropped upstream's `_`-separated model + iam_scope `-` guard.
2. **S3 report prefix** → restore local **per-workflow segment** (727b47d + b60f446) in `_environment.py` + both HTML viewers (repo has 2 PR workflows that would collide on a flat path).
3. **GH auth** → keep **upstream** `GHAuth.auth(workflow, no_strict=True)`; dropped local `USE_CUSTOM_GH_AUTH` gating (runtime unaffected — runners auth via lambda; unknown setting is ignored by the loader). Removed dead `USE_CUSTOM_GH_AUTH` from `ci/settings/settings.py` + project_init template; deleted the 2 `_GH_Auth` tests and the `test_validator` monkeypatch line.

## Verification
`python -m compileall` clean; targeted + full suite = **414 passed, 5 skipped (live), 3 failed** — all 3 unrelated to the sync:
- `test_runner::test_config_workflow_failure_is_handled_gracefully` — fails on clean HEAD too; needs a valid AWS SSO session to mint the GH token. Passes in CI.
- `test_infra_projects::{test_project_image_builders...,test_shared_arm64_images...}` — from the **staged** `ci/infrastructure/projects.py` pool rename `arm-2xsmall-bedrock` → `pr-arm-2xsmall-bedrock` (this branch's WIP); the committed test still expects the old LT name. Pass on clean HEAD. Update `test_infra_projects.py:1582/1759` to the new name as part of the branch work.

## Follow-ups / notes
- `json.html.gz` is a stale gzip of the old json.html; deploy re-gzips from source, so only regenerate if it is served directly.
- Stats URL in json.html left as upstream generic `s3.amazonaws.com` (was a stale CloudFront URL locally); revisit if the statistics widget breaks.
- CI-DB schema moved to `insert_workflow_usage` (JSON `attributes` column) — confirm the CI DB table matches.
