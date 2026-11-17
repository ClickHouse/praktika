# Live job debug shell

Open an interactive shell on a **running** job's runner — straight from the
praktika HTML report — to tail the live `job.log` and inspect the box while the
job is still executing. Before this, `job.log` and the runner were only reachable
*after* the job finished and uploaded its results to S3.

The feature reuses the native **S3 report proxy** (`Components.S3Proxy`) as an
SSH **jump host**: a "🖥️ debug" link on a running job opens a browser terminal
served by the proxy, and the proxy SSHes into the runner over the private VPC.

## Data flow

```
viewer (tailnet member)
  │  HTTPS over the tailnet (same trust boundary as private reports)
  ▼
praktika-ci.<tailnet>.ts.net   (S3 proxy EC2 box)
  tailscale serve :443  — TLS + injects the Tailscale-User-Login identity header
    └─▶ Caddy (127.0.0.1:8080)  — routes + JSON access log → CloudWatch (audit)
          ├── /<bucket>/<key>   → SigV4 signer (GET/HEAD, private S3 reports)
          └── /term/?arg=<id>   → ttyd ──runs──▶ debug_connect <instance_id>
                                           │ 1. ec2:DescribeInstances → verify the
                                           │    target is a RUNNING job_runner;
                                           │    read its private IP + AZ
                                           │ 2. mint an ephemeral ed25519 key;
                                           │    ec2-instance-connect:SendSSHPublicKey
                                           │    (~60s, no standing secret)
                                           │ 3. ssh <user>@<private-ip> over the VPC
                                           ▼
                                  runner EC2 (same VPC) — interactive shell,
                                  auto-tailing ci/tmp/job.log
```

The viewer reaches the proxy exactly the way they already reach private reports
(tailnet-only), so there is no new client-side trust boundary. The proxy reaches
the runner over the shared VPC, so runners do **not** need to be on Tailscale.

## Components

All under `praktika/infrastructure/native/` unless noted.

- **`debug_connect.py`** — the per-session wrapper ttyd runs with the runner's
  instance id as `argv[1]` (from the untrusted `/term/?arg=<id>` URL). It:
  - validates the id format (`^i-[0-9a-f]{8,}$`);
  - calls `ec2:DescribeInstances` and **refuses** anything that is not
    `state=running` with tag `praktika_role=job_runner` (so the proxy cannot be
    pivoted into itself, the CIDB, or arbitrary instances);
  - mints an ephemeral ed25519 keypair and pushes it with
    `ec2-instance-connect:SendSSHPublicKey`;
  - probes candidate login users (`ec2-user`, then `ubuntu`) — the default user
    differs by pool AMI (AL2023 vs Ubuntu), and `SendSSHPublicKey` accepts any
    user string, so it pushes + probes each and connects as the first that
    authenticates;
  - `exec`s `ssh` into the private IP, starting in the job checkout
    (`/opt/praktika/work/repo`) and tailing `ci/tmp/job.log`; Ctrl-C stops the
    tail and drops to an interactive prompt.

  Host-key pinning is intentionally disabled (`StrictHostKeyChecking=no`,
  `UserKnownHostsFile=/dev/null`): runners are ephemeral and recycle private IPs,
  and trust comes from EC2 Instance Connect + the tag scoping, not host keys.

- **`tailscale serve` — the TLS + identity front.** `tailscale serve --bg 8080`
  owns the node's `:443` on the tailnet: it terminates TLS with a
  Tailscale-managed certificate (nothing to renew) and injects verified
  `Tailscale-User-Login` / `Tailscale-User-Name` headers into every proxied
  request. It only ever listens on the tailnet interface, never the VPC ENI.

- **Caddy — the loopback router + audit log.** Behind `tailscale serve`, Caddy
  listens on `127.0.0.1:8080` only (`:8080 { bind 127.0.0.1 }` — any Host, since
  serve forwards the original FQDN). Its job:
  - **Path routing** (reverse proxy): `/term*` → ttyd (`127.0.0.1:7681`),
    upgrading the WebSocket for the interactive terminal; `GET`/`HEAD` on
    `/<bucket>/<key>` → the signer (`127.0.0.1:8081`) for private S3 reports.
  - **Method gating** — anything not `/term*` and not a `GET`/`HEAD` falls
    through to a `405` (the `/term*` matcher is more specific, so it wins first).
  - **Access logging** — a JSON access log records each request's `uri`
    (`/term/?arg=<runner>`) and the `Tailscale-User-Login` header, shipped to
    CloudWatch for a per-user audit trail (see Attribution below).

  Caddy is `auto_https off` (serve does TLS) and is *not* in the SSH path.

- **`s3_proxy_user_data.sh`** — installs `python3.13` + the praktika wheel (the
  signer and `debug_connect` run as `python3.13 -m
  praktika.infrastructure.native.{s3_proxy_signer,debug_connect}`, not inlined —
  the base AL2023 python3 is 3.9, too old for praktika's `X | None` unions),
  installs a static `ttyd` binary and `openssh-clients`, runs ttyd under a
  `praktika-debug-ttyd` unit (`ttyd -b /term -a -W`), writes the Caddyfile +
  `tailscale serve` + the CloudWatch agent, and writes the session cap to
  `/opt/praktika-debug/max_sessions`.

- **`s3_proxy.py`** (`S3Proxy`):
  - `_refresh()` adds two IAM statements: `ec2:DescribeInstances` (Resource `*`,
    the only option it supports) and `ec2-instance-connect:SendSSHPublicKey`
    **`Condition`-scoped to `ec2:ResourceTag/praktika_role = job_runner`**;
  - `deploy()` authorizes the proxy→runner `tcp/22` ingress (self-referencing
    rule on the shared `{vpc}-sg`, mirroring `DockerProxy.deploy`);
  - `praktika_wheel` — the `pip install` spec the node installs at boot (defaults
    to the version-less `latest` S3 alias; also accepts a pinned wheel URL or a
    PyPI requirement like `praktika==x.y.z`);
  - `cloudwatch_log_group` — where the Caddy access log (the attribution trail)
    is shipped (empty default → CloudInfrastructure derives
    `/{project}/praktika-s3-proxy` from the slug during namespacing);
  - `max_debug_sessions` (default 5; 0 = unlimited) — the global concurrent
    session cap, enforced in `debug_connect` via `flock` slots (atomic across both
    the browser and direct-SSH entry points), not `ttyd -m`.

- **`cloud.py`** — wires `S3Proxy.deploy()` into the deploy orchestration with its
  own manifest key, and adds an `--only S3Proxy` branch to `restart_instances`
  so the proxy can be rolled on its own.

- **Report integration**:
  - `orchestrator/state.py` stamps `ext.instance_id` onto a job's RUNNING report
    row (from `JobState.runner_instance_id`, set off the heartbeat);
  - `praktika.html` renders the "🖥️ debug" link in the status column for RUNNING
    rows at the workflow top level, built from the configured **report proxy base
    URL** (the 🔗 toggle, stored as `privateBaseUrl`). No link appears if that
    URL is unset or the job is not RUNNING.

## Using it

1. In the report, set the report proxy base URL via the 🔗 toggle (the tailnet
   FQDN the private reports are served from, e.g.
   `https://praktika-ci.<tailnet>.ts.net`).
2. While a job is RUNNING, click **🖥️ debug** in its status cell. A browser
   terminal opens, already tailing `ci/tmp/job.log`. Ctrl-C to get a shell.

> The report shows both QUEUED and RUNNING jobs as `running`, but the 🖥️ link
> needs `ext.instance_id` (set only once a runner picks the job up and
> heartbeats). So a `running` row without the link is still queued — there is no
> runner attached yet to debug.

## Deployment & rolling

```bash
# Publish user_data / IAM / SG changes (new launch-template version):
python3 -m praktika infrastructure --deploy --only S3Proxy --project <project>

# A single-instance ASG does NOT auto-replace its instance on --deploy. Roll it:
python3 -m praktika infrastructure --restart-instances --only S3Proxy --project <project>
```

`--verify --only S3Proxy` additionally probes `/term/` for an HTTP 200.

> The proxy is one node on the tailnet under a **stable hostname**
> (`{hostname}.{tailnet}`). Its `user_data` deletes any stale device holding that
> name (or a `-N` variant) before joining and registers a logout-on-shutdown
> unit, so a roll reclaims the same FQDN instead of drifting to `…-1`.

## Security considerations

This turns a read-only, tailnet-only report proxy into a path to **remote code
execution (with passwordless `sudo`) on CI runners**. Controls in place:

- **Tailnet-only exposure.** `tailscale serve` exposes `:443` only on the tailnet
  FQDN — the same reachability as private reports. No public exposure.
- **Runners cannot reach the proxy's terminal (the reverse path).** This matters
  because runners execute untrusted PR code. Two independent layers stop a runner
  from using the jump host against other runners:
  1. The proxy's security group does **not** open 443/8080 to the VPC (its only
     same-SG inbound ports are 22/9000/8123, and nothing listens on 9000/8123;
     sshd on 22 has no runner-usable credentials).
  2. `tailscale serve` only listens on the node's tailnet interface, and Caddy +
     ttyd + the signer all bind to `127.0.0.1`. So nothing in the terminal path
     listens on the VPC ENI at all — it is reachable solely over the tailnet
     overlay, which runners are not on. Also forces every `/term` request through
     `tailscale serve`, so none can skip the identity header.
- **Target allow-listing.** `debug_connect.py` refuses any instance that is not
  `state=running` with `praktika_role=job_runner`, and validates the id format.
  The IAM `SendSSHPublicKey` grant is `Condition`-scoped to the same tag, so the
  proxy role cannot key itself, the CIDB, or non-runner instances.
- **No standing credentials.** EC2 Instance Connect keys are ephemeral (~60s),
  minted per session; nothing persistent lands on the proxy or the runners.
- **Ephemeral targets.** Runner pools scale from zero — the instance exists only
  while the job runs, so the shell is unavailable before/after.
- **Concurrency cap.** `max_debug_sessions` bounds simultaneous sessions,
  enforced in `debug_connect` via `flock` slots, limiting blast radius and
  resource use on the proxy.
- **Per-user attribution.** `tailscale serve` authenticates the tailnet peer and
  injects a verified `Tailscale-User-Login` header; Caddy's JSON access log
  records it alongside the request `uri` (`/term/?arg=<runner>`) and ships to
  CloudWatch (`cloudwatch_log_group`). So every debug session — and every report
  fetch — is attributed to a specific person: *who → which runner → when*. (AWS
  SSM Session Manager remains the compliance-grade alternative if full
  per-principal **session content** recording is ever required.)

### Known gap — credentials in EC2 console output

`s3_proxy_user_data.sh` runs under `set -x`, which traces the `curl` calls that
carry the Tailscale **OAuth access token** and the minted **auth key**, so both
land in the instance's EC2 **console output** in cleartext. Both are short-lived
(single-use auth key with a 600s expiry; ~1h OAuth token) and reading console
output requires `ec2:GetConsoleOutput` on the account, but this is still an
avoidable exposure. The fix is to wrap the credential-handling lines in
`set +x` … `set -x` (the same applies to the DockerProxy bootstrap). Tracked as
a follow-up.
