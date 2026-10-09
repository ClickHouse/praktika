# Controller bootstrap performance

Living doc tracking optimizations to the **orchestrator bootstrap** — everything
the `praktika-controller` does between receiving a `pull_request` event and
launching the orchestrator subprocess: clone head, ephemeral PR merge, build +
publish the repo snapshot, install the runtime venv.

The per-step `[profile] <step>: <n>s` lines in the controller log drive this. See
`bootstrap/src/praktika_controller/{common,merge,controller}.py` and
[repo-git-management.md](repo-git-management.md).

## How to read the profile lines

Each phase is wrapped in `common.profile_step(log, label)`, which logs a
`[profile] <label>: <seconds>s` line. Grep a run's CloudWatch log for `[profile]`
to get the breakdown.

## Baselines

### ClickHouse/clickhouse-private PR#78122 — 2026-09-30, controller 0.1.8

First large-repo run with per-step profiling + multipart upload. Total
clone→orchestrator ≈ **5m40s**.

| Step | Time | Notes |
|---|---|---|
| snapshot: pack + hash (tar\|zstd) | **190.04s** | produces 700.5 MiB archive; zstd default level (3), compression-bound |
| clone: fetch head (depth 1) | 31.33s | network / pack download |
| clone: checkout head | 31.11s | parallel checkout on; ~EBS-bandwidth bound |
| snapshot: git init + local fetch | 27.15s | redundant re-materialize of the tree |
| runtime: resolve venv + install | 20.54s | incl. ~12s one-time overlay copy (first task) |
| merge: ensure base history (unshallow) | 14.44s | network |
| snapshot: checkout worktree | 12.37s | redundant re-materialize of the tree |
| merge: merge head into base | 5.61s | |
| snapshot: multipart upload | **5.03s** | 700 MiB → ~140 MiB/s |
| merge: re-read artifact bucket | 0.00s | |

### ClickHouse/clickhouse-private PR#78122 — 2026-10-01, controller with zstd-1

After opt #5 (zstd level 1) deployed. Total clone→orchestrator ≈ **2m40s**
(down from ~5m40s — roughly halved).

| Step | Time | vs 09-30 |
|---|---|---|
| snapshot: pack + hash (tar\|zstd) | **57.95s** | 190.04s → **-69%** (archive 700.9 MiB, ~unchanged) |
| clone: fetch head (depth 1) | 29.43s | 31.33s |
| merge: ensure base history (unshallow) | 14.33s | 14.44s |
| snapshot: git init + local fetch | 12.32s | 27.15s |
| snapshot: checkout worktree | 11.92s | 12.37s |
| clone: checkout head | 11.08s | 31.11s (variance / warm EBS — not attributed) |
| merge: merge head into base | 7.97s | 5.61s |
| runtime: resolve venv + install | 7.15s | 20.54s (overlay copy only ~4s here) |
| snapshot: multipart upload | 4.90s | 5.03s |
| snapshot: build + publish (total) | **89.89s** | 237.38s |

Connection-pool warnings (`size: 10`) still present — opt #6 not yet in the
deployed build (zstd-1 is; the pool fix lands on redeploy).

### ClickHouse/clickhouse-private PR#78122 — 2026-10-01, with #6 + #7

Confirmation run (different head commit, archive 723 MiB). Both fixes verified:

- **#6:** zero `Connection pool is full` warnings (was 6/run); multipart upload 4.73s.
- **#7:** `snapshot: build minimal .git (no checkout): 0.18s` replaced the old
  `snapshot: checkout worktree: 11.92s`.

Remaining breakdown: clone fetch 33.65s, snapshot pack+hash 62.94s (now the clear
#1), merge unshallow 15.52s, snapshot git-init+local-fetch 12.81s, merge 10.20s,
clone checkout 10.31s, runtime 7.71s. Snapshot build+publish total 80.76s.

### ClickHouse/clickhouse-private PR#77396 — 2026-09-28, controller pre-profiling

Coarse timings (from bracketing log lines, before per-step profiling):
head clone+checkout ≈ 37s, unshallow+base ≈ 15s, merge ≈ 2s, **snapshot
build+upload ≈ 94s** (dominated by single-stream `put_object`), overlay copy ≈ 4s,
pip install ≈ 3s.

## Optimizations

Status: **DONE** = merged/deployed · **DEV** = on `controller-bootstrap-perf`,
awaiting a ClickHouse run to confirm · **PLANNED**.

| # | Change | Status | Expected | Measured |
|---|---|---|---|---|
| 1 | **Multipart streaming upload** — `s3.upload_file` + `TransferConfig` instead of `put_object(Body=f.read())` (whole archive in RAM, single stream) | DONE | eliminate single-stream PUT bottleneck | 700 MiB in **5.03s** (was ~90s single-stream) |
| 2 | **Single-pass pack+hash** — tee `tar\|zstd` through the sha256 while writing the archive; drop the separate `_sha256_file` read | DONE | remove one full-archive read pass | folded; hashing overlaps, ~free |
| 3 | **Per-step profiling** — `profile_step` around clone / merge / snapshot / runtime / restore | DONE | visibility | in place |
| 4 | **Clone git flags** — `--no-tags --no-recurse-submodules` on head fetch; `-c checkout.workers=0` (parallel checkout) on both large checkouts | DONE | faster fetch + tree write | checkout still 31s → likely EBS-bound, not CPU; flags kept (cheap) |
| 5 | **zstd level → 1** (`SNAPSHOT_ZSTD_LEVEL`) — snapshot is transient/content-addressed, so favor speed over ratio; also speeds every job's restore (`zstd -dc`) | DONE | cut most of the 190s pack step; slightly larger archive | pack **190s → 57.95s (-69%)**, archive 700.5→700.9 MiB (negligible) |
| 6 | **S3 client pool sizing** — `_s3_client()` sets `max_pool_connections = MULTIPART_MAX_CONCURRENCY (16)` so parallel parts don't exhaust the default-10 pool | DONE | remove "Connection pool is full" warnings / socket churn | **0 warnings** (was 6/run); upload 4.73s |
| 7 | **Skip the redundant snapshot checkout** — keep the depth-1 local fetch for a minimal `.git`, but set `snap_dir` HEAD+index via plumbing (`update-ref` + `read-tree`, no worktree write) and tar `clone_dir`'s already-materialized worktree + `snap_dir/.git` in one pass | DONE | save ~12s (eliminate the second `checkout worktree` step) | `checkout worktree` **11.92s → `build minimal .git` 0.18s** |
| 8 | **Warm / AMI-baked git mirror** — borrow a baked mirror's objects via alternates so the head fetch is a delta + no unshallow | REJECTED | cut ~43s network (fetch 29 + unshallow 14) | net LOSS on AWS (EBS lazy-load); mechanism validated but not deployable on cold one-shot AMIs — see below |
| 9 | **Remove the runtime overlay** — install the `runtime_source` straight into the base venv (force-reinstall --no-deps every task) instead of copying it to a per-instance overlay first | DEV | drop the one-time ~12-20s overlay copytree | overlay copy was 20s this warm run; safe because a `runtime_source` pool always reinstalls and never runs a baked-mode task, and each instance has its own AMI copy |
| 10 | **Idle boot warm-clone** — an idle (reserved) orchestrator pre-fetches the base branch into a warm `.git` (+ checkout); the per-task clone adopts it and applies only the PR delta | DEV | fetch ~33s→~1s + unshallow ~15s→0 + checkout→delta on warm instances | **AWS: head fetch 33s→0.32s, unshallow 15s→0.45s, clone total 44s→15s.** checkout was 14.8s (treeless lazy-fetch) → idle checkout added to make it a delta |
| 11 | **Run Praktika from the checkout via `PYTHONPATH`** — for a local-checkout `runtime_source`, skip the per-task `pip install` of `./ci` into the base venv and instead set `PYTHONPATH=<checkout>/ci` so `python -P -m praktika` imports Praktika straight from the tree (`venv_manager.resolve_praktika_runtime`) | DEV | drop the per-task wheel build + install (~several s); also removes the `ci/build` + `*.egg-info` the in-tree build left in the checkout (dirty-repo noise) | — |

### Experiments run 2026-10-01 (all reverted — negative/null)

Measured on PR#78122 (head 3b1e8cf, archive 751 MiB); all three rolled back to
the committed baseline (zstd `-1`, git-default compression, single-pass hash).

- **Hash cost — ANSWERED (free).** `sha256 cpu (within pack): 0.54s` out of a
  66.9s pack step (0.8%). Hashing is fully hidden behind zstd; single-pass
  pack+hash costs nothing. (Timing scaffold removed; conclusion kept.)
- **zstd `--fast=1` — rejected.** Pack did NOT get faster (62.9s → 66.9s) and the
  archive grew (723 → 751 MiB = bigger per-job restore downloads). Kept `-1`.
- **Item-3 `pack.compression=0` — rejected (null).** `git init + local fetch`
  12.8s → 12.2s, i.e. unchanged: git reuses already-packed blobs as-is, so the
  override only touched a few loose objects. No benefit.

**Key conclusion:** below level 1, zstd is no longer the pack bottleneck — the
pack step is now **I/O-bound on tar reading the ~2-3 GB tree** (contrast: level
3→1 cut it 190s→58s, when it *was* compression-bound). So **level 1 is the
floor**; pack ~60s is near its minimum given the tree must be read+compressed
once. No more to get from zstd/compression tuning. The remaining large cost is
network (clone fetch ~37s + unshallow ~15s), which #8 targeted — but #8 was tried
and rejected (EBS lazy-load; see below).

### Notes / decisions

- **Write-once dropped intentionally (opt #1).** `IfNoneMatch="*"` is not carried
  by managed multipart. Safe under our threat model: only *trusted* execution
  (main/release = `REFs/` tier) must be protected, and the IAM trust-tier policy
  (`ci/infrastructure/projects.py:_UNTRUSTED_DENY_WRITE_TRUSTED_STATEMENT`) denies
  untrusted `pr-*` pools write/delete/abort on `REFs/*`. PR-vs-PR overwrite inside
  the untrusted `PRs/` tier is an accepted non-threat; the restore-side sha256
  check still fails closed, so no tampered bytes execute. See PR#152 discussion.
- **zstd level is backward-compatible** — decompression auto-detects the level, so
  changing the producer level needs no restore-side change and old archives keep
  working.
- **Parallel checkout (opt #4)** did not visibly help the 31s checkout on
  ClickHouse; kept because it is cheap and helps smaller trees.

## #8 warm git mirror — EVALUATED AND REJECTED (2026-10-01)

Target was the ~52s of network per run (`clone: fetch head` ~37s +
`merge: ensure base history` ~15s). Idea: keep a local **mirror** of the repo's
branches and have the per-task work-repo **borrow its objects via git alternates**
(`.git/objects/info/alternates`), so the head fetch is only the PR delta and the
merge's `--unshallow` is unnecessary (base history is already local). Baked into
the orchestrator AMI (one-shot scale-from-zero instances can't warm on boot in
time).

**Built, deployed, measured — it was a net LOSS and was removed (git reset).**

- **AWS AMI-baked run** (43-branch mirror): clone total **44s → 115s**. The
  unshallow did drop (15s → 0.5s ✓), but `clone: fetch head` 33s → **49s** and
  `clone: checkout head` 11s → **61s**, plus merge/snapshot reads all got slower.
- **Local 1-to-1 test at full ClickHouse scale on warm SSD** (same
  `_attach_local_mirror` code, real `../clickhouse-private`): mirror **fetch 0.7s
  vs cold 13.3s (19× faster), checkout 9.8s vs 8.8s (same), total 2× faster.** So
  the git mechanism is excellent *when the objects are resident*.

**Root cause — EBS lazy-load.** An AMI-backed root volume hydrates from S3
**lazily, per-block, on first access**. The baked mirror's packs are cold blocks;
the moment checkout/fetch-negotiation touches them through alternates, EBS faults
each block in from S3 — *random high-latency reads*, slower than GitHub's *bulk
sequential* packfile download. Baking into a cold one-shot AMI just moves the
download from GitHub to S3-via-EBS and loses. Shrinking the branch set (master
only) would cut mirror size and freshen cost but **not** fix this: checkout still
faults in the current tree's blocks regardless.

**If revisited:** the mechanism only pays off with the mirror **resident on fast
local storage before the task**. Options, all constrained by one-shot
scale-from-zero: a warm reserved pool that pre-reads the mirror during genuine
idle; a **bulk** S3 download + extract at boot (writes resident blocks — avoids
lazy-fault, unlike AMI-bake; still races the task); or Fast Snapshot Restore
(cost per snapshot per AZ). Not pursued.

## #10 idle boot warm-clone (the resident-via-fetch version of #8)

#8 failed only because an AMI-baked mirror lazy-faults from S3. Populating the
same objects by **fetching at boot** writes them as resident EBS blocks, so the
borrow-reads are local — exactly what the local 1-to-1 test showed works. #10 is
that, without a bare mirror or alternates: warm the *work repo's* `.git` directly.

**Mechanism** (`common.warm_default_branch` + `clone_repo` adopt):
- **Boot, when idle (reserved instance), background:** into `WORK_DIR/warm-repo`,
  `git fetch --depth=1 origin <branch>` (branch tip tree+blobs — so the per-task
  head fetch dedups) then `git fetch --unshallow --filter=tree:0 origin <branch>`
  (full history treeless — so the merge needs no per-task unshallow) + **checkout**
  the first branch (local, since its tip tree+blobs are present) so the per-task
  checkout is a delta too. Branch set resolved via `ls-remote` + fnmatch, so globs
  (`release/2*`) expand and non-existent names (a push-branch placeholder) are
  skipped. Token scrubbed from the remote; a ready sentinel written last.
- **Per task:** `clone_repo(warm_dir=…)` adopts a *ready* warm repo —
  `os.replace` it into the clone dir, re-auth origin, `fetch --filter=tree:0
  <head_sha>` (small delta; history present so it doesn't deepen/hang), then
  `checkout -f <head_sha>` + `clean -ffdx` so the worktree is EXACTLY the
  authorized tree (warm base is the trusted branch). No ready sentinel → cold
  clone (no regression). Used for PR **and** push events (push just has no merge,
  so only the head-fetch dedup applies).

**Boot sequence** (orchestrator): self-update → receive once → if a task is
waiting, handle it (no warm) → if idle (and not scaling in), warm once in the
background, keep polling.

**Measured on AWS (reserved instance):** head fetch **33s → 0.47s**, unshallow
**15s → 0.38s**, `clone: total` **44s → 6.5s** (checkout 6s = delta write). With
the overlay removed (#9) runtime also dropped 24s → 4s. Bootstrap **≈340s → 102s**
end to end. Caveat: `fetch <head>` must stay shallow/treeless — a plain non-shallow
fetch into a *shallow* repo deepens the full history and hangs. Adopt correctness
(exact head tree, full history, cold fallback) covered by `test_warm_clone.py`.

**Config.** Enabled + tuned entirely in SSM: `ci_config["repo"]` (owner/name) +
`ci_config["warm_branches"]` (a non-empty list of concrete names or globs, e.g.
`["master", "release/2*"]`) — both required. The repo is a ci_config key because
the controller has no reliable owner/name source at boot. Warm the PR base **and**
push branches (list them in `warm_branches`). Orchestrator-only; only pays off with
reserved capacity (`capacity_reserve > 0`) — cold scale-from-zero falls back to the
cold clone. See ci-config.md.

## #11 run Praktika from the checkout via `PYTHONPATH`

The remaining `runtime: resolve venv + install praktika` step pip-installs the
`./ci` checkout into the base venv on EVERY task (`--force-reinstall --no-deps`,
default build isolation). Praktika is **pure Python** and its runtime deps are
baked into the base venv (the `infrastructure` extra: boto3 / PyJWT / cryptography /
requests), so there is nothing to compile — the install only copies files and
provisions a throwaway build env. `resolve_praktika_runtime` replaces it: for a
local-checkout source it returns the base venv **plus** `PYTHONPATH=<checkout>/ci`,
and the controller runs `python -P -m praktika` with that env, importing Praktika
straight from the tree. URL / version-pin sources and no-source (baked) pools are
unchanged (still installed / baked).

**Why it's correct:**
- **`-P` still honors `PYTHONPATH`.** `-P` only drops the implicit cwd/script-dir
  entry from `sys.path`; `PYTHONPATH` is applied regardless, so `-m praktika`
  resolves from the checkout.
- **Stronger freshness than `--force-reinstall`.** The exact checked-out tree is
  imported by construction, so a run can never execute a stale install.
- **No base-venv mutation.** Nothing is written to the venv, so the per-task file
  lock is unnecessary, and the checkout stays clean — no `ci/build`, no
  `ci/*.egg-info` (the in-tree build artifacts that previously showed up as a dirty
  repo before each job).
- **Minimal path pollution.** `<checkout>/ci` on `sys.path` also exposes `ci`'s
  loose top-level modules, but no `ci/` subdir is a package and Praktika imports
  everything package-qualified (`praktika.*`) + loads project config by file path
  (`importlib.util.spec_from_file_location`), so there is no collision.

Covered by `test_resolve_praktika_runtime_*` in `test_bootstrap_venv_manager.py`.
