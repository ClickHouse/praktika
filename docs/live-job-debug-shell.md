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
praktika-ci.<tailnet>.ts.net   (S3 proxy EC2 box, Caddy :443)
  ├── /<bucket>/<key>   → SigV4 signer (GET/HEAD, private S3 reports)
  └── /term/?arg=<id>   → ttyd ──runs──▶ debug_connect.py <instance_id>
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

- **Caddy — the front door.** Caddy is the only process exposed on the tailnet;
  both backends (ttyd and the SigV4 signer) bind to `127.0.0.1` and are reachable
  *only* through it. Its job:
  - **TLS termination** on `:443` using the Tailscale-issued certificate for the
    node's FQDN (plus plain HTTP on `:8080`). The cert is renewed daily by a
    systemd timer and re-issued whenever the ASG replaces the instance.
  - **Path routing** (reverse proxy): `/term*` → ttyd (`127.0.0.1:7681`),
    upgrading the WebSocket for the interactive terminal; `GET`/`HEAD` on
    `/<bucket>/<key>` → the signer (`127.0.0.1:8081`) for private S3 reports.
  - **Method gating** — everything that is not `/term*` and not a `GET`/`HEAD`
    falls through to a `405`, so the signer only ever sees read requests. The
    `/term*` matcher is more specific than the method matcher, so Caddy evaluates
    it first.

  In short: Caddy is the TLS endpoint + router that fronts the two loopback
  services; it is *not* in the SSH path (that is ttyd → `debug_connect.py`).

- **`s3_proxy_user_data.sh`** — extended to install a static `ttyd` binary and
  `openssh-clients`, inline `debug_connect.py`, run it under a
  `praktika-debug-ttyd` systemd unit (`ttyd -b /term -a -W -m <N>`), and add the
  `handle /term*` route to the Caddyfile described above (Caddy upgrades the
  WebSocket automatically). `-m` is a **global** cap on concurrent sessions
  (see `max_debug_sessions`).

- **`s3_proxy.py`** (`S3Proxy`):
  - `_refresh()` adds two IAM statements: `ec2:DescribeInstances` (Resource `*`,
    the only option it supports) and `ec2-instance-connect:SendSSHPublicKey`
    **`Condition`-scoped to `ec2:ResourceTag/praktika_role = job_runner`**;
  - `deploy()` authorizes the proxy→runner `tcp/22` ingress (self-referencing
    rule on the shared `{vpc}-sg`, mirroring `DockerProxy.deploy`);
  - `max_debug_sessions` (default 5; 0 = unlimited) sets ttyd's `-m`.

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

- **Tailnet-only exposure.** Caddy serves `/term*` only on the tailnet FQDN
  (:443) / :8080 — the same reachability as private reports. No public exposure.
- **Target allow-listing.** `debug_connect.py` refuses any instance that is not
  `state=running` with `praktika_role=job_runner`, and validates the id format.
  The IAM `SendSSHPublicKey` grant is `Condition`-scoped to the same tag, so the
  proxy role cannot key itself, the CIDB, or non-runner instances.
- **No standing credentials.** EC2 Instance Connect keys are ephemeral (~60s),
  minted per session; nothing persistent lands on the proxy or the runners.
- **Ephemeral targets.** Runner pools scale from zero — the instance exists only
  while the job runs, so the shell is unavailable before/after.
- **Concurrency cap.** `max_debug_sessions` bounds simultaneous sessions
  (ttyd `-m`), limiting blast radius and resource use on the proxy.

### Known gap — no per-user identity / attribution

Plain Caddy does **not** know *which* tailnet user opened a terminal. Every
session reaches the runner as the same OS user via the proxy's single IAM role,
so there is currently **no attribution**: the audit trail stops at "someone on
the tailnet". Given this grants `sudo` RCE on runners, that is the most important
limitation to close before broad rollout. Options, in rough order of effort:

- inject Tailscale identity headers (`tailscale serve` adds
  `Tailscale-User-Login`) and log them per session;
- add ttyd basic auth (`-c user:pass`);
- move the backend to **AWS SSM Session Manager**, which gives fully-audited,
  per-principal session logging (kept as the compliance-grade alternative).

Until one of these lands, treat tailnet membership itself as the authorization
boundary and keep the tailnet ACL for the proxy tight.

### Known gap — credentials in EC2 console output

`s3_proxy_user_data.sh` runs under `set -x`, which traces the `curl` calls that
carry the Tailscale **OAuth access token** and the minted **auth key**, so both
land in the instance's EC2 **console output** in cleartext. Both are short-lived
(single-use auth key with a 600s expiry; ~1h OAuth token) and reading console
output requires `ec2:GetConsoleOutput` on the account, but this is still an
avoidable exposure. The fix is to wrap the credential-handling lines in
`set +x` … `set -x` (the same applies to the DockerProxy bootstrap). Tracked as
a follow-up.
