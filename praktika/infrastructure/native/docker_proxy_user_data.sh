#!/usr/bin/env bash
# Praktika DockerHub proxy bootstrap (Amazon Linux 2023).
#
# Runs a single zot instance as a DockerHub pull-through cache backed by S3:
#   runner --[registry-mirror]--> zot (:PORT) --> DockerHub (first pull only)
#                                    |
#                                    v
#                          S3 (manifests + blobs)
#
# zot serves DockerHub images at their native library/... paths, so Docker's
# registry-mirror works with no image-reference rewrites. manifestCheckInterval
# makes cached tags serve from S3 without re-contacting DockerHub. The DockerHub
# PAT is read from SSM at boot; S3 uses the EC2 instance role. No static
# credentials are written to disk except the short-lived sync-auth.json (0600).
set -xeuo pipefail

# --- Resolve region + private IP from IMDSv2 ---
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
imds() { curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
REGION=$(imds placement/region)
PRIVATE_IP=$(imds local-ipv4)
export AWS_DEFAULT_REGION="$REGION"

# --- Packages: jq for JSON (aws CLI ships with AL2023) ---
dnf install -y jq

# --- Read the DockerHub PAT from SSM (granted by the instance role) ---
DOCKERHUB_PAT=$(aws ssm get-parameter --with-decryption \
  --name "__DOCKERHUB_PAT_SSM__" --query Parameter.Value --output text)
DOCKERHUB_HOST=$(printf '%s' "__UPSTREAM_URL__" | sed -E 's#^https?://##; s#/.*##')

# --- Dedicated system user + directories ---
id zot &>/dev/null || useradd --system --home /var/lib/zot --shell /sbin/nologin zot
install -d -o zot -g zot /var/lib/zot
install -d -o zot -g zot /etc/zot
# Staging dir for sync (required by zot when combining S3 storage + sync).
install -d -o zot -g zot /var/lib/zot/.sync

# --- zot config (no secrets) ---
cat > /etc/zot/config.json <<'ZOTCONF'
{
  "storage": {
    "rootDirectory": "/var/lib/zot",
    "dedupe": false,
    "gc": true,
    "storageDriver": {
      "name": "s3",
      "region": "__S3_REGION__",
      "bucket": "__S3_BUCKET__",
      "rootdirectory": "__S3_ROOTDIR__",
      "secure": true
    }
  },
  "http": {
    "address": "0.0.0.0",
    "port": "__LISTEN_PORT__",
    "compat": ["docker2s2"]
  },
  "log": { "level": "info" },
  "extensions": {
    __EXTRA_EXTENSIONS__"sync": {
      "enable": true,
      "downloadDir": "/var/lib/zot/.sync",
      "credentialsFile": "/etc/zot/sync-auth.json",
      "registries": [
        {
          "urls": ["__UPSTREAM_URL__"],
          "onDemand": true,
          "tlsVerify": true,
          "manifestCheckInterval": "__MANIFEST_CHECK_INTERVAL__",
          "maxRetries": 3,
          "retryDelay": "5m",
          "content": [{ "prefix": "**" }]
        }
      ]
    }
  }
}
ZOTCONF

# --- Sync credentials (DockerHub PAT), 0600, owned by zot ---
umask 077
cat > /etc/zot/sync-auth.json <<EOF
{ "${DOCKERHUB_HOST}": { "username": "__DOCKERHUB_USERNAME__", "password": "${DOCKERHUB_PAT}" } }
EOF
umask 022
chown zot:zot /etc/zot/config.json /etc/zot/sync-auth.json
chmod 600 /etc/zot/sync-auth.json

# --- Install the zot binary (full build; includes the sync extension) ---
case "$(uname -m)" in
  aarch64) ZOT_ARCH=arm64 ;;
  x86_64)  ZOT_ARCH=amd64 ;;
  *)       ZOT_ARCH=amd64 ;;
esac
curl -fsSL -o /usr/local/bin/zot \
  "https://github.com/project-zot/zot/releases/download/__ZOT_VERSION__/zot-linux-${ZOT_ARCH}"
chmod +x /usr/local/bin/zot

cat > /etc/systemd/system/zot.service <<UNIT
[Unit]
Description=zot OCI registry (DockerHub pull-through cache)
After=network-online.target
Wants=network-online.target
[Service]
User=zot
Group=zot
Environment=AWS_REGION=${REGION}
Environment=AWS_DEFAULT_REGION=${REGION}
ExecStart=/usr/local/bin/zot serve /etc/zot/config.json
Restart=always
RestartSec=2
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now zot

# --- Route53 self-registration: UPSERT the A record on boot, DELETE on shutdown ---
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name __DNS_ZONE__ \
  --query 'HostedZones[0].Id' --output text)
aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"__DNS_RECORD__.","Type":"A","TTL":60,"ResourceRecords":[{"Value":"'"${PRIVATE_IP}"'"}]}}]}'

cat > /etc/systemd/system/dockerhub-proxy-dns-cleanup.service <<UNIT
[Unit]
Description=Remove dockerhub-proxy Route53 record on shutdown
DefaultDependencies=no
Before=shutdown.target reboot.target halt.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStop=/usr/bin/aws route53 change-resource-record-sets --hosted-zone-id ${ZONE_ID} --change-batch '{"Changes":[{"Action":"DELETE","ResourceRecordSet":{"Name":"__DNS_RECORD__.","Type":"A","TTL":60,"ResourceRecords":[{"Value":"${PRIVATE_IP}"}]}}]}'

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now dockerhub-proxy-dns-cleanup.service
__TAILSCALE_SETUP__
