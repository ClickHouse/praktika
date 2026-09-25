# CI config (out-of-repo settings)

The **CI config** is a single per-project JSON object stored in AWS SSM Parameter
Store, outside the repository. It is the home for CI settings that must apply
*regardless of what any given branch's checkout contains* — knobs the operator
controls, not the code under test.

- **Parameter name:** `{PROJECT_SLUG}-ci-config` (e.g. `myproject-ci-config`).
- **Type:** `String`, holding a JSON **object**.
- **Region:** the project's region (`AWS_DEFAULT_REGION` / `AWS_REGION`).
- **Reader:** `praktika_controller.common.load_ci_config`.

## Why it exists

Most CI settings live in the repo (`ci/settings/settings.py`), which is correct:
they version with the code and a PR can only affect its own run. But some settings
can't live there:

1. **They must not depend on branch state.** When you enable a feature, old
   branches and long-lived PRs don't have the new setting in their checkout, so
   praktika would read the old (feature-off) value and behave inconsistently with
   `main`. An out-of-repo value applies uniformly to every run.
2. **They're operational, not code.** Migration switches, incident toggles, and
   rollout flags belong to whoever operates the CI, and should be changeable
   without a commit + review + deploy cycle.

SSM (rather than an instance tag or env var) is used deliberately:

- **Live-editable.** The value is read fresh on each run, so you set or clear it
  in the console / CLI and the *next run* picks it up — no instance replacement,
  no redeploy. Instance tags are only read at boot.
- **Cheap.** One `GetParameter` per run is negligible next to a full CI run.
- **Safe by absence.** A missing / unreadable / non-JSON / non-object parameter
  reads as `{}` — every feature off. The parameter is entirely optional; nothing
  breaks if it doesn't exist.
- **Scoped.** It lives under the project's `{slug}-*` IAM prefix
  (`praktika.infrastructure.native.iam_scope`), so only this project's roles can
  read it.

## Intended scope (roadmap)

Today the CI config carries a single migration switch (below). It is meant to
grow into the general surface for out-of-repo CI settings, for example:

- **More CI-wide operational toggles** — rollout flags, feature gates, temporary
  overrides during incidents or migrations.
- **Per-user settings** — a `users` sub-object keyed by GitHub login, letting
  individual developers opt into experimental behavior for their own PRs without
  touching the repo (e.g. `{"users": {"alice": {"...": true}}}`).

When adding a key, keep the same contract: optional, safe-by-absence, and
documented here.

## Supported settings

### `force_merge_commit` (boolean, default `false`)

Forces the ephemeral PR merge + repo snapshot **on** for every run, overriding
whatever the checked-out branch says:

```json
{ "force_merge_commit": true }
```

Setting it to `true` makes the controller behave as if both
`ENABLE_S3_REPO_SNAPSHOT` and `ENABLE_PR_EPHEMERAL_MERGE_COMMIT` were `True`
(see `praktika/settings.py` and `ci-config`'s consumer in
`praktika_controller.merge.read_repo_settings`).

**Why it's needed — migration.** When you first enable praktika (or these two
settings) in an existing project, old branches and open PRs don't have the
settings in their checkout, so praktika reads them as `False` and CI runs against
the *stale branch head* instead of the merge with the current target tip — the
result is inconsistent with `main`, and may run outdated CI driver code. Turning
`force_merge_commit` on bridges the gap: every run merges the PR head into the
current target tip and runs the *current* praktika code against that merged tree.

**How it propagates.** The controller only needs this at the pre-merge gate. Once
the ephemeral merge happens, the run reinstalls praktika from the **merged tree**,
which carries the target branch's own (True) settings — so the config workflow,
DAG, and every job stay consistent without any further override.

**Lifecycle.** This is a migration lever: set it when you enable the feature,
leave it on until old branches age out, then delete the parameter (or set it to
`false`). Because it's read per run, both flipping it on and removing it take
effect on the next run.

## Setting / clearing the parameter

```bash
# Enable (migration on)
aws ssm put-parameter \
  --name "{PROJECT_SLUG}-ci-config" \
  --type String \
  --value '{"force_merge_commit": true}' \
  --overwrite \
  --region "$AWS_REGION"

# Inspect
aws ssm get-parameter --name "{PROJECT_SLUG}-ci-config" --region "$AWS_REGION" \
  --query 'Parameter.Value' --output text

# Disable (delete → reads as {} → all features off)
aws ssm delete-parameter --name "{PROJECT_SLUG}-ci-config" --region "$AWS_REGION"
```

## Implementation notes

- The reader lives in the **controller** package
  (`bootstrap/src/praktika_controller/common.py`) because the merge module must
  not import praktika (it runs before praktika is reinstalled from the — possibly
  PR-tampered — checkout). When praktika's runtime needs to honor the same
  parameter, add a parallel reader in praktika rather than sharing this one.
- IAM: the orchestrator instance role grants `ssm:GetParameter` on
  `iam_scope.ssm_parameter_arns()` (`{slug}-*`); see
  `praktika/infrastructure/native/orchestrator_pool.py`.
