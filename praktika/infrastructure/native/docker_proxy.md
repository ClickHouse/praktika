# Native DockerHub Proxy (`DockerProxy`)

A single-instance DockerHub pull-through cache backed by S3, running `zot` (an
OCI-native registry) as one process. Deployed as a native praktika infrastructure
component (`Components.DockerProxy`).

```
runner --[registry-mirror]--> zot (:5000) --> DockerHub (first pull of a tag/blob only)
                                 |
                                 v
                       S3 (manifests + blobs)
```

## Why zot, one process

`zot` serves DockerHub images at their **native** `library/...` paths, so Docker's
`registry-mirrors` works with **no image-reference rewrites**. It stores manifests
*and* blobs in S3 natively, and with `manifestCheckInterval` set it serves cached
content **without re-contacting DockerHub**.

A `registry:2`-based pull-through cache re-validates every manifest tag against
DockerHub on every pull (flooding `auth.docker.io` → HTTP 429), which is why such
setups need an `nginx` manifest cache in front. `zot` + `manifestCheckInterval`
removes that revalidation, so a single process does the whole job.

## How a pull works

1. Runner requests the manifest by tag. Within `manifestCheckInterval` of the last
   check, `zot` serves it from S3 and DockerHub is **not** contacted. Otherwise
   `zot` fetches it from DockerHub once, stores it, and serves it.
2. Runner requests each blob by digest. Blobs are content-addressed, so once in S3
   they are served from S3 and never re-fetched.

DockerHub is contacted only on the first pull of a tag/blob, plus one manifest
re-check per tag after a process restart (the last-check time is in memory). With
pinned tags/digests this is bounded and small. The sync content filter is `**`, so
**every** repository is cached on demand, not only a specific namespace.

## Files

- `docker_proxy.py` — the `DockerProxy` dataclass: builds an `AutoScalingGroup`
  (`min=max=desired=1`), `LaunchTemplate`, `IAMRole` and instance profile, and a
  `deploy()` hook that creates the private hosted zone and the SG ingress rule.
- `docker_proxy_user_data.sh` + `user_data.docker_proxy_user_data` — the boot
  script (installs the `zot` binary, renders config, self-registers DNS).
- Wired into `cloud.py` (config field, namespacing, registration + `deploy()` call),
  `verify.py` (`--verify`), and exported as `Components.DockerProxy`
  (`native/__init__.py`).

## Configuration

```python
docker_proxy=Components.DockerProxy(
    instance_type="c7g.large",              # Graviton (arm64)
    s3_bucket="my-docker-mirror",           # S3 bucket for manifests + blobs
    s3_region="us-east-1",
    dockerhub_pat_ssm="/ci/docker/dockerhub-readonly-pat",  # SSM SecureString
    dns_zone="ci.internal",                 # private zone the proxy owns
    dns_record="dockerhub-proxy.ci.internal",
    enable_ui=True,                          # zot web UI at / (optional)
).configure_tailscale(                       # optional: expose the UI over Tailscale
    hostname="dockerhub-proxy",
    tag="tag:ci",
    oauth_client_id_ssm="/tailscale/api-client-id",
    oauth_client_secret_ssm="/tailscale/api-client-secret",
)
```

Key fields:

| Field | Default | Notes |
|---|---|---|
| `instance_type` | `c7g.large` | Graviton; the family must end in `g` for the launch-template AMI resolver to pick arm64 (it misses the `gn`/`gd` variants). |
| `zot_version` | `v2.1.21` | Minimum — `manifestCheckInterval` was added here. |
| `s3_bucket` / `s3_region` | — | Mirror storage. Reused across instance replacements, so a fresh node boots warm. |
| `s3_rootdirectory` | `/zot` | S3 key prefix; isolates zot's layout from anything else in the bucket. |
| `manifest_check_interval` | `168h` | Window a cached tag is served without re-checking DockerHub. |
| `dockerhub_pat_ssm` / `dockerhub_username` | — | DockerHub read-only PAT (SSM SecureString) and its account, read at boot into zot's sync credentials file. |
| `dns_zone` / `dns_record` | — | Private zone + record the instance self-registers and runners point their registry-mirror at. |
| `listen_port` | `5000` | Registry port; runners mirror to `http://<dns_record>:<listen_port>`. |
| `enable_ui` | `False` | Serve zot's web UI at `/` (see below). |

`dns_zone`, `dns_record`, `s3_bucket` and `dockerhub_pat_ssm` are external
identities and are **not** project-namespaced; the IAM role, profile, launch
template and ASG are.

## Web UI and access (optional)

`enable_ui=True` turns on zot's `search` + `ui` extensions, served at `/` on the
same `listen_port` (the registry API stays at `/v2/`). It's lightweight — same
binary, no CVE/trivy scanning.

The proxy has no public/VPC ingress beyond `listen_port` from the runner SG, so to
reach the UI from a browser, call `configure_tailscale(...)` — it joins the node to
Tailscale and serves the registry at root over HTTPS:

```python
docker_proxy.configure_tailscale(
    tag="tag:ci",                                   # required
    oauth_client_id_ssm="/tailscale/api-client-id", # required
    oauth_client_secret_ssm="/tailscale/api-client-secret",  # required
    hostname="dockerhub-proxy",                      # optional, defaults to name
)
```

- `hostname` is the Tailscale machine name → MagicDNS name, so the UI is at
  `https://{hostname}.<tailnet>.ts.net/`. It defaults to the component `name` and,
  being a tailnet identity, is not project-namespaced.
- At boot the node mints a **tagged, ephemeral** auth key from the SSM-stored OAuth
  client, runs `tailscale up --ssh` (also giving node SSH access), and
  `tailscale serve`s `listen_port` at root over HTTPS (TLS handled by Tailscale — no
  cert management). No static credentials are written to disk.
- The instance role is granted SSM read on the two OAuth-client parameters. The
  OAuth client must be authorized to mint keys for `tag` **and** to delete devices
  (for the stable-name handling below).
- **Stable MagicDNS name across rolls.** Ephemeral Tailscale devices linger after a
  node dies until Tailscale GCs them, so a naive re-roll would collide and the new
  node would take `{hostname}-1`, breaking the URL. Two mechanisms keep the name
  stable: (1) a `tailscale logout` `ExecStop` unit deregisters the node on graceful
  shutdown; (2) at boot, before joining, the new node deletes via the Tailscale API
  any existing device holding `{hostname}` or a `{hostname}-N` variant (covers
  crashes where logout never ran). So a replacement always reclaims the clean name.

Without Tailscale, the UI is still reachable by port-forwarding through any tailnet
node in the same VPC: `ssh -L 5000:<dns_record>:5000 <node>` then open
`http://localhost:5000/`.

## zot configuration (rendered at boot)

Settings that must be present (see `docker_proxy_user_data.sh`):

- `extensions.sync[].manifestCheckInterval` — without it zot re-checks DockerHub on
  every by-tag pull (~2–3 s latency and defeats the no-upstream-on-cache goal);
  with it, cached tags serve in ~2–3 ms.
- `http.compat: ["docker2s2"]` — zot is OCI-first and rejects Docker V2 Schema 2
  manifests by default; DockerHub multi-arch images need this.
- `extensions.sync` with `onDemand: true`, `content.prefix: "**"`, **no**
  `destination` (→ native `library/...` paths), and **no** polling (the zot docs
  warn against polling DockerHub — rate limits, no catalog listing).
- `storage.storageDriver.name: s3` (S3 accessed via the EC2 instance role);
  `dedupe: false` (no cache driver needed for a single instance); `gc: true`
  (safe with a single writer).

Under high concurrency, `reqConcurrent` / `reqPerSec` (per-host upstream caps) and
`disableHTTP2` are the knobs if DockerHub throttles.

## IAM

The instance role grants:

- S3 read/write on the mirror bucket (`Get/Put/Delete/List` + multipart) — zot
  populates the mirror.
- `ssm:GetParameter(s)` on the DockerHub PAT parameter.
- `route53:ChangeResourceRecordSets` (self-registration) + `ListHostedZonesByName`.
- `CloudWatchAgentServerPolicy` (managed) for logs/metrics.

## Single instance and reboots

One instance is sufficient: Docker's `registry-mirrors` is best-effort, so if the
proxy is briefly unavailable during a replace the daemon **falls back to pulling
directly from DockerHub** — degraded, not broken. The cache lives in S3, so a
replacement boots warm. The instance UPSERTs its A record on boot and deletes it on
shutdown; a plain (non-multivalue) record means a replacement overwrites a stale
one. To scale later, raise the ASG capacity — but note the manifest last-check state
is per-instance in memory, so multiple instances each re-check a tag once after
their own restart.

## Deploy

The proxy is **fully deployable by `--deploy`** — no manual setup. A full
`python3 -m praktika infrastructure --deploy` runs the passes in order: VPC → IAM
role/profile → `DockerProxy.deploy()` → LaunchTemplate → ASG (launches the
instance). `DockerProxy.deploy()` runs before the instance launches and owns the
two things the standard passes don't:

1. **The private hosted zone.** It creates `dns_zone` (a private Route53 zone
   associated with the VPC) if missing, or associates an existing private zone of
   that name. praktika has no Route53-zone resource, so the component owns it.
2. **The SG ingress** for `listen_port` on the shared SG (runner → proxy).

The instance self-registers `dns_record` at boot (the zone exists by then) and
deletes it on shutdown.

```bash
python3 -m praktika infrastructure --deploy                     # full deploy
python3 -m praktika infrastructure --deploy --only DockerProxy  # just this component
python3 -m praktika infrastructure --verify --only DockerProxy  # verify
```

`--only DockerProxy` deploys the **complete** component — its IAM role/profile,
launch template, ASG, private zone and SG ingress — so it can be rolled out (or a
config change picked up) without touching other pools. A full `--deploy` also
deploys it, via the generic resource passes.

`--verify` checks the PAT is readable from SSM, the mirror bucket is reachable, and
the ASG has an `InService` instance.

The only external prerequisites are the SSM parameter holding the DockerHub PAT and
the S3 mirror bucket.

Note: like every praktika pool, the ASG does not auto-replace a running instance
when only the launch template changes. After a `dns_*`/user_data change on an
already-running proxy, terminate the instance (or trigger an ASG instance refresh)
so the replacement boots with the new user_data.

## Runner integration (baked into the image)

Runner pools use the proxy via a `registry-mirror` baked into the runner AMI (not
pool user_data). Add an image-builder component that merges the mirror into the
image's `/etc/docker/daemon.json`, preserving whatever else is there:

```json
{ "registry-mirrors": ["http://dockerhub-proxy.ci.internal:5000"],
  "insecure-registries": ["dockerhub-proxy.ci.internal:5000"] }
```

`insecure-registries` is required because the proxy serves plain HTTP.

The image-builder component document is YAML, and its command escaper only escapes
double quotes — a raw `jq`/JSON command breaks the parse. Wrap the script in base64
so the command carries no YAML-hostile characters:

```python
import base64

def docker_registry_mirror_component(endpoint):
    script = f"""#!/usr/bin/env bash
set -euo pipefail
f=/etc/docker/daemon.json
[ -f "$f" ] || echo '{{}}' > "$f"
tmp=$(mktemp)
jq '. + {{"registry-mirrors": ["http://{endpoint}"], "insecure-registries": ["{endpoint}"]}}' "$f" > "$tmp"
mv "$tmp" "$f"
jq -e '."registry-mirrors"[0] == "http://{endpoint}"' "$f"
systemctl restart docker || true
"""
    b64 = base64.b64encode(script.encode()).decode()
    return {
        "name": "docker-registry-mirror",
        "platform": "Linux",
        "description": "Point docker at the DockerHub pull-through cache",
        "commands": [f"printf '%s' '{b64}' | base64 -d | bash"],
    }
```

Pass the component (and, ideally, a matching test-phase assertion) to the runner
image builders. Because it changes the image, bump `image_recipe_version` and roll
the pools to the new AMI when the endpoint changes.

## Running alongside an existing proxy

The component runs fully in parallel with a pre-existing proxy: point `dns_zone` at
a separate, component-owned private zone so the existing proxy's DNS is never
touched, and only the runner pools whose images bake this mirror use it. An S3
mirror bucket can even be shared — zot populates under its own `s3_rootdirectory`
prefix, disjoint from any other registry's layout — so no data migration is needed
and the old proxy can be retired once all consumers have moved.
