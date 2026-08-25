# Requirements: native DockerHub proxy component

Status: **Draft / requirements** — no implementation yet.

## Problem

Praktika runner pools pull test images (e.g. `letsencrypt/pebble`,
`letsencrypt/pebble-challtestsrv`) from DockerHub. Runners live in the
Praktika-managed VPC and have no DockerHub registry mirror configured, so every
pull goes directly to `registry-1.docker.io`. This fails in two ways:

1. **Unresolvable / rate-limited upstream.** Observed in CI:

   ```
   pebble Error Get "https://registry-1.docker.io/v2/": dial tcp:
     lookup registry-1.docker.io on 10.0.0.2:53:
     read udp 10.0.0.1:...->10.0.0.2:53: read: connection refused
   ```

   `10.0.0.2` is the VPC resolver for the Praktika VPC (`clickhouse-vpc`,
   `10.0.0.0/18`). Under concurrent CI load, direct DockerHub pulls also hit
   HTTP 429 anonymous-pull rate limits even when DNS succeeds.

2. **The existing proxy is unreachable from the new VPC.** ClickHouse already
   runs a proven DockerHub caching proxy (see the legacy Terraform infra in the
   product repo: `tests/ci/terraform/asg_dockerhub-proxy.tf`,
   `tests/ci/terraform/worker/dockerhub_proxy_user_data.sh`,
   `tests/ci/terraform/dockerhub-proxy.md`). But:

   - The proxy runs in the **old** `ci-cd` VPC (`172.31.0.0/16`).
   - Its private Route 53 zone `dockerhub-proxy-zone` is associated **only** with
     that VPC.
   - The Praktika VPC (`clickhouse-vpc`, `10.0.0.0/18`) has **no peering** to
     `ci-cd` and cannot resolve or route to the proxy.

Wiring the runners' `daemon.json` at the proxy's DNS name alone does not fix
this — there is nothing reachable at that name from the Praktika VPC.

## Goal

Provide a **native Praktika component** that stands up a DockerHub pull-through
cache **inside the same VPC as the runner/orchestrator pools**, and configures
those pools to use it as a registry mirror. The result: transparent, cached,
authenticated DockerHub pulls with no cross-VPC dependency and no per-image
reference rewriting.

### Non-goals

- Rewriting image references in workflows (rules out ECR pull-through, which is
  not transparent — see "Alternatives" below).
- Mirroring registries other than DockerHub (`registry-1.docker.io`) in v1.
- Multi-AZ / HA proxy. A single instance behind an ASG (self-healing, desired
  capacity 1) matches the current production design and is sufficient for v1.

## Background: the proven design to port

The legacy proxy is a single EC2 node running two processes (see
`dockerhub_proxy_user_data.sh` and `dockerhub-proxy.md` in the product repo):

```
runner  →  nginx :5000  →  registry:2 :5001  →  DockerHub
             ↕                    ↕
    local manifest cache      S3 blob cache
    (/var/cache/nginx, 7d)    (bucket ch-docker-mirror)
```

- **`registry:2`** (port 5001) in pull-through mode
  (`REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io`), blobs cached in S3,
  authenticated to DockerHub with the `robotclickhouse` read-only PAT (kept in
  SSM). Pull-through mode re-validates every manifest against DockerHub on every
  pull, which floods `auth.docker.io` and triggers 429s under CI load — hence:
- **nginx** (port 5000) fronts the registry and caches **manifest** responses on
  local disk (7-day TTL, safe because CI images use pinned/digest tags, not
  `:latest`), with `proxy_cache_lock` to collapse concurrent misses. nginx
  `worker_connections` is raised from 768 to 16384 (each in-flight pull holds two
  connection slots) to avoid `worker_connections are not enough` drops.

Runners opt in via `/etc/docker/daemon.json`:

```json
"insecure-registries": ["dockerhub-proxy.dockerhub-proxy-zone:5000"],
"registry-mirrors": ["http://dockerhub-proxy.dockerhub-proxy-zone:5000"]
```

This design is well-understood and battle-tested; the requirement is to package
it as a Praktika component, not to redesign it.

## Proposed component

Add a native component, tentatively `DockerHubProxy`, following the existing
component conventions in `praktika/infrastructure/native/`:

- Shape it like `CIDBCluster` / `OrchestratorPool` / `RunnerPool`: a
  `@dataclass` `Config` whose `__post_init__` constructs the underlying
  `LaunchTemplate`, `AutoScalingGroup`, `IAMRole`, and `IAMInstanceProfile`
  configs and wires them into the VPC by `vpc_name`.
- Register it on `CloudInfrastructure.Config` via a new optional field
  (e.g. `dockerhub_proxy: Optional[DockerHubProxy] = None`), deployed alongside
  the other pools. It must deploy **before** the runner/orchestrator pools that
  depend on the mirror, or at least not block them (mirror is best-effort at
  pull time; a missing mirror should degrade, not hard-fail — but see discovery).

### Placement (same VPC — hard requirement)

- The proxy ASG uses the **same `vpc_name`** as the runner pools
  (`clickhouse-vpc` today), so it lands in the same subnet/CIDR and is reachable
  without peering.
- Instance type: a network-optimized ARM instance mirrors current prod
  (`c7gn.xlarge`); make it configurable with that as the default.
- ASG: `min_size=0`, `desired_capacity=1`, `max_size` small (e.g. 2 for rolling
  replace). Self-healing via ASG; no manual image push needed.

### Node bootstrap (user_data)

Port `dockerhub_proxy_user_data.sh` essentially verbatim, parameterized:

- Run `registry:2` on `:5001` with `REGISTRY_STORAGE=s3`,
  `REGISTRY_STORAGE_S3_BUCKET=<blob-cache-bucket>`,
  `REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io`, and the DockerHub PAT
  from SSM.
- Install and configure nginx on `:5000` with the manifest cache and raised
  `worker_connections` / `worker_rlimit_nofile`.
- Register the node's private IP for discovery (see below) and de-register on
  shutdown.

Base image: the Praktika image builder AMIs already install Docker (see
`_ubuntu_setup_component` in `praktika/infrastructure/native/image_builder.py`),
so the proxy can reuse a runner image or a minimal image that has Docker + nginx.

### S3 blob cache bucket

- The proxy needs an S3 bucket for `registry:2` blob storage (prod uses
  `ch-docker-mirror`).
- Express it as a Praktika `Storage.Config` (project-namespaced) or an
  explicit bucket name in the component config. The proxy's instance role needs
  read/write to `<bucket>` and `<bucket>/*`.
- Consider a lifecycle/retention policy on the cache bucket (out of scope for v1
  correctness, but note it).

### DockerHub credentials

- Read-only DockerHub PAT stored in SSM (prod:
  `/ci/docker/robotclickhouse-readonly-token`). The proxy instance role needs
  `ssm:GetParameter` (+ decrypt) on that parameter. Surface the parameter name
  as a component config field; reuse the `allowed_ssm_parameters` scoping
  pattern from `RunnerPool`.

### Service discovery — DECISION NEEDED

Runners must resolve a stable address for the proxy. Two viable patterns already
exist in the codebase:

- **Option A — Route 53 private zone + self-registration (recommended).**
  Create a private hosted zone associated with the Praktika VPC and have the
  node UPSERT `dockerhub-proxy.<zone>` → its private IP on boot, DELETE on
  shutdown (exactly the legacy mechanism). Runners get a **static DNS name** that
  can be baked into the image builder's `daemon.json` — no boot-time lookup on
  the runner side, and the proxy IP can change freely.
  - Requires the component to create/associate the zone with the VPC and grant
    the node `route53:ChangeResourceRecordSets` on that zone.

- **Option B — SSM parameter with the private IP (CIDB pattern).** Mirrors
  `CIDBCluster`, which publishes its private IP to an SSM parameter at deploy
  time (`praktika/infrastructure/native/cidb_cluster.py`, see the
  `put_parameter` in its deploy). Runner `user_data` reads the parameter and
  writes `daemon.json` at boot. Simpler infra (no Route 53) but couples the
  runner boot to an SSM read and requires a docker restart in `user_data`.

Recommendation: **Option A**, because it lets the mirror address be baked into
the AMI's `daemon.json` (matching legacy behavior) and keeps runner boot free of
proxy-discovery logic.

### Runner integration (daemon.json)

Runners currently get a `daemon.json` **without** a mirror, written in
`_ubuntu_setup_component` (`praktika/infrastructure/native/image_builder.py`,
the `with_docker` branch). Add mirror support there:

- Extend `create_ubuntu_image_builder_config` (and the AWS-Linux variant) to
  accept an optional `registry_mirror` (host:port). When set, include it in the
  generated `daemon.json` as both `registry-mirrors`
  (`http://<mirror>`) and `insecure-registries` (`<mirror>`), since the proxy is
  plain HTTP inside the VPC.
- Projects then pass the proxy's stable DNS name (Option A) when constructing
  their image builders in `projects.py`.
- Alternative: write `daemon.json` in the pool `user_data` instead of the image,
  which avoids an AMI rebuild to change the mirror but adds a docker restart at
  boot. The image-builder approach is preferred for parity with legacy infra.

### Security group

- The default VPC SG created by `VPC.Config` (`<vpc_name>-sg`) is created with
  **no ingress rules** (see `praktika/infrastructure/vpc.py`), so runners in
  that SG cannot reach the proxy on `:5000` by default.
- The component must ensure an ingress rule allowing **TCP 5000** to the proxy
  from the runner instances — either a self-referencing rule on `<vpc_name>-sg`
  or a dedicated proxy SG that admits the runner SG / the VPC CIDR. Prefer a
  dedicated proxy SG that admits the runner SG by ID (least privilege), matching
  the legacy `launch-wizard-1` rule that admits the `pr-runner` SG on 5000.

### IAM for the proxy instance role

Minimum:

- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`,
  `s3:*MultipartUpload*` on the blob-cache bucket and `/*`.
- `ssm:GetParameter` (+ KMS decrypt if the PAT is a SecureString) on the
  DockerHub PAT parameter.
- `route53:ChangeResourceRecordSets` on the discovery zone (Option A only).
- `autoscaling:TerminateInstanceInAutoScalingGroup` scoped to its own ASG
  (self-terminate), consistent with the other pools.
- CloudWatch agent policy (log shipping), consistent with the other pools.

## Proposed config surface (sketch)

```python
@dataclass
class DockerHubProxy:
    vpc_name: str                       # same VPC as the runner pools
    instance_type: str = "c7gn.xlarge"
    image_builder: ImageBuilder.Config | None = None
    ami_id: str = ""
    blob_cache_bucket: str = ""         # or a Storage.Config reference
    dockerhub_token_ssm_parameter: str = "/ci/docker/robotclickhouse-readonly-token"
    dns_zone_name: str = "dockerhub-proxy-zone"   # Option A
    mirror_hostname: str = "dockerhub-proxy"      # -> dockerhub-proxy.<zone>:5000
    manifest_cache_ttl_days: int = 7
    nginx_worker_connections: int = 16384
    volume_size_gb: int = 100           # local manifest cache + registry temp
    max_size: int = 2
    # created in __post_init__:
    launch_template / autoscaling_group / ec2_role / instance_profile /
    security_group (+ ingress rule) / route53 record (Option A)
```

The mirror address (`<mirror_hostname>.<dns_zone_name>:5000`) is what the image
builders' `daemon.json` points at.

## Alternatives considered

- **Reach the legacy proxy cross-VPC** (peering + zone association + SG rules).
  Rejected: couples the self-contained Praktika infra to the legacy `ci-cd` VPC,
  spans two Terraform/ownership domains, and leaves the mirror outside Praktika's
  lifecycle.
- **ECR pull-through cache.** Rejected: not transparent — cached images are
  served under a rewritten URI prefix
  (`<acct>.dkr.ecr.<region>.amazonaws.com/docker.io/...`), so every image
  reference in CI would have to change. (AWS transparent-mode request:
  https://github.com/aws/containers-roadmap/issues/2096)
- **Plain registry (no proxy mode), images pushed manually.** Rejected: needs an
  operational process to populate images.

## Acceptance criteria

- A runner pool in the Praktika VPC pulls a fresh DockerHub image
  (`letsencrypt/pebble`) successfully with the proxy up, contacting only the
  in-VPC proxy (no direct `registry-1.docker.io` traffic from the runner).
- A second pull of the same tag is served from cache (S3 blob + nginx manifest)
  without contacting DockerHub.
- The proxy self-heals: terminating the instance brings up a replacement that
  re-registers discovery and resumes serving.
- Concurrent pulls from many runners do not exhaust nginx connections and do not
  trigger DockerHub 429s.
- No cross-VPC dependency on `ci-cd` or `dockerhub-proxy-zone`.

## Open questions

Every decision point flagged in the body is collected here.

1. **Discovery mechanism**: confirm **Option A (Route 53 private zone +
   self-registration, recommended)** vs **Option B (SSM-published private IP,
   CIDB pattern)**. See "Service discovery".
2. **Where `daemon.json` is written**: **image builder** (mirror baked into the
   AMI, needs a rebuild to change) vs **pool `user_data`** (boot-time, adds a
   docker restart). See "Runner integration".
3. **Security group shape**: a **dedicated proxy SG** that admits the runner SG
   by ID on TCP 5000 (least privilege, recommended) vs a **self-referencing
   ingress rule** on the shared `<vpc_name>-sg`. See "Security group".
4. **Deploy ordering / failure mode**: must the proxy deploy **before** the
   runner/orchestrator pools, and should a missing/unhealthy mirror **degrade**
   (runners fall back to direct pulls) or **hard-fail** the pull? See "Proposed
   component".
5. **S3 blob-cache bucket**: reuse the existing `ch-docker-mirror` bucket, or
   create a project-namespaced bucket for the Praktika infra (`Storage.Config`)?
   See "S3 blob cache bucket".
6. **Cache bucket retention**: define a lifecycle/retention policy on the blob
   cache, or leave it unbounded for v1? See "S3 blob cache bucket".
7. **DockerHub credentials**: reuse the existing `robotclickhouse` PAT / SSM
   parameter, or mint a new one owned by the Praktika project? See "DockerHub
   credentials".
8. **Base image for the proxy node**: reuse a runner image (already has Docker),
   or build a minimal Docker + nginx image? See "Node bootstrap".
9. **Instance type default**: keep the prod `c7gn.xlarge` (network-optimized
   ARM) as the default, and confirm the AMI/image builder architecture matches.
   See "Placement".
10. **Outbound egress**: confirm the Praktika VPC subnet routing (IGW/NAT) lets
    the proxy reach `registry-1.docker.io` and `auth.docker.io` outbound — the
    intermittent `10.0.0.2:53 connection refused` in the incident suggests the
    VPC's own egress/DNS should be validated. See "Non-goals" / incident above.
</content>
</invoke>
