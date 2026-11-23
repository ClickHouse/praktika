#!/usr/bin/env bash
# Praktika S3 report proxy bootstrap (Amazon Linux 2023). Serves PRIVATE S3
# report buckets to the tailnet and hosts the live-debug ttyd terminal. Path:
# tailscale serve (TLS :443 + Tailscale-User identity headers) -> Caddy (loopback
# router, JSON access log) -> signer.py SigV4 -> S3, and -> ttyd for /term. Joins
# the tailnet with an ephemeral SSM-minted auth key; no static credentials on disk.
set -xeuo pipefail

# --- Resolve region from IMDSv2 (launch template forces HttpTokens=required) ---
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region)
export AWS_DEFAULT_REGION="$REGION"

# --- Packages: jq, openssh-clients (ssh/ssh-keygen for the live-debug wrapper),
#     and python3.13. The base image's python3 is 3.9, but the praktika package
#     uses 3.10+ `X | None` type unions, so the signer + jump-host wrapper -- run
#     as `python3.13 -m praktika...` from the wheel below -- need a newer
#     interpreter. ---
dnf install -y jq openssh-clients python3.13 python3.13-pip
# Install praktika from the wheel with the `infrastructure` extra so the whole
# praktika.infrastructure.native import chain resolves (boto3 + PyJWT). No inlining
# into user_data -- the proxy tracks the same versioned wheel as the runners.
python3.13 -m pip install --quiet "praktika[infrastructure] @ __PRAKTIKA_WHL__"

# --- Read the Tailscale OAuth client from SSM (granted by the instance role) ---
# Stop `set -x` tracing for the credential-handling block below: it would expand
# the OAuth token / auth key into the trace, which lands in EC2 console output.
# `set -e` (errexit) stays in effect; only the `x` (xtrace) flag is toggled.
set +x
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
set -x  # resume tracing; nothing below handles secrets
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

# --- SigV4 signing proxy (from the praktika wheel): signs with the instance
#     role, streams from S3 ---
cat > /etc/systemd/system/praktika-s3-signer.service <<UNIT
[Unit]
Description=Praktika S3 SigV4 signing proxy
After=network-online.target
Wants=network-online.target
[Service]
Environment=PRAKTIKA_S3_PROXY_BUCKETS=__PROXIED_BUCKETS__
Environment=AWS_DEFAULT_REGION=${REGION}
ExecStart=/usr/bin/python3.13 -m praktika.infrastructure.native.s3_proxy_signer 127.0.0.1 8081
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now praktika-s3-signer

# --- Live job debug: ttyd web terminal + per-session jump-host wrapper ---
# ttyd spawns `python3.13 -m praktika.infrastructure.native.debug_connect <id>`
# per browser connection (URL arg = runner instance id; wrapper installed from the
# praktika wheel above). ttyd ships a fully-static per-arch binary; uname -m
# matches its release names.
TTYD_VERSION=1.7.7
curl -fsSL -o /usr/local/bin/ttyd \
  "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.$(uname -m)"
chmod +x /usr/local/bin/ttyd

install -d /opt/praktika-debug
# Cap on disk for the direct-SSH path (runs debug_connect outside the ttyd unit,
# so it can't read the unit's PRAKTIKA_DEBUG_MAX_SESSIONS env).
printf '%s\n' "__MAX_DEBUG_SESSIONS__" > /opt/praktika-debug/max_sessions

cat > /etc/systemd/system/praktika-debug-ttyd.service <<UNIT
[Unit]
Description=Praktika live-debug web terminal (ttyd jump host)
After=network-online.target
Wants=network-online.target
[Service]
Environment=AWS_DEFAULT_REGION=${REGION}
# Global concurrent-session cap, enforced in debug_connect.py (see there).
Environment=PRAKTIKA_DEBUG_MAX_SESSIONS=__MAX_DEBUG_SESSIONS__
# -a: pass the URL ?arg= query to the command; -W: writable (interactive);
# -b /term: base path fronted by Caddy; bound to loopback (Caddy owns TLS/auth).
# -O: reject WebSocket upgrades whose Origin != Host, blocking cross-site
# WebSocket hijacking -- otherwise any page a tailnet member visits could drive a
# terminal in their browser and get RCE on a runner. serve/Caddy preserve the
# Host + Origin headers, so the same-origin /term page still connects.
ExecStart=/usr/local/bin/ttyd -p 7681 -i 127.0.0.1 -b /term -a -W -O \
  -t disableLeaveAlert=true \
  /usr/bin/python3.13 -m praktika.infrastructure.native.debug_connect
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now praktika-debug-ttyd

# --- Caddy: loopback router behind `tailscale serve`. Routes /term* -> ttyd and
#     report GETs -> the signer, and ACCESS-LOGS every request as JSON (including
#     the Tailscale-User-Login header that `tailscale serve` injects) for per-user
#     attribution. TLS + identity live in `tailscale serve` below, so Caddy
#     listens only on loopback and terminates no TLS. ---
case "$(uname -m)" in
  aarch64) CADDY_ARCH=arm64 ;;
  x86_64)  CADDY_ARCH=amd64 ;;
  *)       CADDY_ARCH=amd64 ;;
esac
curl -fsSL -o /usr/local/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=${CADDY_ARCH}"
chmod +x /usr/local/bin/caddy
id caddy &>/dev/null || useradd --system --home /var/lib/caddy --shell /sbin/nologin caddy
install -d /etc/caddy
# Caddy's data dir (home) and log dir, owned by the caddy user. useradd --system
# does not create the home, and Caddy writes state (e.g. its local CA) there.
install -d -o caddy -g caddy /var/lib/caddy /var/log/caddy

cat > /etc/caddy/Caddyfile <<'CADDY'
{
	admin off
	# tailscale serve terminates TLS; Caddy is plain HTTP on loopback. Disable
	# automatic HTTPS so Caddy neither manages certs nor runs HTTP->HTTPS redirects.
	auto_https off
}
:8080 {
	# Listen on loopback only (tailscale serve proxies here); match ANY Host,
	# since serve forwards the original FQDN Host, not 127.0.0.1. A `127.0.0.1:8080`
	# site address would instead host-match 127.0.0.1 and 400 serve's requests.
	bind 127.0.0.1
	# Per-request JSON access log. request.uri carries /term?arg=<runner> and
	# request.headers includes Tailscale-User-Login (who connected) -> one
	# self-describing audit line per debug session. Shipped to CloudWatch below.
	log {
		output file /var/log/caddy/access.log {
			roll_size 20MiB
			roll_keep 5
		}
		format json
	}
	# Live job debug terminal (ttyd). More specific than the method matcher, so
	# /term* routes here first; reverse_proxy upgrades the WebSocket.
	handle /term* {
		reverse_proxy 127.0.0.1:7681
	}
	@get_head method GET HEAD
	handle @get_head {
		reverse_proxy 127.0.0.1:8081
	}
	# Anything not GET/HEAD (and not /term) is rejected.
	handle {
		respond 405
	}
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
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now caddy

# --- Expose on the tailnet via `tailscale serve`: terminates TLS on :443 with a
#     Tailscale-managed cert (nothing to renew) and injects verified
#     Tailscale-User-* identity headers into each request proxied to Caddy, which
#     Caddy logs above. Tailnet-only -- serve never listens on the VPC ENI. ---
tailscale serve --bg 8080

# --- Ship the Caddy access log (the per-user /term audit trail) to CloudWatch via
#     the instance role (CloudWatchAgentServerPolicy) ---
dnf install -y amazon-cloudwatch-agent
cat > /etc/praktika-cw-agent.json <<CWCFG
{
	"agent": { "region": "${REGION}" },
	"logs": {
		"logs_collected": {
			"files": {
				"collect_list": [
					{
						"file_path": "/var/log/caddy/access.log",
						"log_group_name": "__CW_LOG_GROUP__",
						"log_stream_name": "{instance_id}",
						"retention_in_days": 90
					}
				]
			}
		}
	}
}
CWCFG
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -c file:/etc/praktika-cw-agent.json -s
