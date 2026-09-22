from dataclasses import dataclass, field
from typing import Any, Dict, List

from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.iam_instance_profile import IAMInstanceProfile
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.launch_template import LaunchTemplate

from .configs import DOCKER_PROXY_INSTANCE_PROFILE_NAME, DOCKER_PROXY_ROLE_NAME
from .user_data import docker_proxy_user_data


def _ssm_parameter_arn(name: str) -> str:
    value = (name or "").strip()
    if value.startswith("arn:"):
        return value
    return f"arn:aws:ssm:*:*:parameter/{value.lstrip('/')}"


def _bucket_rw_arns(bucket: str) -> List[str]:
    bucket = (bucket or "").strip()
    if not bucket:
        return []
    return [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]


@dataclass
class DockerProxy:
    """A single-instance DockerHub pull-through cache backed by S3, running
    ``zot`` (an OCI-native registry) as one process.

    Replaces the two-process ``nginx + registry:2`` proxy: zot serves DockerHub
    images at their native ``library/...`` paths (so Docker's ``registry-mirrors``
    works with no image-reference rewrites) and, with ``manifestCheckInterval``
    set, serves cached manifests and blobs from S3 **without re-contacting
    DockerHub** — the behavior that previously required an nginx manifest cache
    in front of ``registry:2``.

    Shape: a single-instance AutoScalingGroup (min=max=desired=1) so a dead node
    is replaced automatically, plus the LaunchTemplate, IAM role, and instance
    profile it needs. All are created at construction time and registered into
    CloudInfrastructure.Config automatically via its ``docker_proxy`` field.

    Data path (see docker_proxy_user_data.sh)::

        runner --[registry-mirror]--> zot (:5000) --> DockerHub (first pull only)
                                         |
                                         v
                              S3 (manifests + blobs)

    Credentials at rest: none. The DockerHub PAT is read from SSM at boot
    (``dockerhub_pat_ssm``) into zot's sync credentials file; S3 is accessed with
    the EC2 instance role. The instance self-registers a Route53 A record
    (``dns_record`` in ``dns_zone``) at boot and deletes it on shutdown, so
    runners reach it at a stable name.

    Example::

        docker_proxy = DockerProxy(
            instance_type="c7gn.large",
            s3_bucket="ch-docker-mirror",
        )
    """

    name: str = "dockerhub-proxy"
    # Graviton (arm64). Keep a family ending in "g" so the launch-template AMI
    # resolver detects arm64 (it keys off family.endswith("g"), which misses the
    # "gn"/"gd" network/disk-optimized variants).
    instance_type: str = "c7g.large"
    vpc_name: str = ""
    ami_id: str = ""  # AL2023 arm64/x86_64 resolved at deploy time if empty
    # zot release to install. Must be >= v2.1.21 (needs manifestCheckInterval).
    zot_version: str = "v2.1.21"
    # S3 bucket for the mirror storage (manifests + blobs). Reused across
    # instance replacements so a fresh node boots warm.
    s3_bucket: str = "ch-docker-mirror"
    s3_region: str = "us-east-1"
    # S3 key prefix zot stores under; isolates zot's layout from any legacy
    # registry:2 layout in the same bucket.
    s3_rootdirectory: str = "/zot"
    upstream_url: str = "https://registry-1.docker.io"
    # SSM SecureString holding the DockerHub read-only PAT. Left as-is by project
    # namespacing (it is an existing external parameter).
    dockerhub_pat_ssm: str = "/ci/docker/robotclickhouse-readonly-token"
    dockerhub_username: str = "robotclickhouse"
    # How long a cached tag is served without re-checking DockerHub. Bounds
    # upstream contact to the first pull of each tag (plus one re-check per tag
    # after a process restart, since the last-check time is in-memory).
    manifest_check_interval: str = "168h"
    listen_port: int = 5000
    # Serve zot's web UI (repo/tag browser) on the same port at `/` (the registry
    # API stays at `/v2/`). Lightweight: same binary, no CVE/trivy scanning.
    enable_ui: bool = False
    # Route53 self-registration. The hosted zone must already exist; the record
    # is what runners point their registry-mirror at. Both are external DNS
    # identities and are NOT project-namespaced.
    dns_zone: str = "dockerhub-proxy-zone"
    dns_record: str = "dockerhub-proxy.dockerhub-proxy-zone"
    security_group_ids: List[str] = field(default_factory=list)
    security_group_names: List[str] = field(default_factory=list)
    volume_size_gb: int = 40
    region: str = ""
    ext: Dict[str, Any] = field(default_factory=dict)

    ec2_role: IAMRole.Config = field(init=False)
    instance_profile: IAMInstanceProfile.Config = field(init=False)
    launch_template: LaunchTemplate.Config = field(init=False)
    autoscaling_group: AutoScalingGroup.Config = field(init=False)

    def __post_init__(self):
        if (
            self.vpc_name
            and not self.security_group_ids
            and not self.security_group_names
        ):
            self.security_group_names = [f"{self.vpc_name}-sg"]

        self.ec2_role = IAMRole.Config(
            name=DOCKER_PROXY_ROLE_NAME,
            trust_service="ec2.amazonaws.com",
            policy_arns=[
                "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
            ],
            inline_policies={},
        )
        self.instance_profile = IAMInstanceProfile.Config(
            name=DOCKER_PROXY_INSTANCE_PROFILE_NAME,
            role_name=self.ec2_role.name,
        )
        self.launch_template = LaunchTemplate.Config(
            name=f"{self.name}-lt",
            image_id=self.ami_id,
            instance_type=self.instance_type,
            security_group_ids=self.security_group_ids,
            security_group_names=self.security_group_names,
            vpc_name=self.vpc_name,
            iam_instance_profile_name=self.instance_profile.name,
            set_default_version_to_latest=True,
            user_data="",  # rendered by _refresh() below
            root_volume_size_gb=self.volume_size_gb,
            root_volume_type="gp3",
            tags={"praktika_role": "docker_proxy"},
            praktika_resource_tag="docker-proxy",
        )
        self.autoscaling_group = AutoScalingGroup.Config(
            name=self.name,
            vpc_name=self.vpc_name,
            availability_zones=[],
            min_size=1,
            max_size=1,
            desired_capacity=1,
            launch_template_name=self.launch_template.name,
            launch_template_version="$Latest",
            tags={"praktika_role": "docker_proxy"},
            praktika_resource_tag="docker-proxy",
        )
        self._refresh()

    def _refresh(self):
        """(Re)derive the IAM statements and user_data from the current config.
        Called at construction and again by CloudInfrastructure after project
        namespacing resolves resource names."""
        statements = [
            {
                "Sid": "ReadDockerHubPAT",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                "Resource": [_ssm_parameter_arn(self.dockerhub_pat_ssm)],
            },
            {
                "Sid": "Route53SelfRegister",
                "Effect": "Allow",
                "Action": ["route53:ChangeResourceRecordSets"],
                "Resource": ["arn:aws:route53:::hostedzone/*"],
            },
            {
                "Sid": "Route53LookupZone",
                "Effect": "Allow",
                "Action": ["route53:ListHostedZonesByName"],
                "Resource": ["*"],
            },
        ]
        bucket_arns = _bucket_rw_arns(self.s3_bucket)
        if bucket_arns:
            statements.append(
                {
                    "Sid": "MirrorBucketReadWrite",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                        "s3:GetBucketLocation",
                        "s3:ListBucketMultipartUploads",
                        "s3:ListMultipartUploadParts",
                        "s3:AbortMultipartUpload",
                    ],
                    "Resource": bucket_arns,
                }
            )
        ts = self._tailscale_config()
        if ts:
            statements.append(
                {
                    "Sid": "ReadTailscaleOAuthClient",
                    "Effect": "Allow",
                    "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                    "Resource": [
                        _ssm_parameter_arn(ts["oauth_client_id_ssm"]),
                        _ssm_parameter_arn(ts["oauth_client_secret_ssm"]),
                    ],
                }
            )
        self.ec2_role.inline_policies = {
            "DockerProxyAccess": {
                "Version": "2012-10-17",
                "Statement": statements,
            }
        }
        self.launch_template.user_data = docker_proxy_user_data(
            zot_version=self.zot_version,
            s3_bucket=self.s3_bucket,
            s3_region=self.s3_region,
            s3_rootdirectory=self.s3_rootdirectory,
            upstream_url=self.upstream_url,
            dockerhub_pat_ssm=self.dockerhub_pat_ssm,
            dockerhub_username=self.dockerhub_username,
            manifest_check_interval=self.manifest_check_interval,
            listen_port=self.listen_port,
            dns_zone=self.dns_zone,
            dns_record=self.dns_record,
            tailscale=ts,
            enable_ui=self.enable_ui,
        )

    def _tailscale_config(self):
        """The Tailscale settings set via configure_tailscale, or None."""
        return self.ext.get("tailscale")

    def configure_tailscale(
        self,
        tag,
        oauth_client_id_ssm,
        oauth_client_secret_ssm,
        hostname="",
    ):
        """Opt into exposing the UI/registry over Tailscale.

        Stores the settings in ``ext["tailscale"]`` (keeping the Config lean and
        backward-compatible) and re-derives the IAM policy + user_data. At boot the
        node mints a tagged ephemeral auth key from the SSM-stored Tailscale OAuth
        client, joins the tailnet (``tailscale up --ssh``), and serves the registry
        at root over HTTPS via ``tailscale serve``.

        ``tag``, ``oauth_client_id_ssm`` and ``oauth_client_secret_ssm`` are
        required (there is no safe default for which tag/OAuth client to use).
        ``hostname`` is the Tailscale machine name, which becomes the node's
        MagicDNS name — the UI is served at ``https://{hostname}.<tailnet>.ts.net/``.
        It defaults to the component ``name`` and is a tailnet identity, so it is
        NOT project-namespaced. Returns self for chaining."""
        self.ext["tailscale"] = {
            "hostname": hostname or self.name,
            "tag": tag,
            "oauth_client_id_ssm": oauth_client_id_ssm,
            "oauth_client_secret_ssm": oauth_client_secret_ssm,
        }
        self._refresh()
        return self

    def deploy(self):
        """Create the private hosted zone (if needed) and authorize inbound to
        the proxy's listen port. The IAM role/profile, launch template and ASG
        deploy through the standard CloudInfrastructure passes; this method owns
        the two things specific to the proxy that those passes don't cover:

          1. A private Route53 zone (``dns_zone``) associated with the VPC, so the
             instance can self-register ``dns_record`` at boot and runners can
             resolve it. praktika has no Route53-zone resource, so the component
             owns it here — making the proxy fully deployable by ``--deploy``.
          2. An ingress rule for ``listen_port`` on the shared SG (runner ->
             proxy), source = the same SG.

        This runs before the LaunchTemplate/ASG passes, so on a fresh deploy the
        zone exists before the instance boots. Idempotent."""
        from .._utils import aws_client
        from ..vpc import VPC

        if not self.region:
            raise ValueError("DockerProxy.region is not set")

        lookup = VPC.Lookup(name=self.vpc_name, region=self.region)

        # 1) Ensure the private hosted zone exists and is associated with the VPC.
        if self.dns_zone:
            self._ensure_hosted_zone(self.region, lookup.vpc_id)
        sg_ids = list(self.security_group_ids)
        sg_ids.extend(lookup.resolve_security_group_ids(self.security_group_names))
        if not sg_ids:
            raise ValueError("DockerProxy has no resolvable security groups")

        # 2) Authorize the listen port on the shared SG (runner -> proxy).
        ec2 = aws_client("ec2", self.region, "docker-proxy")
        for sg_id in sg_ids:
            try:
                ec2.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[
                        {
                            "IpProtocol": "tcp",
                            "FromPort": self.listen_port,
                            "ToPort": self.listen_port,
                            "UserIdGroupPairs": [{"GroupId": sg_id}],
                        }
                    ],
                )
                print(f"Authorized tcp/{self.listen_port} on {sg_id} from {sg_id}")
            except ec2.exceptions.ClientError as e:
                if "InvalidPermission.Duplicate" in str(e):
                    print(f"Ingress tcp/{self.listen_port} on {sg_id} already present")
                else:
                    raise

    def _ensure_hosted_zone(self, region: str, vpc_id: str) -> str:
        """Ensure a PRIVATE hosted zone named ``dns_zone`` exists and is
        associated with ``vpc_id``; create or associate as needed. Returns the
        hosted zone id. Idempotent, and never touches zones of other names (the
        legacy Terraform ``dockerhub-proxy-zone`` is left alone)."""
        from .._utils import aws_client

        r53 = aws_client("route53", region, "docker-proxy")
        fqdn = self.dns_zone.rstrip(".") + "."

        # Private zones with this exact name.
        listed = r53.list_hosted_zones_by_name(DNSName=fqdn).get("HostedZones", [])
        candidates = [
            z
            for z in listed
            if z.get("Name") == fqdn and z.get("Config", {}).get("PrivateZone")
        ]
        for zone in candidates:
            detail = r53.get_hosted_zone(Id=zone["Id"])
            associated = {v.get("VPCId") for v in detail.get("VPCs", [])}
            if vpc_id in associated:
                print(f"Hosted zone {fqdn} already associated with {vpc_id}: {zone['Id']}")
                return zone["Id"]
        if candidates:
            zone_id = candidates[0]["Id"]
            r53.associate_vpc_with_hosted_zone(
                HostedZoneId=zone_id,
                VPC={"VPCRegion": region, "VPCId": vpc_id},
            )
            print(f"Associated hosted zone {fqdn} ({zone_id}) with {vpc_id}")
            return zone_id

        resp = r53.create_hosted_zone(
            Name=fqdn,
            CallerReference=f"praktika-docker-proxy-{self.dns_zone}",
            HostedZoneConfig={
                "Comment": "praktika DockerHub proxy",
                "PrivateZone": True,
            },
            VPC={"VPCRegion": region, "VPCId": vpc_id},
        )
        zone_id = resp["HostedZone"]["Id"]
        print(f"Created private hosted zone {fqdn}: {zone_id} (associated {vpc_id})")
        return zone_id
