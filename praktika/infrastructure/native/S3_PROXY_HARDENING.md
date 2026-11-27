# S3 proxy — incident + hardening notes

Recorded after the 2026-11-18 outage of the **amber** project's S3 report proxy
(`https://amber-ci-reports.tail983ac.ts.net/...` served an **expired TLS cert**,
then "refused to connect").

Relevant code:
- `praktika/infrastructure/native/s3_proxy.py` — the `S3Proxy` component (single-instance ASG + LT + IAM).
- `praktika/infrastructure/native/s3_proxy_user_data.sh` — boot script (Tailscale join, cert, signer, Caddy).

## Incident timeline

- The live instance booted **2026-08-20** and died exactly at the ~90-day
  Tailscale cert lifetime → it was serving an **expired** cert on 2026-11-18.
  The daily `praktika-renew-tailscale-cert.timer` never actually refreshed the
  cert even once.
- **Quick fix applied:** terminated the instance so the min=max=1 ASG replaced
  it (fresh boot mints a fresh 90-day cert).
- The first replacement **collided on the hostname** and came up as
  `amber-ci-reports-1` because the old *ephemeral* tailnet device still held the
  `amber-ci-reports` name. Resolution: deleted both stale tailnet devices via the
  Tailscale API, then rolled again into a clean tailnet so the node reclaimed
  `amber-ci-reports`.
- Break-glass: the proxy role (`praktika-s3-proxy-role`) had no SSM access, so
  the box was unreachable except via the tailnet. `AmazonSSMManagedInstanceCore`
  was **manually attached** to the role during the incident — this is live drift
  vs. `s3_proxy.py` and must be either codified or removed (see below).

## Root problems

### 1. Cert renewal silently never happened

The manual renewal mechanism (`s3_proxy_user_data.sh:125-154`) —
`tailscale cert` run from a daily systemd timer, followed by
`systemctl reload caddy` — never refreshed the boot cert. The instance expired
at exactly the original cert's 90-day mark, i.e. zero successful renewals.
The whole `tailscale cert` + `praktika-renew-tailscale-cert.{service,timer}` +
`systemctl reload caddy` dance is fragile and failed silently.

Related: the boot-time `tailscale cert` call (`s3_proxy_user_data.sh:46-47`)
runs **inline and blocking** before Caddy is installed/started. First issuance
can be slow/flaky, which delays or blocks the proxy coming up at all (observed
as "refused to connect" while boot was stuck at `tailscale cert`).

**Suggested fix:** let **Caddy manage the Tailscale cert itself** via
`tls { get_certificate tailscale }`, giving the Caddy process access to the
`tailscaled` local socket. Caddy then obtains the cert on demand and auto-renews
it in-process before expiry.
- Delete `tailscale cert` from boot, delete the renew script + service + timer.
- Caddy no longer blocks boot on cert issuance; renewal is no longer a separate
  moving part that can silently stop.
- Requires the Caddy service to reach `/var/run/tailscale/tailscaled.sock`
  (run Caddy as root, or grant the `caddy` user socket access).

### 2. Rolling the instance is not safe (hostname collision)

Replacing the instance is the recovery path **and** happens routinely on spot
interruption / ASG health replacement / OOM. But a replacement that joins while
the previous *ephemeral* device is still registered gets bumped to
`amber-ci-reports-N`, silently changing the report URL. This is exactly what bit
us during the quick fix.

**Suggested fix:** in `user_data`, **before `tailscale up`, delete any existing
tailnet device with the same hostname** via the Tailscale API. The script
already holds the OAuth access token at that point
(`s3_proxy_user_data.sh:31-37`), so it can list devices, find the one whose name
== the target hostname, and `DELETE /api/v2/device/{id}` before joining. Then
every replacement deterministically reclaims the clean name.

### 3. No SSM / break-glass access to the box

The proxy role grants only `CloudWatchAgentServerPolicy`, so the only way onto
the instance is the tailnet — useless when the tailnet-facing cert/hostname is
the thing that's broken.

**Suggested fix:** add `AmazonSSMManagedInstanceCore` to
`S3Proxy.ec2_role.policy_arns` in `s3_proxy.py` so Session Manager is always
available as a break-glass path. (This is what was manually attached during the
incident — codify it or detach it; don't leave it as drift.)

### 4. (Defense in depth) No alerting on cert expiry

Nothing warned that the cert was approaching/past expiry; we found out from a
broken link. **Suggested fix:** a CloudWatch alarm on cert days-to-expiry (or a
small periodic check that TLS-handshakes the FQDN and alarms under N days).

## Recommended order

1. **#2 (collision-safe boot)** — highest priority; rolling is the recovery path
   and it's currently broken.
2. **#1 (Caddy-managed certs)** — removes the whole "cert expired" failure class
   and unblocks boot.
3. **#3 (SSM break-glass)** — cheap, codify the already-attached policy.
4. **#4 (expiry alarm)** — defense in depth.

Per project norm, test on **amber** (profile `Amber`, region `eu-west-1`) before
committing deployable changes.
