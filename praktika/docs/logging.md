# Praktika instance logging & CloudWatch

This document describes how logs from praktika controller/runner EC2 instances
reach CloudWatch, and the opt-in **system-log streamer** that captures why the
controller process can be killed without leaving a trace in its own log (e.g.
an OOM kill).

All of the wiring below is baked into the AMI at build time in
[`praktika/infrastructure/native/image_builder.py`](../praktika/infrastructure/native/image_builder.py).
Changing it requires an image rebuild, not just a redeploy of `user_data`.

## What ships to CloudWatch

At boot, `user_data` runs the baked `praktika-configure-cloudwatch-agent`
script, which writes `/etc/praktika/amazon-cloudwatch-agent.json` and then the
CloudWatch agent tails the files listed there. Log groups are named from the
`praktika_project_slug` instance tag; the stream is the instance id.

| File | Log group | Always shipping? |
|---|---|---|
| `/var/log/praktika-controller.log` | `/{slug}/praktika-controller` | Yes — the controller's own stdout/stderr |
| `/var/log/praktika-system.log` | `/{slug}/praktika-system` | Only when the streamer is activated (see below) |

The `praktika-controller.service` systemd unit redirects the controller's
stdout/stderr to `/var/log/praktika-controller.log`. That covers everything the
controller logs itself — but **not** the reason it dies when something external
kills it.

## The system-log streamer (kernel / OOM / systemd-kill evidence)

When the OS OOM-killer (or systemd, or `systemd-oomd`) kills the controller,
the evidence is written to the **systemd journal by the kernel and by PID 1** —
never by the controller, which cannot log its own `SIGKILL`. The CloudWatch
agent only tails files, not the journal, so that evidence never leaves the box.

To capture it, the image bakes:

- **`/usr/local/bin/praktika-system-log-stream`** — a small script that runs
  `journalctl --follow`, filtered to exactly the kill/OOM signal and nothing
  else:
  - `_TRANSPORT=kernel` — the kernel OOM killer ("Out of memory: Killed process …")
  - `_PID=1` — systemd manager notices ("Main process exited, code=killed, status=9/KILL", "killed by the OOM killer")
  - `_COMM=systemd-oomd` — userspace OOM daemon (Ubuntu)
  - `UNIT=` / `_SYSTEMD_UNIT=praktika-controller.service` — anything systemd logs about, or the controller's cgroup emits under, the unit

  It uses `--cursor-file` so it resumes cleanly across restarts. The filter keeps
  volume tiny — this is not a full-journal firehose.
- **`praktika-system-logs.service`** — a `Restart=always` systemd unit that runs
  the script and appends to `/var/log/praktika-system.log`.
- The `/var/log/praktika-system.log` entry in the CloudWatch collect list.

All three are baked into **every** image. The unit is **not** enabled at build
time — it stays off until a pool opts in. When off, the streamer never runs,
the file stays empty, and no CloudWatch stream is created, so there is no cost.

## Activation: the `praktika_system_logs` instance tag

Activation is per-pool at boot, driven by the `praktika_system_logs` instance
tag. The `praktika-configure-cloudwatch-agent` script reads it via IMDS and:

- truthy (`1`/`true`/`yes`/`on`/`enabled`) → `systemctl enable --now praktika-system-logs`
- anything else / absent → `systemctl disable --now praktika-system-logs`

No `user_data` change is needed — the configure script is already invoked at
boot on every instance.

### Turning it on for a pool

Set `ext["system_logs"]` on the pool config; this adds the
`praktika_system_logs` tag to the launch template and ASG:

```python
RunnerPool(
    name="arm-small",
    instance_type="m8g.2xlarge",
    ...,
    ext={"system_logs": True},
)

OrchestratorPool(
    instance_type="t4g.small",
    size=1,
    max_size=1,
    ext={"system_logs": True},
)
```

Instances launched after the change (or replaced instances) will start the
streamer at boot. Because activation is a tag read at boot — not baked into the
image — flipping it does **not** require an AMI rebuild.

## Investigating a suspected OOM / silent restart

1. Confirm the pool has `ext["system_logs"]` set (otherwise the trace was not
   being collected at the time — enable it for future incidents).
2. Look at the `/{slug}/praktika-system` log group, stream = instance id, around
   the restart time. OOM kills show up as kernel `Out of memory: Killed process`
   lines and/or systemd `code=killed, status=9/KILL`.
3. Cross-reference `/{slug}/praktika-controller` for the same instance/time to
   see what the controller was doing right before it was killed.

See also the runner-instance boot-failure diagnostics in the repo root
[`CLAUDE.md`](../CLAUDE.md).

## Known limitations

- **Growth / rotation.** `/var/log/praktika-system.log` (like
  `/var/log/praktika-controller.log`) is append-only with no `logrotate` entry.
  Volume is low given the journal filter, but long-lived instances would
  benefit from rotation.
- **Pool-scoped, not workflow-scoped.** The log group is derived from the
  per-instance `praktika_project_slug` tag, and instances are reused across
  workflows, so system logs cannot be separated per pipeline. A per-pipeline
  toggle would be a larger structural change (the host controller does not read
  `Workflow.Config`).
