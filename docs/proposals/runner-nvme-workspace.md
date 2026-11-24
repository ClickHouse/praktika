# Runner local-NVMe workspace (instance store for workspace + docker)

**Goal.** Move the IO-heavy paths off the single root EBS volume onto local
NVMe instance store, for IO-bound pools. Opt-in per pool, one AMI, EBS fallback.

**Not "instead of EBS".** Nitro boots from EBS (AMIs are `ebs-gp3`,
`native/configs.py:10-13`). Keep the small EBS root; put the **workspace** and
optionally **docker data-root** on instance-store NVMe.

## IO-heavy paths today (all on root EBS)
- Workspace `/opt/praktika/work` — clone, warm-clone cache, repo snapshots
  (`WORK_DIR`, `controller.py:57`). Job temp `.../repo/ci/tmp` is relative to
  the clone cwd, so it follows `WORK_DIR` automatically (`settings.py:116`).
- Docker `/var/lib/docker` — default, `data-root` unset
  (`image_builder.py:30/97`). The `docker pull` hog lives here (`docker.py:203`).

## Design — bake into the AMI, gate with an instance tag

The AMI's value is **systemd ordering**, not the path. Baking `data-root` alone
races dockerd (starts before any mount). Instead bake:

1. **Oneshot** `praktika-nvme-setup.service` (`Type=oneshot`,
   `RemainAfterExit=yes`) running `/usr/local/bin/praktika-setup-nvme`:
   discover instance-store disks → RAID0 if >1 → mkfs-if-blank/mount at
   `/mnt/nvme` → `mkdir -p /mnt/nvme/{docker,work}`.
2. **Docker drop-in** `/etc/systemd/system/docker.service.d/10-nvme.conf`:
   `[Unit]\nAfter=praktika-nvme-setup.service\nRequires=praktika-nvme-setup.service`.
   Set `"data-root":"/mnt/nvme/docker"` in the baked `daemon.json`.
3. **Controller drop-in** `After=praktika-nvme-setup.service`,
   `Environment=WORK_DIR=/mnt/nvme/work`.

Result: dockerd + controller cannot start until NVMe is mounted. **user_data
unchanged.**

### Tag gate (one AMI, default off)
IMDS tags are already on (`launch_template.py:268-271`); pools already toggle
baked behavior via `ext["host_metrics"]`/`ext["system_logs"]` → instance tags.
Mirror it:
- `praktika-setup-nvme` reads tag `praktika_nvme_workspace` from IMDS.
  **Absent / no NVMe present →** oneshot `mkdir -p /mnt/nvme/{docker,work}` on
  root EBS and exits 0 (byte-identical to today, just a different path).
  **Present →** format/mount NVMe.
- Add `nvme_workspace: bool = False` to `RunnerPool.Config`, sets the tag. Same
  shape as the existing `host_metrics` flag.

## Implementation checklist
- [ ] `image_builder.py`: write `/usr/local/bin/praktika-setup-nvme`, the two
      systemd drop-ins, set `data-root` in `daemon.json`, `systemctl enable`
      the oneshot. (Both AL2023 + Ubuntu daemon.json branches.)
- [ ] `runner_pool.py`: `nvme_workspace` flag → instance tag (follow
      `ext["host_metrics"]` plumbing).
- [ ] Enable on one IO-bound pool in `ci/infrastructure/projects.py`.

## Hard constraints (get these right or it's a silent dead runner)
1. **Device selection by model, NOT device index.** EBS also enumerates as
   `/dev/nvme*` on Nitro; the CIDB `exclude nvme0` heuristic is unsafe here.
   Match only `Amazon EC2 NVMe Instance Storage` (`lsblk -d -o NAME,MODEL` or
   `/dev/disk/by-id/nvme-Amazon_EC2_NVMe_Instance_Storage_*`). Never touch any
   other disk.
2. **Oneshot must always exit 0.** A non-zero exit blocks docker + controller →
   dead runner (CLAUDE.md failure mode). Wrap optional steps, log loudly, fall
   back to the EBS dir.
3. **Reboot-safe.** Instance store survives reboot (not stop/terminate):
   "has a filesystem? mount it : mkfs then mount" — never blind-mkfs.

## Notes
- Ephemerality is a non-issue: runners are already churned; warm-cache/snapshots
  rebuild; OOM/system-log evidence ships to CloudWatch, not disk.
- Needs an instance type with instance store (`*d` families, `i4i`, `im4gn`).
- **Testing: AMI change** — Image Builder rebuild + deploy on a real
  instance-store type, verify boot/mount/data-root AND the no-NVMe fallback,
  before commit. Image Builder turnaround is slow.
- Cheaper alternative first: the gp3 `volume_iops`/`volume_throughput` knobs on
  `RunnerPool.Config` (up to 16000 IOPS / 1000 MiB/s) — zero boot complexity.
  Reach for NVMe only when gp3 ceiling isn't enough.
- If `docker pull` isn't the bottleneck, workspace-only NVMe (skip docker
  relocation) avoids the dockerd-ordering concern entirely.
