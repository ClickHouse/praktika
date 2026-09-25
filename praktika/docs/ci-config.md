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
- **Version pinning** — pin the praktika / praktika-controller version a run uses,
  read once and preserved in run metadata (see [Proposed: version
  pinning](#proposed-version-pinning-not-yet-implemented)).

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

## Proposed: version pinning (not yet implemented)

> Status: **design only.** Nothing below is wired up yet — the sole implemented
> setting today is `force_merge_commit`. This section records the intended shape
> so the config schema grows coherently.

Goal: pin the praktika (and praktika-controller) version a run uses, so an
unexpected upgrade cannot break an already-started run, and so the version can be
rolled out from SSM instead of rebaking the AMI. Proposed keys:

```json
{
  "praktika_version": "<source>",
  "praktika_controller_version": "<source>"
}
```

Each `<source>` supports three interchangeable forms, all of which `pip install`
already accepts as-is:

- **version** — a released spec, e.g. `"praktika==0.1.9"` (once published to an
  index).
- **https path** — a wheel URL, e.g. `"https://.../praktika-0.1.9-py3-none-any.whl"`.
- **repo path** — a filesystem path/checkout, e.g. `"."` or `/opt/praktika/src`
  (the current `runtime_source` behavior).

### Two different lifecycles (do not conflate)

- **`praktika_version` → per-run pin.** Read once at run start, frozen into the
  run metadata (the `_Environment` / `task` / `state.json` carrier — same pattern
  as `SNAPSHOT_SHA` / `WORKFLOW_START_TIME`), and every job of the run sources its
  runtime from that frozen value. This is what protects a started run from a
  mid-run upgrade: all jobs agree on one version regardless of what changes in SSM
  afterward. In S3-snapshot mode a repo-sourced praktika is *already* content-pinned
  by the snapshot; this closes the gap for the URL / version / moving-source forms.
- **`praktika_controller_version` → global desired-state, NOT a per-run pin.** One
  controller process serves many runs off the queue, so it cannot run a different
  controller version per in-flight run. Instead the controller converges to the
  SSM-desired version by self-reinstalling **between runs** (at an idle boundary),
  never mid-run. It still protects in-flight runs (the reinstall/re-exec happens
  when the controller is idle), but the mechanism is a rolling self-update.

### Sketch of the mechanism

- **`venv_manager._normalize_source`** must branch on the form (URL scheme or
  requirement spec → pass through verbatim; otherwise resolve as a local path).
  This is the only code gap for the three forms; pip handles the rest.
- **Version introspection already exists** (`praktika/version.py`:
  `current_praktika_version`, `current_praktika_controller_version`,
  `version_key`). Mismatch detection compares the running version to the desired
  one.
- **Controller self-reinstall** reads `praktika_controller_version` from SSM
  (trusted infra, read before any PR code runs — keep it in SSM, never in
  PR-influenced metadata), and on mismatch installs the desired form into a fresh
  overlay venv and re-execs into it (mirroring `_install_runtime_over_base_venv`),
  rather than mutating the live system-python in place.

### Risks / guards this needs before shipping

- **Crash-loop protection** — a bad version otherwise makes every fresh instance
  reinstall → crash → restart → reinstall forever. Fall back to the baked version
  on install/import/health failure, cap attempts, persist last-known-good.
- **Install atomicity** — install into an overlay venv and re-exec into it; never
  half-mutate the running controller's env.
- **Idle boundary** — only reinstall/re-exec with no message in flight (or at
  process start, before claiming work).
- **Bootstrapping** — only controllers that already ship this logic can
  self-update; the first rollout is still a normal AMI/deploy.

Because parts differ in risk, the intended rollout order is: (1) `_normalize_source`
three-form support + freeze `praktika_version` into run metadata (low risk), then
(2) controller self-reinstall as a separate, carefully-guarded change.

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
