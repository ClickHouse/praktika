# Live job debug: tail a running job's log + interactive shell via the S3 proxy jump host

**Goal.** While a job is still running, let a maintainer open its live `job.log`
and get an interactive shell on the runner — from the praktika HTML report. Today
`job.log` is only visible after the job finishes and its results are uploaded to
S3 (`_ResultS3.upload_result_files_to_s3`, `ci/praktika/result.py:1413`); there is
no mid-run access to the log or the instance.

## Chosen approach

The existing native **S3 report proxy** (`Components.S3Proxy`,
`ci/infrastructure/clickhouse_private.py:242`) becomes a **jump host**. A "Debug"
button on a *running* job's report opens a browser terminal served by the proxy;
the proxy SSHes into the runner and drops the viewer into a shell where they can
`tail -F ci/tmp/job.log` and run any command. A real shell subsumes the one-way
log tail, so we build the shell and treat "tail the log" as just the first thing
you type.

Decisions already made:
- **Transport: proxy-as-jump-host (option A).** Not a per-runner daemon, not a
  periodic S3 push of the live log.
- **Access level: full interactive shell** via `ttyd` (web terminal), not
  read-only tail.
- **Runners are NOT on Tailscale.** Only the proxy is on the tailnet. Proxy →
  runner is reached over the **private VPC network** (they share one VPC / subnet
  / security group — verified below), authenticated with **EC2 Instance Connect**
  ephemeral keys (no long-lived secrets).
- **No AWS SSM Session Manager** backend (kept for a possible audited follow-up).

## Data flow

```
viewer (tailnet member)
  │  HTTPS over tailnet (same trust boundary as private reports today)
  ▼
clickhouseprivate-ci-reports.tail983ac.ts.net  (S3 proxy EC2 box, Caddy :443)
  ├── /<bucket>/<key>   → existing SigV4 signer (unchanged, GET/HEAD, S3 only)
  └── /term/?arg=<id>   → ttyd (new)  ── runs ──▶ debug-connect.sh <instance_id>
                                                    │ 1. ec2:DescribeInstances → private IP, AZ,
                                                    │    verify tag praktika_role=job_runner
                                                    │ 2. ec2-instance-connect:SendSSHPublicKey
                                                    │    (ephemeral 60 s key, user "ubuntu")
                                                    │ 3. exec ssh ubuntu@<private-ip>
                                                    ▼
                                          runner EC2 (Ubuntu 24.04, same VPC)
                                          shell → `tail -F ci/tmp/job.log`, etc.
```

The viewer reaches the proxy exactly the way they already reach private reports,
so no new client-side trust boundary. The proxy reaches the runner over the VPC,
so no Tailscale on runners.

## Why this is feasible (verified topology)

- **Same VPC / subnet / SG.** One project VPC + single `us-east-1a` subnet
  (`ci/infrastructure/clickhouse_private.py:224-234`). Both runner pools and the
  proxy get the same VPC, subnet and default SG `{slug}-vpc-sg` via
  `_apply_pool_defaults` (`ci/praktika/infrastructure/cloud.py:235-276`, called for
  pools at `:263` and for the proxy at `:267`). Private-IP SSH works with no
  peering.
- **Runners are Ubuntu 24.04** (Canonical AMI, `native/image_builder.py:399-428`,
  `native/configs.py:12-13,35-42`), which ships the `ec2-instance-connect` agent →
  `SendSSHPublicKey` to user `ubuntu` works out of the box. No runner-side change,
  no baked-in key. (The runner AMI's `praktika-controller` is out of this repo and
  is untouched by this plan.)
- **Runner instances are tagged** `praktika_role=job_runner`,
  `praktika_resource_tag=runner`, `praktika_pool=<pool>`
  (`native/runner_pool.py:559-576`) — usable to *verify* an SSH target is really a
  runner before connecting.
- **The proxy IAM role + inline policy** are built in `S3Proxy._refresh`
  (`ci/praktika/infrastructure/native/s3_proxy.py:147-189`, policy `S3ProxyAccess`)
  — the exact place to add the two new permissions.
- **Ingress pattern exists**: DockerProxy authorizes a same-SG self-referencing
  ingress in its `deploy()` (`native/docker_proxy.py:302-322`); the same pattern
  opens port 22.
- **The log is already live on disk**: `Runner.run` line-buffers the whole job
  through `_TeeStream` into `Settings.RUN_LOG = ./ci/tmp/job.log`
  (`ci/praktika/runner.py:1312-1373`, `settings.py:89`) — so `tail -F` on the
  runner shows output essentially in real time.
- **The report page already re-renders running jobs**: `checkForUpdatesAndRender`
  (`ci/praktika/praktika.html:2740`) polls while status is `running`/`pending` — a
  natural home for a gated Debug button.

## Work items

### 1. Proxy box: ttyd web terminal + connect wrapper

New files under `ci/praktika/infrastructure/native/`:

- **`debug_connect.sh`** — the per-session wrapper ttyd runs. Takes the instance
  id as `$1`:
  1. Validate `$1 =~ ^i-[0-9a-f]{8,}$` (reject anything else — this arg comes from
     an untrusted URL via ttyd `--url-arg`).
  2. `aws ec2 describe-instances --instance-ids "$1"` → private IP, AZ, state, and
     the `praktika_role` tag. **Refuse unless** state is `running` and
     `praktika_role=job_runner` (prevents pivoting to the proxy, CIDB, or arbitrary
     instances).
  3. `mktemp -d`; `ssh-keygen -t ed25519 -N '' -f $tmp/id`;
     `aws ec2-instance-connect send-ssh-public-key --instance-id "$1"
     --availability-zone "$az" --instance-os-user ubuntu
     --ssh-public-key "file://$tmp/id.pub"` (key valid ~60 s).
  4. `exec ssh -i $tmp/id -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10
     ubuntu@<private-ip>`. Optionally start in the checkout dir and print a hint
     (`tail -F ci/tmp/job.log`) as an SSH remote MOTD/command.
- Extend **`s3_proxy_user_data.sh`** (`ci/praktika/infrastructure/native/`):
  - Download the static `ttyd` binary by arch (mirror the existing Caddy download
    block).
  - Install `debug_connect.sh` to `/opt/praktika-debug/` (base64-inline it in
    `user_data.py`, mirroring `__SIGNER_PY_B64__`).
  - Add a systemd unit `praktika-debug-ttyd.service`:
    `ExecStart=/usr/local/bin/ttyd -p 7681 -i 127.0.0.1 -b /term -a -W
    -t disableLeaveAlert=true /opt/praktika-debug/debug_connect.sh`.
    `-a` = pass URL query args to the command; `-W` = writable (interactive);
    ttyd spawns a fresh process per browser connection, so concurrent viewers on
    different instances are isolated. `Restart=always`.
  - Add the AWS CLI to the box if not already present (the signer only needs
    boto3; the wrapper uses the `aws` CLI, or rewrite the wrapper in Python+boto3
    to avoid the dependency — preferred, keeps parity with the signer).
- Extend the **Caddyfile** block in `s3_proxy_user_data.sh` `(handlers)`:
  add `handle /term* { reverse_proxy 127.0.0.1:7681 }` before the S3 catch-all.
  Caddy's `reverse_proxy` handles the WebSocket upgrade automatically; keep the
  existing `@get_head` route for `/<bucket>/<key>`. The `405` catch-all stays for
  everything else.
- `user_data.py` `s3_proxy_user_data()` (`native/user_data.py:39`): add the new
  placeholders (`__TTYD_ARCH_URL__`, `__DEBUG_CONNECT_B64__`).

### 2. Proxy IAM: two new permissions

In `S3Proxy._refresh` (`native/s3_proxy.py:147-189`), append to `statements`
before the `inline_policies` assignment (`:177`):

```python
statements.append({
    "Sid": "DescribeRunnerInstances",
    "Effect": "Allow",
    "Action": ["ec2:DescribeInstances"],   # only supports Resource "*"
    "Resource": "*",
})
statements.append({
    "Sid": "SendSSHKeyToRunners",
    "Effect": "Allow",
    "Action": ["ec2-instance-connect:SendSSHPublicKey"],
    "Resource": "arn:aws:ec2:*:*:instance/*",
    "Condition": {
        "StringEquals": {"ec2:ResourceTag/praktika_role": "job_runner"}
    },
})
```

`_refresh` is idempotent (called at construction `:145` and from
`set_proxied_buckets` `:191`), so this stays correct across the namespacing pass.

### 3. Proxy security group: open port 22 proxy → runner

`S3Proxy` currently has **no `deploy()`** (only `__post_init__`, `_refresh`,
`set_proxied_buckets`, `report_fqdn`). Add a `deploy()` modeled on
`DockerProxy.deploy` (`native/docker_proxy.py:302-322`) that authorizes a
same-SG self-referencing ingress on tcp/22 (idempotent on
`InvalidPermission.Duplicate`), and invoke it from the deploy orchestration
alongside the DockerProxy/CIDB block (`cloud.py:~1597-1617`).

- Minimal-change option: self-referencing rule on the shared `{slug}-vpc-sg`
  (source = same SG). Works because proxy and runners share that SG; slight
  over-grant (also allows runner↔runner:22 within the CI VPC).
- Least-privilege option (recommended if we touch SGs anyway): give the proxy its
  own dedicated SG and add the rule to the runner SG with the proxy SG as the
  `UserIdGroupPairs[].GroupId`. More infra churn.

### 4. Surface `instance_id` into the running job's report JSON

The client needs the runner `instance_id` to build the Debug link. The
orchestrator already learns it from the heartbeat and sets
`JobState.runner_instance_id` (`orchestrator/state.py`, `sweep_liveness` ~`:1717`,
heartbeat carries `instance_id`). Project it into the per-job row the report reads:

- Add `instance_id` (and optionally `run_id`) to the running job's result `ext`
  when the orchestrator publishes/updates the pending summary
  (`hook_html._build_pending_summary` `:137` / the orchestrator `publish_report`
  path). Clear it (or let it go stale) once the job leaves RUNNING.
- Exact write site to confirm at implementation time — the summary is written in
  several places; pick the orchestrator-owned pending-row path so it is set the
  moment the job flips QUEUED→RUNNING and removed on completion.

### 5. Report UI: the Debug button

In `praktika.html`, gate a button on `status ∈ {running, pending}` **and**
`ext.instance_id` present. Natural site: `addFileLinksWidget`
(`praktika.html:926`) or the status/details widget (`:953`/`:1877`).

```js
// pseudo
if ((status === 'running' || status === 'pending') && ext && ext.instance_id) {
  const a = document.createElement('a');
  a.href = `${window.location.origin}/term/?arg=${encodeURIComponent(ext.instance_id)}`;
  a.target = '_blank';
  a.textContent = '🐞 Debug shell';
  a.className = 'file-link';
  container.appendChild(a);
}
```

`window.location.origin` is already the proxy FQDN (the page is served from the
proxy for private reports), so the link is same-origin. No new config needed
client-side.

## Security

This turns a read-only, tailnet-only feature into **remote code execution (with
passwordless `sudo`) on CI runners**. Controls:

- **Exposure stays tailnet-only.** Caddy serves `/term*` only on the tailnet FQDN
  (:443) / :8080, same reachability as private reports. No public exposure.
- **Target allow-listing.** `debug_connect.sh` refuses any instance that is not
  `state=running` with tag `praktika_role=job_runner`, and validates the id
  format. The IAM `SendSSHPublicKey` is `Condition`-scoped to the same tag. So the
  proxy cannot be used to SSH into itself, CIDB, or non-runner instances.
- **No standing credentials.** EC2 Instance Connect keys are ephemeral (~60 s) and
  generated per session; nothing persistent lands on the proxy or runners.
- **Ephemeral targets.** Runner pools scale from 0; the instance only exists while
  the job runs, so the shell is unavailable before/after the job.
- **Known gap — attribution.** Plain Caddy does not know *which* tailnet user
  connected. If we need per-user audit, add Tailscale identity headers
  (`tailscale serve` injects `Tailscale-User-Login`) and log them, and/or add
  ttyd basic auth (`-c user:pass`). SSM Session Manager remains the fully-audited
  alternative if compliance later requires it. **Flag for review before rollout.**

## Deployment & verification

1. Regenerate nothing under `ci/workflows` (this is infra, not a workflow).
2. `python3 -m praktika infrastructure --deploy --only S3Proxy --project clickhouse-private`
   (updates the IAM role policy, the SG ingress via the new `deploy()`, and the
   launch template / user_data).
3. Roll the single ASG instance so it boots the new user_data:
   `--restart-instances` (instance refresh) or terminate the one ASG instance and
   let it relaunch from the latest launch-template version.
4. `python3 -m praktika infrastructure --verify --only S3Proxy` — extend
   `verify._verify_s3_proxy` (`ci/praktika/infrastructure/verify.py:434`) to also
   check the ttyd port answers (`GET /term/` returns 200 over the tailnet).
   End-to-end (SSH into a real runner) can't be verified without a live job; do it
   manually once against a running job.

## Testing

- Unit-ish: run `debug_connect.sh` locally with a fake instance id → asserts it
  rejects bad ids and non-runner tags (mock `aws` or point at a throwaway tagged
  instance). CI-script tests are throwaway (not committed), per repo convention.
- Manual E2E: start any workflow, open the report while a job is RUNNING, click
  "Debug shell", confirm the terminal opens and `tail -F ci/tmp/job.log` streams
  the same lines that later appear in the uploaded `job.log`.

## Phasing

1. **Infra + shell (backend):** items 1–3. Deliver a working
   `https://<proxy>/term/?arg=<instance_id>` reachable by hand (paste the id).
   No report changes yet — proves the jump-host path end to end.
2. **Report integration:** items 4–5. The button appears automatically on running
   jobs.
3. **Hardening (pre-rollout):** attribution (Tailscale identity headers +
   logging), optional ttyd auth, verify-path check. Resolve the audit gap before
   announcing.

## Open decisions

- **SG scope:** shared-SG self-reference (minimal) vs dedicated proxy SG
  (least-privilege). Recommend dedicated SG.
- **`aws` CLI vs boto3** in `debug_connect.sh`: prefer boto3 to match the signer
  and avoid installing the CLI on the proxy.
- **Attribution before rollout:** is tailnet-membership sufficient, or do we
  require per-user audit (Tailscale identity headers / SSM) from day one?
