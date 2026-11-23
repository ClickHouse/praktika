# Runner disk: fast mount options (measured)

**Goal.** Close the measured fsync gap vs GitHub-hosted runners by mounting the
disposable runner volume with GitHub's durability-sacrificing options. No
hardware change, no instance-type change.

## GHA uses the same class of disk — the win is mount options, not NVMe

GitHub-hosted runners are Azure VMs; their root disk is an Azure virtualized,
SSD-backed disk (reports as `sda`, model "Virtual Disk", ext4). That's the same
*category* as our EBS — network/hypervisor-mediated, **not** instance-local NVMe.
So the measured gap below is not a hardware gap; it's entirely the mount flags.

## Measured (2× idle Praktika runners, gp3 EBS, same `dd` probe GHA used)

| Metric | Praktika | GHA 32-core |
|---|---|---|
| Root mount | `rw,relatime,discard,commit=30` (default `data=ordered`, barriers **on**) | `…nobarrier,data=writeback,journal_async_commit,commit=30` |
| fsync 4k `oflag=dsync` | **~3.0 ms/op** | ~0.28 ms/op (**~10×**) |
| create+fsync small files | **~3.2 ms/file, ~310/s** | sub-ms |

Reproducible across both runners. Impacts anything fsync-heavy: git/Go object
writes, docker layer extraction, Postgres commits.

## What the flags do

- **`nobarrier`** — stops forcing the storage device to flush its write cache on
  every journal commit. That forced flush is what makes `fsync` a full durable
  round-trip to EBS (~3 ms — the 10× cost). Without it, `fsync` returns as soon
  as the write is acked into the device cache (~0.3 ms). Trade-off: on power
  loss the journal/filesystem can be left inconsistent.
- **`data=writeback`** — ext4 still journals metadata, but no longer forces file
  *data* to disk before the metadata that references it. Filesystem stays
  consistent after a crash; only files mid-write at crash time can contain stale
  blocks.
- **`commit=30`** (already set) — flush the journal every 30 s instead of 5 s;
  widens the window of un-fsync'd data lost on crash. No corruption.

## Why it's safe on CI runners

These guarantees only matter on unclean power loss, and a crashed Praktika runner
is worthless by definition: the instance is disposable and replaced (never
fsck'd and reused), an interrupted job is re-run from scratch, and artifacts go
to S3 — the local disk is never the thing we protect. Single-tenant, so
`data=writeback`'s stale-block exposure can't leak across users. This is exactly
GitHub's reasoning for mounting their ephemeral runners the same way.

**Not for stateful hosts** — the CIDB server (`/var/lib/clickhouse`), any
database of record, or any volume meant to survive a crash and be re-mounted.
Precondition for any pool enabling this: a crashed runner is terminated, not
resurrected (true for Praktika's churned pools).

## Implementation

- `nobarrier` + `commit=30` can be applied by `mount -o remount` (boot hook).
- `data=writeback` **cannot** be weakened on an already-mounted root; set it at
  AMI-build time via `tune2fs -o journal_data_writeback` on the root fs + fstab
  options (mirrors the existing `user_data_cidb.sh:57` fstab pattern and the
  `image_builder.py` daemon.json edits).
- Testing: AMI change → Image Builder rebuild + boot verification on a real
  instance before commit (slow turnaround).

