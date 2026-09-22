"""Read-only health checks for deployed infrastructure.

`python3 -m praktika infrastructure --verify` calls `verify_infrastructure`
below. Unlike deploy/destroy, verification makes no changes: it probes live
components and reports whether each is functional.

Adding a new check:
  1. Write a `_verify_<component>(...)` function that returns a `CheckResult`
     (or a list of them). It must never raise — a broken component is a FAIL
     result, not an aborted run, so `verify_infrastructure` can report every
     check.
  2. Register it in `verify_infrastructure` behind a `_wants(...)` guard using
     the same component name as `deploy --only` (so `--only <Component>` selects
     it consistently across commands).
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def verify_infrastructure(config, only: Optional[List[str]] = None) -> bool:
    """Run the selected health checks against `config`'s live infrastructure.

    Returns True when every selected check passes, False otherwise.
    """
    config._verify_account()
    region = config._settings.AWS_REGION if config._settings else ""

    only_set = {
        s.strip().lower() for s in (only or []) if isinstance(s, str) and s.strip()
    }

    def _wants(type_name: str, *aliases: str) -> bool:
        if not only_set:
            return True
        keys = {type_name.lower(), *{a.lower() for a in aliases if a}}
        return bool(keys & only_set)

    results: List[CheckResult] = []

    if _wants("GitHubTokenMinter", "GitHubTokenMinters", "gh-token", "token"):
        for token_minter in config.github_token_minters:
            name = f"GitHubTokenMinter[{token_minter.lambda_config.name}]"
            print(f"Checking {name} ...")
            results.append(_verify_token_minter(name, token_minter, region))

    if _wants(
        "SecretParameter",
        "SecretParameters",
        "secrets",
        "parameters",
        "secrets-and-parameters",
    ):
        print("Checking secrets and parameters ...")
        results.extend(_verify_secrets_and_parameters(config, region))

    if _wants(
        "RunnerRoleAccess",
        "RunnerPool",
        "RunnerPools",
        "runner-role-access",
        "runner-access",
        "role-access",
        "pools",
    ):
        print("Checking runner role access to secrets and parameters ...")
        results.extend(_verify_runner_role_access(config, region))

    if _wants("S3Proxy", "s3-proxy", "s3proxy", "proxy"):
        if config.s3_proxy:
            name = f"S3Proxy[{config.s3_proxy.name}]"
            print(f"Checking {name} ...")
            results.extend(_verify_s3_proxy(name, config.s3_proxy, region))

    if _wants("DockerProxy", "docker-proxy", "dockerproxy", "dockerhub-proxy", "proxy"):
        if config.docker_proxy:
            name = f"DockerProxy[{config.docker_proxy.name}]"
            print(f"Checking {name} ...")
            results.extend(_verify_docker_proxy(name, config.docker_proxy, region))

    return _report(results)


def _report(results: List[CheckResult]) -> bool:
    print("\n" + "=" * 60)
    print("Infrastructure verification")
    print("=" * 60)
    failed = 0
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")
        if not result.ok:
            failed += 1
    print("=" * 60)
    if not results:
        print("No checks matched the selection")
        return True
    if failed:
        print(f"{failed}/{len(results)} check(s) FAILED")
        return False
    print(f"All {len(results)} check(s) passed")
    return True


def _verify_token_minter(name: str, token_minter, region: str) -> CheckResult:
    """Invoke a GitHub token minter Lambda and confirm it returns a token."""
    import json

    from ._utils import aws_client

    lambda_region = token_minter.lambda_config.region or region
    if not lambda_region:
        return CheckResult(name, False, "no region configured (set Settings.AWS_REGION)")
    try:
        client = aws_client("lambda", lambda_region, context="token-minter-verify")
        response = client.invoke(
            FunctionName=token_minter.lambda_config.name,
            InvocationType="RequestResponse",
            Payload=b"{}",
        )
    except Exception as e:  # noqa: BLE001 - report any probe failure as FAIL
        return CheckResult(name, False, f"lambda invoke failed: {e}")

    payload_raw = response["Payload"].read().decode("utf-8", errors="replace")
    if response.get("FunctionError"):
        return CheckResult(name, False, f"lambda FunctionError: {payload_raw[:300]}")
    try:
        result = json.loads(payload_raw)
    except json.JSONDecodeError as e:
        return CheckResult(name, False, f"non-JSON lambda payload [{payload_raw[:200]}]: {e}")

    status_code = result.get("statusCode")
    if status_code != 200:
        return CheckResult(name, False, f"lambda returned statusCode={status_code}: {result}")
    body = result.get("body", {})
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as e:
            return CheckResult(name, False, f"invalid lambda body [{body[:200]}]: {e}")
    token = (body or {}).get("token")
    if not token:
        return CheckResult(name, False, "lambda returned no token")
    expires_at = body.get("expires_at", "?")
    cached = body.get("cached", False)
    return CheckResult(
        name,
        True,
        f"minted token (len={len(token)}, expires_at={expires_at}, cached={cached})",
    )


def _collect_secret_references(config):
    """Gather every concrete secret/parameter the infrastructure depends on,
    as (store, name) pairs. `store` is "ssm" (SSM Parameter Store) or
    "secretsmanager" (AWS Secrets Manager).

    Names are read off the fully-namespaced config, so they are the final AWS
    names. Wildcard/ARN allow-list grants (e.g. a pool's `allowed_ssm_parameters`)
    are intentionally excluded: those are IAM permission patterns, not hard
    dependencies that must already exist.
    """
    refs = []

    # Lambda env secrets are fetched from SSM Parameter Store (see
    # Lambda.Config._fetch_secrets / _validate_secrets).
    for lambda_config in config.lambda_functions:
        for param_name in (lambda_config.secrets or {}):
            refs.append(("ssm", param_name))

    # SecretParameter entries are SSM SecureStrings (auto-generated on deploy
    # or created out of band). After a deploy they must exist.
    for secret in config.secret_parameters:
        refs.append(("ssm", secret.name))

    # The S3 report proxy reads a Tailscale OAuth client from SSM at boot.
    if config.s3_proxy:
        refs.append(("ssm", config.s3_proxy.tailscale_oauth_client_id_ssm))
        refs.append(("ssm", config.s3_proxy.tailscale_oauth_client_secret_ssm))

    # The DockerHub proxy reads a DockerHub PAT from SSM at boot, and (if
    # configured) a Tailscale OAuth client.
    if config.docker_proxy:
        refs.append(("ssm", config.docker_proxy.dockerhub_pat_ssm))
        ts = config.docker_proxy.ext.get("tailscale")
        if ts:
            refs.append(("ssm", ts["oauth_client_id_ssm"]))
            refs.append(("ssm", ts["oauth_client_secret_ssm"]))

    # The GitHub token minter reads its GitHub App private key from Secrets
    # Manager (see native/github_token_minter.py).
    for token_minter in config.github_token_minters:
        refs.append(("secretsmanager", token_minter.secret_name))

    # Dedup while preserving order; drop empties and wildcard patterns.
    seen = set()
    result = []
    for store, name in refs:
        name = (name or "").strip()
        if not name or "*" in name:
            continue
        # An ARN allow-list value that slipped in — resolve to the bare name.
        if name.startswith("arn:aws:secretsmanager:") and ":secret:" in name:
            name = name.split(":secret:", 1)[1]
        elif name.startswith("arn:aws:ssm:") and ":parameter/" in name:
            name = "/" + name.split(":parameter/", 1)[1]
        key = (store, name)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _verify_secrets_and_parameters(config, region: str) -> List[CheckResult]:
    """Confirm every secret/parameter referenced by the infrastructure exists
    in AWS (SSM Parameter Store / Secrets Manager). One result per secret."""
    from ._utils import aws_client

    refs = _collect_secret_references(config)
    if not refs:
        return [CheckResult("Secrets", True, "no secret/parameter references found")]
    if not region:
        return [
            CheckResult(
                "Secrets", False, "no region configured (set Settings.AWS_REGION)"
            )
        ]

    ssm = aws_client("ssm", region, context="secrets-verify")
    secretsmanager = None
    results: List[CheckResult] = []
    for store, name in refs:
        label = f"Secret[{store}:{name}]"
        try:
            if store == "ssm":
                ssm.get_parameter(Name=name, WithDecryption=False)
                results.append(CheckResult(label, True, "exists in SSM Parameter Store"))
            else:
                if secretsmanager is None:
                    secretsmanager = aws_client(
                        "secretsmanager", region, context="secrets-verify"
                    )
                secretsmanager.describe_secret(SecretId=name)
                results.append(CheckResult(label, True, "exists in Secrets Manager"))
        except Exception as e:  # noqa: BLE001 - a missing/unreadable secret is a FAIL
            kind = type(e).__name__
            if "ParameterNotFound" in kind or "ResourceNotFoundException" in kind:
                results.append(CheckResult(label, False, "NOT FOUND in AWS"))
            else:
                results.append(CheckResult(label, False, f"lookup failed: {e}"))
    return results


def _runtime_secret_targets(config, region: str, account_id: str):
    """Expand `config.runtime_secrets` into concrete IAM simulation targets.

    Each target is a dict: {label, action, resource_arn}. Only AWS-backed
    secret types produce targets — GitHub secrets/vars reach jobs as env vars,
    not via the instance role, so they are not IAM-relevant here. A Secret.Config
    `name` may be a single string or a list of names; both are flattened.
    """
    from praktika.secret import Secret

    targets = []
    for secret in config.runtime_secrets or []:
        names = secret.name if isinstance(secret.name, list) else [secret.name]
        secret_region = (getattr(secret, "region", "") or region).strip()
        for raw_name in names:
            name = (raw_name or "").strip()
            if not name:
                continue
            if secret.type == Secret.Type.AWS_SSM_PARAMETER:
                arn = f"arn:aws:ssm:{secret_region}:{account_id}:parameter/{name.lstrip('/')}"
                targets.append(
                    {
                        "label": f"ssm:{name}",
                        "action": "ssm:GetParameter",
                        "resource_arn": arn,
                    }
                )
            elif secret.type == Secret.Type.AWS_SSM_SECRET:
                # Secrets Manager secret; a trailing ".key" selects a JSON field
                # of the same underlying secret, so the IAM target is the root.
                root = name.split(".", 1)[0]
                arn = (
                    f"arn:aws:secretsmanager:{secret_region}:{account_id}:secret:{root}"
                )
                targets.append(
                    {
                        "label": f"secretsmanager:{root}",
                        "action": "secretsmanager:GetSecretValue",
                        # Secrets Manager appends a random 6-char suffix to the
                        # ARN; match it with a trailing wildcard so the simulator
                        # resolves against the deployed "secret:{root}*" grant.
                        "resource_arn": f"{arn}*",
                    }
                )
            # GH_SECRET / GH_VAR: env-delivered, no instance-role permission.
    # Dedup while preserving order (same secret can appear in multiple lists).
    seen = set()
    deduped = []
    for target in targets:
        key = (target["action"], target["resource_arn"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _verify_runner_role_access(config, region: str) -> List[CheckResult]:
    """Simulate, per runner pool, whether the pool's IAM role can actually read
    every secret/parameter that jobs consume at runtime (`config.runtime_secrets`).

    This uses IAM policy simulation (`iam:SimulatePrincipalPolicy`) against the
    pool's *deployed* role — it reads nothing from the secrets themselves, makes
    no changes, and does not assume the role (runner roles trust only the EC2
    service, not an operator). Because it evaluates the deployed policy, it also
    catches a config change that has not been re-applied yet.
    """
    from ._utils import aws_account_id, aws_client

    if not region:
        return [
            CheckResult(
                "RunnerRoleAccess",
                False,
                "no region configured (set Settings.AWS_REGION)",
            )
        ]

    pools = list(getattr(config, "runner_pools", []) or []) + list(
        getattr(config, "dedicated_runner_pools", []) or []
    )
    if not pools:
        return [CheckResult("RunnerRoleAccess", True, "no runner pools configured")]

    if not (config.runtime_secrets or []):
        return [
            CheckResult(
                "RunnerRoleAccess",
                True,
                "no runtime_secrets configured — nothing to check (set "
                "CloudInfrastructure.Config.runtime_secrets to the secrets jobs read)",
            )
        ]

    try:
        account_id = aws_account_id(region)
    except Exception as e:  # noqa: BLE001
        return [
            CheckResult("RunnerRoleAccess", False, f"cannot resolve AWS account id: {e}")
        ]

    targets = _runtime_secret_targets(config, region, account_id)
    if not targets:
        return [
            CheckResult(
                "RunnerRoleAccess",
                True,
                "runtime_secrets contains no AWS-backed (SSM/Secrets Manager) entries",
            )
        ]

    iam = aws_client("iam", region, context="runner-role-access-verify")
    results: List[CheckResult] = []
    for pool in pools:
        role = getattr(pool, "ec2_role", None)
        role_name = getattr(role, "name", "") if role else ""
        if not role_name:
            # External / pre-existing instance profile: praktika did not build a
            # role here, so there is no generated policy to simulate.
            results.append(
                CheckResult(
                    f"RunnerRoleAccess[{pool.name}]",
                    True,
                    "pool uses an external instance profile — skipped (no praktika-managed role)",
                )
            )
            continue
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        for target in targets:
            label = f"RunnerRoleAccess[{pool.name}: {target['label']}]"
            try:
                response = iam.simulate_principal_policy(
                    PolicySourceArn=role_arn,
                    ActionNames=[target["action"]],
                    ResourceArns=[target["resource_arn"]],
                )
            except Exception as e:  # noqa: BLE001 - a failed probe is a FAIL, not an abort
                kind = type(e).__name__
                if "NoSuchEntity" in kind:
                    results.append(
                        CheckResult(label, False, f"role {role_name} not found in AWS")
                    )
                elif "AccessDenied" in kind:
                    results.append(
                        CheckResult(
                            label,
                            False,
                            "operator lacks iam:SimulatePrincipalPolicy — cannot verify",
                        )
                    )
                else:
                    results.append(CheckResult(label, False, f"simulate failed: {e}"))
                continue
            evaluations = response.get("EvaluationResults", [])
            decision = evaluations[0].get("EvalDecision") if evaluations else "unknown"
            if decision == "allowed":
                results.append(
                    CheckResult(label, True, f"{target['action']} allowed by role policy")
                )
            else:
                results.append(
                    CheckResult(
                        label,
                        False,
                        f"{target['action']} on {target['resource_arn']} is {decision} "
                        f"for role {role_name} (grant it via the pool's allowed_* / "
                        f"allow_all_* fields, then re-apply)",
                    )
                )
    return results


def _verify_s3_proxy(name: str, proxy, region: str) -> List[CheckResult]:
    """Smoke-test the S3 report proxy end to end: fetch a real object from a
    proxied private bucket *through the proxy* and confirm the bytes match what
    S3 returns directly. This exercises the full data path
    (Tailscale -> Caddy -> signer -> instance-role SigV4 -> private S3)."""
    from ._utils import aws_client

    if not region:
        return [
            CheckResult(
                f"{name} serves",
                False,
                "no region configured (set Settings.AWS_REGION)",
            )
        ]

    results = [_verify_tailscale_auth(f"{name} tailscale-auth", proxy, region)]

    buckets = [b for b in (proxy.proxied_buckets or []) if b and b.strip()]
    if not buckets:
        return results + [
            CheckResult(
                f"{name} serves",
                False,
                "no proxied_buckets configured — the proxy would serve nothing",
            )
        ]
    fqdn = proxy.report_fqdn()
    if not fqdn:
        return results + [
            CheckResult(
                f"{name} serves",
                False,
                "tailnet not configured on S3Proxy (set tailnet=...); "
                "cannot smoke-test the served URL",
            )
        ]

    # Find a small, real object in one of the proxied buckets to fetch.
    s3 = aws_client("s3", region, context="s3-proxy-verify")
    probe = None  # (bucket, key)
    for bucket in buckets:
        try:
            listing = s3.list_objects_v2(Bucket=bucket, MaxKeys=50)
        except Exception as e:  # noqa: BLE001
            return results + [CheckResult(f"{name} serves", False, f"cannot list s3://{bucket}: {e}")]
        objects = [o for o in listing.get("Contents", []) if o.get("Size", 0) > 0]
        if objects:
            objects.sort(key=lambda o: o.get("Size", 0))
            probe = (bucket, objects[0]["Key"])
            break
    if probe is None:
        return results + [
            CheckResult(
                f"{name} serves",
                False,
                f"no non-empty objects found in {buckets} to smoke-test",
            )
        ]

    bucket, key = probe
    # Compare only the first few KB so large objects stay cheap; the signer
    # forwards the Range header, so S3 and the proxy return the same slice.
    range_header = "bytes=0-4095"
    try:
        s3_body = s3.get_object(Bucket=bucket, Key=key, Range=range_header)["Body"]
        expected = s3_body.read()
        s3_body.close()
    except Exception as e:  # noqa: BLE001
        return results + [
            CheckResult(f"{name} serves", False, f"cannot read s3://{bucket}/{key}: {e}")
        ]

    import urllib.error
    import urllib.request

    url = f"http://{fqdn}:8080/{bucket}/{key}"
    request = urllib.request.Request(url, headers={"Range": range_header})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            served = response.read()
            status = response.status
    except urllib.error.HTTPError as e:
        return results + [
            CheckResult(
                f"{name} serves",
                False,
                f"proxy returned HTTP {e.code} for {bucket}/{key} at {fqdn}:8080",
            )
        ]
    except Exception as e:  # noqa: BLE001 - DNS/connection failure => unreachable
        return results + [
            CheckResult(
                f"{name} serves",
                False,
                f"unreachable at {fqdn}:8080 ({type(e).__name__}: {e}); "
                f"ensure Tailscale is connected and the proxy node is up",
            )
        ]

    if served != expected:
        return results + [
            CheckResult(
                f"{name} serves",
                False,
                f"content mismatch for {bucket}/{key}: proxy returned "
                f"{len(served)} bytes (HTTP {status}), S3 returned {len(expected)}",
            )
        ]
    return results + [
        CheckResult(
            f"{name} serves",
            True,
            f"served {len(served)} bytes of s3://{bucket}/{key} via {fqdn}:8080, "
            f"bytes match S3 (HTTP {status})",
        )
    ]


def _verify_docker_proxy(name: str, proxy, region: str) -> List[CheckResult]:
    """Check the DockerHub proxy's prerequisites and liveness:
      - the DockerHub PAT is readable from SSM (the instance needs it at boot);
      - the mirror S3 bucket is reachable with the current credentials;
      - the ASG has an InService instance (best effort).

    The live /v2/ endpoint is only reachable from inside the VPC, so it is not
    probed here."""
    from ._utils import aws_client

    if not region:
        return [
            CheckResult(
                f"{name}",
                False,
                "no region configured (set Settings.AWS_REGION)",
            )
        ]

    results: List[CheckResult] = []

    # 1) DockerHub PAT present in SSM.
    try:
        ssm = aws_client("ssm", region, context="docker-proxy-verify")
        value = ssm.get_parameter(
            Name=proxy.dockerhub_pat_ssm, WithDecryption=True
        )["Parameter"]["Value"]
        ok = bool(value and value.strip())
        results.append(
            CheckResult(
                f"{name} dockerhub-pat",
                ok,
                f"read {proxy.dockerhub_pat_ssm} (len={len(value or '')})"
                if ok
                else f"{proxy.dockerhub_pat_ssm} is empty",
            )
        )
    except Exception as e:  # noqa: BLE001
        results.append(
            CheckResult(
                f"{name} dockerhub-pat",
                False,
                f"cannot read {proxy.dockerhub_pat_ssm} from SSM: {e}",
            )
        )

    # 2) Mirror bucket reachable.
    try:
        s3 = aws_client("s3", region, context="docker-proxy-verify")
        s3.head_bucket(Bucket=proxy.s3_bucket)
        results.append(
            CheckResult(
                f"{name} s3-bucket", True, f"s3://{proxy.s3_bucket} reachable"
            )
        )
    except Exception as e:  # noqa: BLE001
        results.append(
            CheckResult(
                f"{name} s3-bucket",
                False,
                f"cannot reach s3://{proxy.s3_bucket}: {e}",
            )
        )

    # 3) ASG has an InService instance.
    try:
        asg = aws_client("autoscaling", region, context="docker-proxy-verify")
        groups = asg.describe_auto_scaling_groups(
            AutoScalingGroupNames=[proxy.autoscaling_group.name]
        ).get("AutoScalingGroups", [])
        instances = groups[0].get("Instances", []) if groups else []
        in_service = [i for i in instances if i.get("LifecycleState") == "InService"]
        ok = bool(in_service)
        results.append(
            CheckResult(
                f"{name} asg",
                ok,
                f"{len(in_service)}/{len(instances)} instance(s) InService"
                if groups
                else f"ASG {proxy.autoscaling_group.name} not found",
            )
        )
    except Exception as e:  # noqa: BLE001
        results.append(
            CheckResult(f"{name} asg", False, f"cannot describe ASG: {e}")
        )

    # 4) The instance self-registered its A record in the private zone. A missing
    # record means the running instance never booted the current user_data (e.g.
    # the ASG was not rolled after a config change), so runners cannot resolve it.
    if proxy.dns_zone and proxy.dns_record:
        try:
            r53 = aws_client("route53", region, context="docker-proxy-verify")
            zone_fqdn = proxy.dns_zone.rstrip(".") + "."
            zones = [
                z
                for z in r53.list_hosted_zones_by_name(DNSName=zone_fqdn).get(
                    "HostedZones", []
                )
                if z.get("Name") == zone_fqdn and z.get("Config", {}).get("PrivateZone")
            ]
            if not zones:
                results.append(
                    CheckResult(
                        f"{name} dns", False, f"private zone {proxy.dns_zone} not found"
                    )
                )
            else:
                rec_fqdn = proxy.dns_record.rstrip(".") + "."
                rr = r53.list_resource_record_sets(
                    HostedZoneId=zones[0]["Id"],
                    StartRecordName=rec_fqdn,
                    StartRecordType="A",
                    MaxItems="1",
                ).get("ResourceRecordSets", [])
                match = [
                    r
                    for r in rr
                    if r.get("Name") == rec_fqdn and r.get("Type") == "A"
                ]
                values = [
                    v.get("Value")
                    for r in match
                    for v in r.get("ResourceRecords", [])
                ]
                results.append(
                    CheckResult(
                        f"{name} dns",
                        bool(values),
                        f"{proxy.dns_record} -> {', '.join(values)}"
                        if values
                        else f"{proxy.dns_record} A record missing "
                        "(instance not registered — roll the ASG instance?)",
                    )
                )
        except Exception as e:  # noqa: BLE001
            results.append(
                CheckResult(f"{name} dns", False, f"cannot check DNS record: {e}")
            )

    return results


def _verify_tailscale_auth(name: str, proxy, region: str) -> CheckResult:
    """Confirm the proxy's Tailscale OAuth client can mint an auth key with the
    configured `tailscale_tag`. This mirrors exactly what the boot script does
    (s3_proxy_user_data.sh): a valid client but an unauthorized tag makes the
    node silently fail to join the tailnet. The check exchanges the OAuth creds
    from SSM for a token, mints a throwaway ephemeral key with the tag, and then
    deletes it — so it catches bad creds AND a wrong tag before any redeploy,
    from anywhere (no tailnet required)."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    from ._utils import aws_client

    tag = (proxy.tailscale_tag or "").strip()
    if not tag:
        return CheckResult(name, False, "no tailscale_tag configured")

    try:
        ssm = aws_client("ssm", region, context="tailscale-auth-verify")
        client_id = ssm.get_parameter(
            Name=proxy.tailscale_oauth_client_id_ssm, WithDecryption=True
        )["Parameter"]["Value"]
        client_secret = ssm.get_parameter(
            Name=proxy.tailscale_oauth_client_secret_ssm, WithDecryption=True
        )["Parameter"]["Value"]
    except Exception as e:  # noqa: BLE001
        return CheckResult(name, False, f"cannot read OAuth client from SSM: {e}")

    def _post(url, data, headers):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    # 1. OAuth client credentials -> access token.
    try:
        token_body = urllib.parse.urlencode(
            {"client_id": client_id, "client_secret": client_secret}
        ).encode()
        access_token = _post(
            "https://api.tailscale.com/api/v2/oauth/token",
            token_body,
            {"Content-Type": "application/x-www-form-urlencoded"},
        ).get("access_token")
    except Exception as e:  # noqa: BLE001
        return CheckResult(name, False, f"OAuth token exchange failed: {e}")
    if not access_token:
        return CheckResult(name, False, "OAuth token exchange returned no access_token")

    # 2. Mint a throwaway ephemeral key with the configured tag (same shape as
    #    the boot script). An unauthorized tag fails here.
    key_body = json.dumps(
        {
            "capabilities": {
                "devices": {
                    "create": {
                        "reusable": False,
                        "ephemeral": True,
                        "preauthorized": True,
                        "tags": [tag],
                    }
                }
            },
            "expirySeconds": 300,
        }
    ).encode()
    try:
        minted = _post(
            "https://api.tailscale.com/api/v2/tailnet/-/keys",
            key_body,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200] if hasattr(e, "read") else ""
        return CheckResult(
            name,
            False,
            f"cannot mint auth key with tag '{tag}' (HTTP {e.code}): {detail}",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(name, False, f"cannot mint auth key with tag '{tag}': {e}")

    key_id = minted.get("id")
    # 3. Clean up the throwaway key so the check leaves no trace.
    if key_id:
        try:
            delete = urllib.request.Request(
                f"https://api.tailscale.com/api/v2/tailnet/-/keys/{key_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                method="DELETE",
            )
            urllib.request.urlopen(delete, timeout=15).close()
        except Exception:  # noqa: BLE001 - best-effort cleanup; key is ephemeral+short-lived
            pass
    return CheckResult(name, True, f"OAuth client can mint keys for tag '{tag}'")
