# CI config (out-of-repo settings)

The **CI config** is a single per-project JSON object stored in AWS SSM Parameter
Store, outside the repository. It is the home for CI settings that must apply
*regardless of what any given branch's checkout contains* — knobs the operator
controls, not the code under test.

- **Parameter name:** `{PROJECT_SLUG}-ci-config` (e.g. `myproject-ci-config`).
- **Type:** `String`, holding a JSON **object**. JSONC is tolerated — `//` and
  `/* */` comments and trailing commas are stripped before parsing (`_strip_jsonc`),
  so you can comment a field out without silently invalidating the whole parameter
  (a strict parse error reads as `{}` — every feature off). Comments/commas inside
  string values (e.g. a `//` in an `https://` URL) are preserved.
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

- **Live-editable.** The value is read once at the **start of each run** (by the
  controller — see [How the config reaches
  jobs](#how-the-config-reaches-jobs-read-once-frozen-in-run-metadata)), so you
  set or clear it in the console / CLI and the *next run* picks it up — no instance
  replacement, no redeploy. Instance tags are only read at boot.
- **Cheap.** One `GetParameter` per run is negligible next to a full CI run. It is
  read once and then frozen into run metadata, so jobs never re-read it.
- **Safe by absence.** A missing / unreadable / non-JSON / non-object parameter
  reads as `{}` — every feature off. The parameter is entirely optional; nothing
  breaks if it doesn't exist.
- **Scoped.** It lives under the project's `{slug}-*` IAM prefix
  (`praktika.infrastructure.native.iam_scope`), so only this project's roles can
  read it.

## Intended scope (roadmap)

Today the CI config carries a migration switch and version pins (below). It is
meant to grow into the general surface for out-of-repo CI settings, for example:

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

**Limitation — sticky merge base is disabled.** `force_merge_commit` also forces
`STICKY_MERGE_BASE_HOURS` to `0`, so every run merges the PR head into the *live*
target tip (the sticky-base optimization, which reuses a pinned target commit
across a PR's rapid re-pushes to keep the digest cache warm, is off). This is
deliberate: with `force_merge_commit` the checkout (e.g. an upstream-sync head)
can't be trusted to resolve `S3_ARTIFACT_BUCKET`, so the bucket is read from the
**merged tree** *after* the merge. Sticky-base resolution would need that bucket
*before* the merge (it reads/writes an S3 pin), which is no longer available — so
it is turned off in this mode. See `praktika_controller.merge.read_repo_settings`
and `prepare_repo_snapshot`.

**Lifecycle.** This is a migration lever: set it when you enable the feature,
leave it on until old branches age out, then delete the parameter (or set it to
`false`). Because it's read per run, both flipping it on and removing it take
effect on the next run.

### `praktika_version` / `praktika_controller_version` (version pins)

Pin the praktika and/or praktika-controller version a run uses, so an unexpected
upgrade cannot break an already-started run and so a version can be rolled out (or
rolled back) from SSM instead of rebaking the AMI:

```json
{
  "praktika_version": "<source>",
  "praktika_controller_version": "<source>"
}
```

Both are optional and independent, and an empty string (`""`) is treated exactly
like "not set" (no pin). Each `<source>` supports three interchangeable forms, all
of which `pip install` accepts as-is (detected by `venv_manager.is_passthrough`):

- **version** — a released spec, e.g. `"praktika==0.1.9"` (must include an exact
  `==`; a bare name is treated as a path).
- **https path** — a wheel URL, e.g. `"https://.../praktika-0.1.9-py3-none-any.whl"`.
- **repo path** — a filesystem path/checkout, e.g. `"."` or `/opt/praktika/src`.
  For `praktika_version`, relative paths resolve against the run's checkout. For
  `praktika_controller_version`, **only absolute paths** are allowed (a URL or spec
  otherwise): controller self-update runs *before* any checkout exists, so a
  relative path has nothing to resolve against and is rejected.

Both pins are read **once from SSM by the orchestrator** and frozen into run
metadata via the same `ci_config` carrier as `force_merge_commit` (§ *How the
config reaches jobs*), so every controller of the run obeys the value the
orchestrator pinned — **read from run metadata, not SSM**.

#### When it applies (praktika development setup)

Version pinning targets the **praktika development setup**: pools whose runtime is
*not* already pinned on the runner — i.e. the install source points at a **floating
"latest"** (a `.../latest/…whl` alias, a moving branch checkout, or a base venv
built to track head). If a pool already pins an exact version on the runner (a
versioned base venv / wheel), that fixed version is what runs; a `ci_config` pin is
redundant there.

Its purpose in that floating setup is twofold:

- **Pin one version across the whole run fleet.** Without a pin, each instance
  resolves "latest" independently, so a fleet can end up straddling versions
  (some runners on the wheel published a minute ago, others on the previous one) —
  and within a single run the orchestrator and its job runners could disagree.
  A pin freezes one version into run metadata so the orchestrator and every job
  runner of the run use exactly the same one.
- **Change the version without an infra update.** Rolling a floating setup forward
  or back otherwise means republishing the "latest" wheel and/or rebaking the AMI.
  A pin moves that control to a single SSM edit that the *next* run picks up — no
  wheel republish, no AMI rebake, no instance replacement.

#### `praktika_version` (per-run runtime pin)

Read once at run start, frozen into run metadata, and every job runner installs
its praktika runtime from that frozen value (via
`controller._resolve_runtime_source`, taking precedence over a pool's
`praktika_runtime_source` tag). All jobs of a run agree on one version regardless
of what changes in SSM afterward. In S3-snapshot mode a repo-sourced praktika is
already content-pinned by the snapshot; this closes the gap for the URL / version
forms. Installed into the base-venv overlay with `--no-deps`, so the base venv must
already carry praktika's runtime dependencies (a new dependency needs an AMI
rebake, as today).

#### `praktika_controller_version` (per-run controller pin, self-upgrade)

The controller is the `praktika-controller` wheel installed into the system
`python3.12` and run by systemd (`Restart=always`). When the frozen
`praktika_controller_version` differs from the source the controller last
installed, the controller **self-upgrades**: at the idle boundary (a message is
received but not yet processed), it `pip install --force-reinstall`s the pinned
source into system python, persists the new source, releases the message back to
the queue un-processed, and exits — the `Restart=always` unit relaunches into the
new code, which re-receives the message and proceeds. No run is interrupted
mid-flight. Mechanism lives in `praktika_controller.self_update.maybe_self_update`,
wired into `controller.poll()`.

This is a **dev-mode** mechanism, kept deliberately simple:

- **Source of the desired version.** The orchestrator (workflow role) reads it
  from SSM (`load_ci_config`) for a fresh run, or the frozen copy on a rerun event.
  Job runners read it from the task's frozen `ci_config` — never SSM — so all
  runners of a run converge to the version the orchestrator pinned.
- **Persistence.** The last-installed source string is persisted to
  `/var/lib/praktika/controller_version.json` (override with
  `PRAKTIKA_CONTROLLER_STATE_PATH`), so the controller reinstalls only when the pin
  string changes, not on every message.
- **Fail hard.** The pin is not validated, version-checked, or rolled back. A
  bad/unreachable source makes pip fail and the error propagates (the message is
  retried / the instance replaced by the normal infra-failure path) rather than
  silently running the old controller. Use an exact, reachable pin.
- **Bootstrapping.** Only controllers that already ship this logic can self-update;
  the first rollout is a normal AMI / boot-time wheel install, which also remains
  the fallback when no pin is set.

> **Caveat — runner flapping.** Because the controller version is frozen per run, a
> runner serving tasks from two concurrently-active runs pinned to *different*
> controller versions will reinstall/restart back and forth. In practice SSM is
> stable and all live runs share one value; changing SSM only affects new runs
> while in-flight runs keep their frozen value.
>
> **Caveat — moving sources.** Persistence is keyed on the source *string*, so a
> mutable `…/latest/…whl` URL is not detected as "changed". Pin to an immutable
> exact version or versioned URL.

## Setting / clearing the parameter

```bash
# Enable (migration on) / pin versions
aws ssm put-parameter \
  --name "{PROJECT_SLUG}-ci-config" \
  --type String \
  --value '{"force_merge_commit": true, "praktika_version": "praktika==0.1.9", "praktika_controller_version": "praktika-controller==0.1.8"}' \
  --overwrite \
  --region "$AWS_REGION"

# Inspect
aws ssm get-parameter --name "{PROJECT_SLUG}-ci-config" --region "$AWS_REGION" \
  --query 'Parameter.Value' --output text

# Disable (delete → reads as {} → all features off)
aws ssm delete-parameter --name "{PROJECT_SLUG}-ci-config" --region "$AWS_REGION"
```

## How the config reaches jobs (read once, frozen in run metadata)

The SSM parameter is read **once per run, by the controller**, and then frozen
into run metadata so no job re-reads SSM (which could change mid-run) and a resume
reuses the original run's config:

1. **Controller** (`controller.py`) resolves the config once at run start and
   (a) passes it to `merge.read_repo_settings` for the `force_merge_commit` gate,
   and (b) exports it to the orchestrator as `PRAKTIKA_CI_CONFIG` (JSON). A **fresh
   run** reads it from SSM (`common.load_ci_config`); a **resume/rerun** instead
   uses the original run's frozen config carried on the rerun event (from
   `state.json`, alongside the snapshot identity), so a resume never re-reads SSM
   and cannot make a different merge decision than the run it resumes — important
   for a fresh-base rerun, where a divergent decision would mismatch the DAG/runtime
   against the restored snapshot.
2. **Orchestrator** (`orchestrator/__init__.py`) parses `PRAKTIKA_CI_CONFIG` and
   calls `WorkflowState.seed_ci_config(...)` — first-write-wins — which freezes it
   into `state.json` (`save_snapshot`) and restores it on resume
   (`seed_from_snapshot`). Same lifecycle as `snapshot_sha`.
3. Every per-job **task** carries `ci_config` (`state.py`), and `job_runner.py`
   puts it into `_Environment.CI_CONFIG`.
4. **Job code** reads it via `Info().ci_config` (a dict; empty when unset).

So `force_merge_commit` is consumed on trusted infra in the controller; the
job-visible copy in `Info().ci_config` is the frozen snapshot for any future
job-side settings — treat it as advisory for anything security-relevant.

## Implementation notes

- The reader lives in the **controller** package
  (`bootstrap/src/praktika_controller/common.py`) because the merge module must
  not import praktika (it runs before praktika is reinstalled from the — possibly
  PR-tampered — checkout). Praktika's runtime does **not** re-read SSM; it reads
  the frozen copy threaded through run metadata (above).
- IAM: the orchestrator instance role grants `ssm:GetParameter` on
  `iam_scope.ssm_parameter_arns()` (`{slug}-*`); see
  `praktika/infrastructure/native/orchestrator_pool.py`.
