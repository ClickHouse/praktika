from pathlib import Path

_HERE = Path(__file__).parent


def cidb_user_data(vpc_cidr, admin_password_ssm_name, replica_name):
    """Render the CI DB node bootstrap script.

    Inlines the schema SQL (gzip+base64-encoded) so the resulting script is
    self-contained — the EC2 instance does not need to reach back into S3
    or any praktika package on first boot.
    """
    import base64
    import gzip

    schema_sql = (_HERE / "cidb_schema.sql").read_text()
    template = (_HERE / "user_data_cidb.sh").read_text()
    placeholders = (
        "__VPC_CIDR__",
        "__ADMIN_PASSWORD_SSM_NAME__",
        "__SCHEMA_SQL_B64__",
        "__REPLICA_NAME__",
    )
    for ph in placeholders:
        if ph not in template:
            raise RuntimeError(f"user_data_cidb.sh is missing {ph}")
    schema_b64 = base64.b64encode(
        gzip.compress(schema_sql.encode("utf-8"), mtime=0)
    ).decode("ascii")
    return (
        template
        .replace("__VPC_CIDR__", vpc_cidr)
        .replace("__ADMIN_PASSWORD_SSM_NAME__", admin_password_ssm_name)
        .replace("__SCHEMA_SQL_B64__", schema_b64)
        .replace("__REPLICA_NAME__", replica_name)
    )


def s3_proxy_user_data(
    hostname,
    tailscale_tag,
    oauth_client_id_ssm,
    oauth_client_secret_ssm,
    proxied_buckets,
):
    """Render the S3 report proxy bootstrap script.

    Inlines the SigV4 signer (base64-encoded) so the instance is self-contained
    on first boot. The proxied bucket allowlist is passed to the signer via an
    environment variable rendered into its systemd unit.
    """
    import base64

    signer_py = (_HERE / "s3_proxy_signer.py").read_text()
    template = (_HERE / "s3_proxy_user_data.sh").read_text()
    placeholders = (
        "__TS_OAUTH_CLIENT_ID_SSM__",
        "__TS_OAUTH_CLIENT_SECRET_SSM__",
        "__TS_TAG__",
        "__TS_HOSTNAME__",
        "__PROXIED_BUCKETS__",
        "__SIGNER_PY_B64__",
    )
    for ph in placeholders:
        if ph not in template:
            raise RuntimeError(f"s3_proxy_user_data.sh is missing {ph}")
    signer_b64 = base64.b64encode(signer_py.encode("utf-8")).decode("ascii")
    return (
        template
        .replace("__TS_OAUTH_CLIENT_ID_SSM__", oauth_client_id_ssm)
        .replace("__TS_OAUTH_CLIENT_SECRET_SSM__", oauth_client_secret_ssm)
        .replace("__TS_TAG__", tailscale_tag)
        .replace("__TS_HOSTNAME__", hostname)
        .replace("__PROXIED_BUCKETS__", " ".join(proxied_buckets))
        .replace("__SIGNER_PY_B64__", signer_b64)
    )


def _docker_proxy_tailscale_setup(tailscale, listen_port):
    """Render the Tailscale bootstrap block (or "" when not configured). Mints a
    tagged ephemeral auth key from the SSM OAuth client, joins the tailnet with
    SSH, and serves the registry at root over HTTPS via ``tailscale serve``."""
    if not tailscale:
        return ""
    hostname = str(tailscale["hostname"])
    tag = str(tailscale["tag"])
    id_ssm = str(tailscale["oauth_client_id_ssm"])
    secret_ssm = str(tailscale["oauth_client_secret_ssm"])
    return f"""
# --- Tailscale: join the tailnet (+SSH) and serve the registry at root over HTTPS ---
TS_CLIENT_ID=$(aws ssm get-parameter --with-decryption --name "{id_ssm}" --query Parameter.Value --output text)
TS_CLIENT_SECRET=$(aws ssm get-parameter --with-decryption --name "{secret_ssm}" --query Parameter.Value --output text)
TS_ACCESS_TOKEN=$(curl -s https://api.tailscale.com/api/v2/oauth/token \\
  -d "client_id=$TS_CLIENT_ID" -d "client_secret=$TS_CLIENT_SECRET" | jq -r .access_token)
TS_AUTHKEY=$(curl -s "https://api.tailscale.com/api/v2/tailnet/-/keys" \\
  -H "Authorization: Bearer $TS_ACCESS_TOKEN" -H "Content-Type: application/json" \\
  -d '{{"capabilities":{{"devices":{{"create":{{"reusable":false,"ephemeral":true,"preauthorized":true,"tags":["{tag}"]}}}}}},"expirySeconds":600}}' \\
  | jq -r .key)
# Forcefully release the target hostname before joining: delete any device that
# still holds "{hostname}" (or a "{hostname}-N" variant left by an ungraceful
# prior termination where logout-on-shutdown never ran). Without this, Tailscale
# would append "-1" and the stable MagicDNS name would drift. Requires the OAuth
# client to have device-delete scope.
for TS_DID in $(curl -s "https://api.tailscale.com/api/v2/tailnet/-/devices" \
  -H "Authorization: Bearer $TS_ACCESS_TOKEN" \
  | jq -r --arg h "{hostname}" '.devices[] | select(.hostname == $h or (.hostname | test("^" + $h + "-[0-9]+$"))) | .id'); do
  curl -s -o /dev/null -X DELETE "https://api.tailscale.com/api/v2/device/$TS_DID" \
    -H "Authorization: Bearer $TS_ACCESS_TOKEN" || true
done
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --auth-key="$TS_AUTHKEY" --hostname="{hostname}"
# Serve the registry (UI at /, API at /v2/) at https://{hostname}.<tailnet>.ts.net/
tailscale serve --bg {listen_port}
# Deregister this ephemeral node on shutdown so the stable hostname is freed
# immediately for the replacement (otherwise Tailscale keeps the offline device
# for a while and the next node collides onto {hostname}-1).
cat > /etc/systemd/system/tailscale-logout.service <<'UNIT'
[Unit]
Description=Tailscale logout on shutdown (deregister ephemeral node)
DefaultDependencies=no
Before=shutdown.target reboot.target halt.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStop=/usr/bin/tailscale logout
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now tailscale-logout.service
"""


def docker_proxy_user_data(
    zot_version,
    s3_bucket,
    s3_region,
    s3_rootdirectory,
    upstream_url,
    dockerhub_pat_ssm,
    dockerhub_username,
    manifest_check_interval,
    listen_port,
    dns_zone,
    dns_record,
    enable_ui=False,
    tailscale=None,
):
    """Render the DockerHub proxy bootstrap script.

    Substitutes the zot version, S3 storage settings, upstream + credentials
    source, manifest cache window, listen port and Route53 self-registration
    identities into the template. Contains no secrets: the DockerHub PAT is
    fetched from SSM on the instance at boot.

    ``enable_ui`` adds zot's ``search`` + ``ui`` extensions (served at ``/`` on the
    same port). No CVE/trivy scanning is enabled, so it stays lightweight.

    ``tailscale`` (a dict with ``hostname``, ``tag``, ``oauth_client_id_ssm``,
    ``oauth_client_secret_ssm``) opts the node into Tailscale: it mints a tagged
    ephemeral auth key from the SSM OAuth client, joins the tailnet with SSH, and
    ``tailscale serve``s the registry at root over HTTPS. None => no Tailscale.
    """
    template = (_HERE / "docker_proxy_user_data.sh").read_text()
    # zot's extensions are a JSON object; UI needs search enabled. Rendered
    # before the "sync" key, so keep the trailing comma.
    extra_extensions = (
        '"ui": {"enable": true}, "search": {"enable": true}, ' if enable_ui else ""
    )
    tailscale_setup = _docker_proxy_tailscale_setup(tailscale, listen_port)
    replacements = {
        "__ZOT_VERSION__": str(zot_version),
        "__S3_BUCKET__": str(s3_bucket),
        "__S3_REGION__": str(s3_region),
        "__S3_ROOTDIR__": str(s3_rootdirectory),
        "__UPSTREAM_URL__": str(upstream_url),
        "__DOCKERHUB_PAT_SSM__": str(dockerhub_pat_ssm),
        "__DOCKERHUB_USERNAME__": str(dockerhub_username),
        "__MANIFEST_CHECK_INTERVAL__": str(manifest_check_interval),
        "__LISTEN_PORT__": str(listen_port),
        "__DNS_ZONE__": str(dns_zone),
        "__DNS_RECORD__": str(dns_record),
        "__EXTRA_EXTENSIONS__": extra_extensions,
        "__TAILSCALE_SETUP__": tailscale_setup,
    }
    for placeholder in replacements:
        if placeholder not in template:
            raise RuntimeError(f"docker_proxy_user_data.sh is missing {placeholder}")
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered
