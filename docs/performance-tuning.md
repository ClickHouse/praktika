# Performance tuning

Tunings praktika bakes in, and their trade-offs.

## Runner disk: ext4 fast-mount

Runner AMIs set `journal_data_writeback,nobarrier` as ext4 superblock defaults
(via `tune2fs` at image-build time, `image_builder.py`). This makes `fsync`
nearly free (~3 ms → ~0.3 ms measured), speeding up git/Go object writes, docker
layer extraction, and Postgres commits.

- **Safe because runners are disposable:** the weakened durability only matters
  on an unclean crash, and a crashed runner is terminated and replaced, its job
  re-run, artifacts live in S3. Single-tenant, so stale blocks never leak. Not
  for stateful hosts (e.g. the CIDB server).
- **ext4 only.** XFS (AL2023 roots) has no equivalent: it never journals data
  and dropped the `nobarrier` option. Those pools keep default mount (~2 ms
  fsync); use an ext4 (Ubuntu) root or the local-NVMe lever
  ([proposals/runner-nvme-workspace.md](proposals/runner-nvme-workspace.md))
  if that matters.
