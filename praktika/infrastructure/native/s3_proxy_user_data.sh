#!/usr/bin/env bash
# Praktika S3 report proxy bootstrap (Amazon Linux 2023). Serves PRIVATE S3
# report buckets to the tailnet (Caddy TLS -> signer.py SigV4 -> S3) and hosts
# the live-debug ttyd terminal. Joins the tailnet with an ephemeral SSM-minted
# auth key; no static credentials on disk.
set -xeuo pipefail

# --- Resolve region from IMDSv2 (launch template forces HttpTokens=required) ---
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region)
export AWS_DEFAULT_REGION="$REGION"

# --- Packages: jq for JSON, pip for boto3 (used by the signer and the live-debug
#     wrapper), openssh-clients for the ssh/ssh-keygen the wrapper shells out to ---
dnf install -y jq python3-pip openssh-clients
pip3 install --quiet boto3

# --- Read the Tailscale OAuth client from SSM (granted by the instance role) ---
TS_CLIENT_ID=$(aws ssm get-parameter --with-decryption \
  --name "__TS_OAUTH_CLIENT_ID_SSM__" --query Parameter.Value --output text)
TS_CLIENT_SECRET=$(aws ssm get-parameter --with-decryption \
  --name "__TS_OAUTH_CLIENT_SECRET_SSM__" --query Parameter.Value --output text)

# --- Mint an ephemeral, pre-authorized, tagged auth key via the Tailscale API ---
TS_ACCESS_TOKEN=$(curl -s https://api.tailscale.com/api/v2/oauth/token \
  -d "client_id=$TS_CLIENT_ID" -d "client_secret=$TS_CLIENT_SECRET" | jq -r .access_token)
TS_AUTHKEY=$(curl -s "https://api.tailscale.com/api/v2/tailnet/-/keys" \
  -H "Authorization: Bearer $TS_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"capabilities":{"devices":{"create":{"reusable":false,"ephemeral":true,"preauthorized":true,"tags":["__TS_TAG__"]}}},"expirySeconds":600}' \
  | jq -r .key)

# --- Release the target hostname before joining: delete any device holding
#     "__TS_HOSTNAME__" / "__TS_HOSTNAME__-N" (left by an ungraceful prior
#     termination) so Tailscale doesn't append "-1" and drift the stable FQDN.
#     Needs device-delete scope on the OAuth client. ---
for TS_DID in $(curl -s "https://api.tailscale.com/api/v2/tailnet/-/devices" \
  -H "Authorization: Bearer $TS_ACCESS_TOKEN" \
  | jq -r --arg h "__TS_HOSTNAME__" '.devices[] | select(.hostname == $h or (.hostname | test("^" + $h + "-[0-9]+$"))) | .id'); do
  curl -s -o /dev/null -X DELETE "https://api.tailscale.com/api/v2/device/$TS_DID" \
    -H "Authorization: Bearer $TS_ACCESS_TOKEN" || true
done

# --- Install Tailscale and join the tailnet ---
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --auth-key="$TS_AUTHKEY" --hostname="__TS_HOSTNAME__"
TS_FQDN=$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')

# --- Deregister on shutdown so the stable hostname is freed for the replacement ---
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

# --- TLS certificate from Tailscale (renewed by a daily systemd timer below) ---
mkdir -p /etc/caddy/tls
tailscale cert --cert-file "/etc/caddy/tls/${TS_FQDN}.crt" \
  --key-file "/etc/caddy/tls/${TS_FQDN}.key" "$TS_FQDN"

# --- SigV4 signing proxy: signs with the instance role, streams from S3 ---
install -d /opt/praktika-s3-proxy
base64 -d <<'SIGNER_B64' | gunzip > /opt/praktika-s3-proxy/signer.py
__SIGNER_PY_B64__
SIGNER_B64
cat > /etc/systemd/system/praktika-s3-signer.service <<UNIT
[Unit]
Description=Praktika S3 SigV4 signing proxy
After=network-online.target
Wants=network-online.target
[Service]
Environment=PRAKTIKA_S3_PROXY_BUCKETS=__PROXIED_BUCKETS__
Environment=AWS_DEFAULT_REGION=${REGION}
ExecStart=/usr/bin/python3 /opt/praktika-s3-proxy/signer.py 127.0.0.1 8081
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now praktika-s3-signer

# --- Live job debug: ttyd web terminal + per-session jump-host wrapper ---
# ttyd spawns a fresh debug_connect.py per browser connection (URL arg = runner
# instance id). The wrapper validates the target is a running job runner, mints
# an ephemeral EC2 Instance Connect key, and ssh'es into the runner over the VPC.
# ttyd ships a fully-static per-arch binary; uname -m matches its release names.
TTYD_VERSION=1.7.7
curl -fsSL -o /usr/local/bin/ttyd \
  "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.$(uname -m)"
chmod +x /usr/local/bin/ttyd

install -d /opt/praktika-debug
base64 -d <<'DEBUG_CONNECT_B64' | gunzip > /opt/praktika-debug/debug_connect.py
__DEBUG_CONNECT_B64__
DEBUG_CONNECT_B64

cat > /etc/systemd/system/praktika-debug-ttyd.service <<UNIT
[Unit]
Description=Praktika live-debug web terminal (ttyd jump host)
After=network-online.target
Wants=network-online.target
[Service]
Environment=AWS_DEFAULT_REGION=${REGION}
# PRAKTIKA_DEBUG_MAX_SESSIONS: GLOBAL cap on concurrent debug sessions on this
# jump host (across all runners/users), from S3Proxy.max_debug_sessions (0 ==
# unlimited). Enforced inside debug_connect.py so an over-limit viewer gets a
# readable banner in the terminal rather than ttyd's bare reconnect button.
Environment=PRAKTIKA_DEBUG_MAX_SESSIONS=__MAX_DEBUG_SESSIONS__
# -a: pass the URL ?arg= query to the command; -W: writable (interactive);
# -b /term: base path fronted by Caddy; bound to loopback (Caddy owns TLS/auth).
ExecStart=/usr/local/bin/ttyd -p 7681 -i 127.0.0.1 -b /term -a -W \
  -t disableLeaveAlert=true \
  /usr/bin/python3 /opt/praktika-debug/debug_connect.py
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now praktika-debug-ttyd

# --- Caddy (static binary) fronting the signer: TLS :443 + HTTP :8080, GET/HEAD only ---
case "$(uname -m)" in
  aarch64) CADDY_ARCH=arm64 ;;
  x86_64)  CADDY_ARCH=amd64 ;;
  *)       CADDY_ARCH=amd64 ;;
esac
curl -fsSL -o /usr/local/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=${CADDY_ARCH}"
chmod +x /usr/local/bin/caddy
id caddy &>/dev/null || useradd --system --home /var/lib/caddy --shell /sbin/nologin caddy
chown -R caddy:caddy /etc/caddy/tls

cat > /etc/caddy/Caddyfile <<CADDY
{
	admin off
}
(handlers) {
	# Live job debug terminal (ttyd). More specific than the method matcher
	# below, so Caddy routes /term* here first. reverse_proxy upgrades the
	# WebSocket automatically. Tailnet-only, same reachability as reports.
	handle /term* {
		reverse_proxy 127.0.0.1:7681
	}
	@get_head method GET HEAD
	handle @get_head {
		reverse_proxy 127.0.0.1:8081
	}
	# Anything that is not GET/HEAD falls through to this mutually-exclusive
	# handle block and is rejected.
	handle {
		respond 405
	}
}
:8080 {
	import handlers
}
${TS_FQDN}:443 {
	tls /etc/caddy/tls/${TS_FQDN}.crt /etc/caddy/tls/${TS_FQDN}.key
	import handlers
}
CADDY

cat > /etc/systemd/system/caddy.service <<UNIT
[Unit]
Description=Caddy
After=network-online.target praktika-s3-signer.service
Wants=network-online.target
[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=always
RestartSec=2
AmbientCapabilities=CAP_NET_BIND_SERVICE
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now caddy

# --- Renew the Tailscale TLS cert daily (certs expire ~90d; ASG also refreshes
#     the cert whenever it replaces the instance) ---
cat > /usr/local/bin/praktika-renew-tailscale-cert <<'RENEW'
#!/usr/bin/env bash
set -euo pipefail
TS_FQDN=$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')
tailscale cert --cert-file "/etc/caddy/tls/${TS_FQDN}.crt" \
  --key-file "/etc/caddy/tls/${TS_FQDN}.key" "$TS_FQDN"
chown -R caddy:caddy /etc/caddy/tls
systemctl reload caddy
RENEW
chmod +x /usr/local/bin/praktika-renew-tailscale-cert
cat > /etc/systemd/system/praktika-renew-tailscale-cert.service <<UNIT
[Unit]
Description=Renew Tailscale TLS certificate for the Praktika S3 proxy
[Service]
Type=oneshot
ExecStart=/usr/local/bin/praktika-renew-tailscale-cert
UNIT
cat > /etc/systemd/system/praktika-renew-tailscale-cert.timer <<UNIT
[Unit]
Description=Daily Tailscale TLS certificate renewal
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now praktika-renew-tailscale-cert.timer
