#!/usr/bin/env python3
"""Per-session jump-host wrapper for the Praktika live-debug shell.

ttyd (or a direct ssh) runs this per connection with the target runner's EC2
instance id as argv[1] (from the untrusted ``/term/?arg=<id>`` URL). It: checks
the id format; confirms via ``ec2:DescribeInstances`` the target is a *running*
``praktika_role=job_runner`` (refusing the proxy/CIDB/anything else); mints an
ephemeral (~60s) ed25519 key, pushes it with EC2 Instance Connect, and ssh's
into the runner's private IP over the shared VPC. Login user varies by pool AMI
(AL2023 => ec2-user, Ubuntu => ubuntu), so we push+probe each candidate and use
the first that authenticates. Runs on the S3 proxy box; the instance role's two
scoped grants (see ``S3Proxy._refresh``) are all it needs.
"""
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

import boto3

_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,}$")
_RUNNER_ROLE_TAG = "job_runner"
# Max concurrent sessions on this jump host (from the ttyd unit, set from
# S3Proxy.max_debug_sessions). 0/unset => no cap. Enforced here (not via ttyd -m)
# so an over-limit viewer gets a readable banner, not ttyd's bare reconnect
# button. Every live session's ssh argv carries the temp key dir, so we count it.
_MAX_SESSIONS = int(os.environ.get("PRAKTIKA_DEBUG_MAX_SESSIONS") or 0)
_KEYDIR_PREFIX = "praktika-debug-"
# Login user by pool AMI; first whose pushed key authenticates wins.
_CANDIDATE_USERS = ("ec2-user", "ubuntu")
# No host-key pinning: ephemeral VPC-internal runners recycle IPs (trust is the
# EIC + tag-scoping, not host keys), so skip known_hosts.
_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
]
# Open in the job checkout (/opt/praktika/work/repo; Settings.RUN_LOG lives at
# ci/tmp/job.log there) and auto-tail the live log. The tail is the rcfile
# startup command of an INTERACTIVE shell, so Ctrl-C stops the tail and returns
# to a prompt (a non-interactive "tail; exec bash" would be killed whole by
# SIGINT). rcfile also sources ~/.bashrc. Falls back to $HOME. NB: bash wants
# --rcfile before -i.
_REMOTE_CMD = (
    "cd /opt/praktika/work/repo 2>/dev/null || cd ~; "
    "exec bash --rcfile <("
    "cat ~/.bashrc 2>/dev/null; "
    "printf '%s\\n' "
    "'echo \"--- praktika-debug: tailing ci/tmp/job.log "
    "(Ctrl-C stops the tail, keeps the shell) ---\"' "
    "'tail -F ci/tmp/job.log 2>/dev/null'"
    ") -i"
)


def _die(msg):
    sys.stderr.write(f"praktika-debug: {msg}\n")
    sys.stderr.flush()
    sys.exit(1)


def _active_sessions():
    """Count live sessions via pgrep over the temp-key-dir prefix in each
    session's ssh argv. This process has no key dir yet, so it isn't counted."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", _KEYDIR_PREFIX],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:  # noqa: BLE001
        return 0


def _enforce_cap():
    """At the concurrent-session cap, print a visible banner and exit."""
    if _MAX_SESSIONS <= 0:
        return
    active = _active_sessions()
    if active >= _MAX_SESSIONS:
        bar = "=" * 56
        sys.stdout.write(
            f"\r\n{bar}\r\n"
            f"  Too many live debug sessions on this jump host\r\n"
            f"  ({active}/{_MAX_SESSIONS} in use). Close one and reconnect.\r\n"
            f"{bar}\r\n"
        )
        sys.stdout.flush()
        sys.exit(0)


def _imds(method, path, headers):
    req = urllib.request.Request(
        f"http://169.254.169.254{path}", method=method, headers=headers
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.read().decode().strip()


def _resolve_region():
    """AWS region: env (set by the ttyd unit) else the proxy's own region from
    IMDSv2 (a direct ssh session has no env). Keeps both paths working."""
    for var in ("AWS_DEFAULT_REGION", "AWS_REGION"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        token = _imds(
            "PUT",
            "/latest/api/token",
            {"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        return _imds(
            "GET",
            "/latest/meta-data/placement/region",
            {"X-aws-ec2-metadata-token": token},
        )
    except Exception:  # noqa: BLE001
        return None


def _describe_runner(ec2, instance_id):
    """Return ``(private_ip, az)`` for a running job runner, or exit refusing."""
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
    except Exception as e:  # noqa: BLE001
        _die(f"cannot describe {instance_id}: {e}")
    instances = [
        i for r in resp.get("Reservations", []) for i in r.get("Instances", [])
    ]
    if not instances:
        _die(f"instance {instance_id} not found")
    inst = instances[0]
    state = inst.get("State", {}).get("Name")
    if state != "running":
        _die(f"instance {instance_id} is {state!r}, not running")
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
    if tags.get("praktika_role") != _RUNNER_ROLE_TAG:
        _die(
            f"instance {instance_id} is not a job runner "
            f"(praktika_role={tags.get('praktika_role')!r})"
        )
    private_ip = inst.get("PrivateIpAddress")
    az = inst.get("Placement", {}).get("AvailabilityZone")
    if not private_ip or not az:
        _die(f"instance {instance_id} has no private IP / AZ yet")
    return private_ip, az


def main():
    if len(sys.argv) < 2:
        _die("usage: debug_connect.py <instance-id>")
    instance_id = sys.argv[1].strip()
    if not _INSTANCE_ID_RE.match(instance_id):
        _die(f"invalid instance id: {instance_id!r}")

    # Refuse with a visible message before touching AWS if we are at the cap.
    _enforce_cap()

    session = boto3.session.Session(region_name=_resolve_region())
    private_ip, az = _describe_runner(session.client("ec2"), instance_id)
    eic = session.client("ec2-instance-connect")

    tmp = tempfile.mkdtemp(prefix=_KEYDIR_PREFIX)
    key_path = os.path.join(tmp, "id")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", key_path],
        check=True,
    )
    with open(key_path + ".pub") as f:
        public_key = f.read().strip()

    def _push_key(user):
        eic.send_ssh_public_key(
            InstanceId=instance_id,
            InstanceOSUser=user,
            AvailabilityZone=az,
            SSHPublicKey=public_key,
        )

    # Find the login user whose pushed key authenticates. SendSSHPublicKey never
    # errors on a user that doesn't exist (it just won't log in), so probe with a
    # non-interactive ssh and keep the first that connects.
    chosen = None
    for user in _CANDIDATE_USERS:
        try:
            _push_key(user)
        except Exception as e:  # noqa: BLE001
            _die(f"send-ssh-public-key failed for {user}: {e}")
        probe = subprocess.run(
            ["ssh", "-i", key_path, "-o", "BatchMode=yes", *_SSH_OPTS,
             f"{user}@{private_ip}", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            chosen = user
            break
    if chosen is None:
        _die(
            f"could not authenticate to {instance_id} ({private_ip}) as any of "
            f"{', '.join(_CANDIDATE_USERS)}"
        )

    sys.stderr.write(
        f"praktika-debug: connecting to runner {instance_id} ({private_ip}) "
        f"as {chosen}\n"
    )
    sys.stderr.flush()

    # Refresh the ~60s Instance Connect key right before handing over, then exec
    # so ttyd's TTY is passed straight to the interactive session.
    try:
        _push_key(chosen)
    except Exception as e:  # noqa: BLE001
        _die(f"send-ssh-public-key failed for {chosen}: {e}")
    # -tt forces a remote PTY even though a command is given, so bash stays
    # interactive after the tail is interrupted. ttyd already handed us a PTY.
    os.execvp(
        "ssh",
        ["ssh", "-tt", "-i", key_path, *_SSH_OPTS,
         f"{chosen}@{private_ip}", _REMOTE_CMD],
    )


if __name__ == "__main__":
    main()
