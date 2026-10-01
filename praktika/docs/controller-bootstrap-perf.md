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
| 8 | **Warm / AMI-baked git mirror** — persistent blob-full mirror of the default branch; per-task fetch becomes a delta (`--reference`/alternates) | PLANNED | cut ~43s network (fetch 29 + unshallow 14) | — |
| 9 | **Overlay prebake in AMI** — pre-copy base venv → overlay at image-build time | PLANNED | save ~12s first-task overlay copy | — |

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
once. No more to get from zstd/compression tuning. The next addressable cost is
**#8 (warm/AMI mirror)** — clone fetch ~37s + unshallow ~15s of network.

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
  ClickHouse; kept because it is cheap and helps smaller trees. The real clone
  lever is the warm mirror (#8).

## #8 design: warm git mirror (opt-in, warm-on-startup)

**Status: DESIGN.** Target: the ~52s of network on every orchestrator run —
`clone: fetch head` (~37s) + `merge: ensure base history (unshallow + fetch
base)` (~15s). Both are large because a cold work-repo downloads the whole
~2-3 GB tree from GitHub every task. A PR only changes a few files, so if the
instance already holds the default branch locally, both collapse to a small
delta.

### Mechanism: alternates (object borrowing)

A git repo stores objects under `.git/objects`. The file
`.git/objects/info/alternates` lists *other* object directories git also reads
from, so a repo can **borrow** another's objects instead of copying them
(`git clone --reference` sets this up). If a local **mirror** of the default
branch (full history + blobs) exists, a per-PR work-repo pointed at it via
alternates already "has" ~the whole tree; fetching the PR head then transfers
only the changed objects, and the merge's `--unshallow` is unnecessary (base
history is already local).

Hazard: borrowing is a pointer, not a copy — if the mirror prunes/GCs reachable
objects while a work-repo borrows them, the borrow breaks. The design never
prunes the mirror (append-only fetches; `gc.auto=0`), which keeps borrows valid.
`--dissociate` (copy borrowed objects into the work-repo) is the safe fallback if
we ever need to prune, at the cost of the copy.

### Approach: bake the mirror into the AMI

Orchestrator instances are **one-shot** (boot -> poll -> handle one task ->
terminate), so warm-on-startup would lose the race: the initial mirror clone takes
minutes, but the single SQS message can arrive seconds after boot. The mirror must
therefore already be present at boot -> **bake it into the AMI**.

- **Bake:** an EC2 Image Builder component clones the default branch as a bare,
  blob-full, full-history mirror into `/opt/praktika/git-mirror/<repo>.git` during
  image build. The baked mirror is as of image-build time.
- **Boot freshen (best-effort):** on controller start, a time-boxed, non-blocking
  `git -C mirror fetch` pulls the (small) delta of commits since the AMI was
  baked, using the runner's existing GitHub token. If a message arrives first, the
  per-task clone just uses the slightly-staler baked mirror — still a tiny delta
  vs a full cold clone. Never blocks the poll loop.
- Per-task clone borrows from the mirror via alternates (next section).

**Build-time auth — resolved via the token-minter lambda.** `clickhouse-private`
is private, so the Image Builder build instance needs GitHub credentials to clone
it at bake time. The existing `GitHubTokenMinter` lambda (same one the controller
uses) mints a short-lived installation token; `GitHubTokenMinter.grant_invoke()`
grants a role permission to call it. So: grant the Image Builder build role invoke
on the minter, and the bake component invokes it for a token, clones, done — no
long-lived secret on the build host. One consequence to record: the AMI artifact
then contains the source tree — acceptable for a private project (runners already
clone it), but a deliberate choice. Public repos (ClickHouse/ClickHouse) need no
auth.

**Alternative if build-time auth is a blocker — S3-distributed mirror.** A single
warmer (cron/job) maintains the bare mirror and uploads a compressed tarball to
S3; the AMI bakes nothing repo-specific, and at boot the instance downloads the
tarball from S3 (in-region, ~seconds for a few GB) + a small GitHub delta fetch.
Sidesteps build-time auth, keeps the AMI generic, and decouples mirror freshness
from the AMI rebuild cadence. Boot download is short enough that most one-shot
instances get it warm; a message arriving first falls back to the cold path (no
regression). Noted as a fallback; primary plan is the AMI bake above.

### Per-task wiring (`clone_repo`, when mirror enabled)

1. `git init work/repo`; set `objects/info/alternates` -> the mirror's objects.
2. `git remote add origin <github>`; `git fetch origin <head_sha>` — negotiation
   counts the borrowed objects as "have", so GitHub sends only the PR delta.
   (Still pinned to the authorized `head_sha` — security model unchanged.)
3. Merge: `fetch origin <base_branch>` is a small delta; `--unshallow` is skipped
   because full base history is borrowed from the mirror. Merge-base is local.

Everything downstream (ephemeral merge, snapshot build) is unchanged. The
snapshot still ships a self-contained minimal `.git` (the alternates are a
build-time optimization of the work-repo, not something the snapshot references).

### Trust & safety

- **Trusted content only.** The mirror only ever fetches the default branch
  (never PR/fork refs), so it holds only trusted objects. Work-repos borrow
  read-only and fetch the untrusted head *on top* into their own object store;
  nothing writes back into the mirror, so a fork PR cannot poison it. The base is
  trusted content we already clone today — no new exposure.
- **No pruning while borrowed** (above). Refreshes only add objects.
- **Disk.** The mirror is a full clone (several GB for ClickHouse) plus the
  per-task work-repo. Reserved instances need headroom; size the volume for it.

### Opt-in via infra config

Off by default. A pool-level `ext` flag (e.g. `warm_git_mirror: true`, plus the
repo + default branch the pool serves) enables both the background warm-up and
the `clone_repo` alternates wiring. Only pools that opt in pay the disk/warm-up
cost.

### Expected savings & open questions

- Expected: most of `fetch head` (~37s) + `unshallow` (~15s) -> a few seconds of
  delta, on warm instances. Snapshot pack (~60s, I/O-bound) is unaffected.
- Open: refresh cadence while idle (keep the delta small vs churn); interaction
  with instance lifetime (one-shot vs long-lived); exact `clone_repo` changes to
  the depth/unshallow logic; whether to measure a `mirror: warm/refresh` profile
  line; eventual AMI-bake for cold-scale.
