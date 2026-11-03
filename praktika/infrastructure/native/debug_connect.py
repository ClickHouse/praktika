#!/usr/bin/env python3
"""Per-session jump-host wrapper for the Praktika live-debug shell.

ttyd runs this once per browser connection with the target runner's EC2
instance id as argv[1] (passed through the ``/term/?arg=<id>`` URL). The arg is
attacker-controlled, so this script is the trust boundary between the tailnet
viewer and the runner. It:

  1. validates the instance id format,
  2. confirms via ``ec2:DescribeInstances`` that the target is a *running* job
     runner (tag ``praktika_role=job_runner``) — refusing the proxy itself, the
     CIDB, or any non-runner instance,
  3. mints an ephemeral (~60s) ed25519 key and pushes it to the runner with
     ``ec2-instance-connect:SendSSHPublicKey`` (no standing secrets land on the
     proxy or the runner),
  4. exec's ``ssh`` into the runner's private IP over the shared VPC, as the
     first login user that the pushed key actually authenticates.

The runner's default login user depends on the pool's AMI: AL2023 runners use
``ec2-user``, Ubuntu runners use ``ubuntu``. EC2 Instance Connect's
``SendSSHPublicKey`` accepts any user string and only *delivers* the key for
that user at login time, so we push + probe each candidate in turn and connect
as the one that works (rather than hardcoding one and failing on the other
pool type).

Runs on the S3 proxy box (Amazon Linux 2023); boto3 and openssh-clients are
installed by ``s3_proxy_user_data.sh``. The instance role grants exactly the two
scoped permissions used here (see ``S3Proxy._refresh``). The IAM
``SendSSHPublicKey`` grant is additionally tag-scoped to ``praktika_role=
job_runner``, so the DescribeInstances guard below is defense in depth.
"""
import os
import re
import subprocess
import sys
import tempfile

import boto3

_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,}$")
_RUNNER_ROLE_TAG = "job_runner"
# Login users to try, in order. AL2023 runners => ec2-user; Ubuntu runners =>
# ubuntu. The first whose pushed key authenticates is used.
_CANDIDATE_USERS = ("ec2-user", "ubuntu")
# Host-key pinning buys nothing here (ephemeral runners, VPC-internal, trust is
# the EIC + tag-scoping) and would break when a recycled private IP reappears
# with a new host key, so skip known_hosts entirely.
_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
]


def _die(msg):
    sys.stderr.write(f"praktika-debug: {msg}\n")
    sys.stderr.flush()
    sys.exit(1)


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

    session = boto3.session.Session()
    private_ip, az = _describe_runner(session.client("ec2"), instance_id)
    eic = session.client("ec2-instance-connect")

    tmp = tempfile.mkdtemp(prefix="praktika-debug-")
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
        "  tip: tail the live job log with  tail -F ci/tmp/job.log\n"
    )
    sys.stderr.flush()

    # Refresh the ~60s Instance Connect key right before handing over, then exec
    # so ttyd's TTY is passed straight to the interactive session.
    try:
        _push_key(chosen)
    except Exception as e:  # noqa: BLE001
        _die(f"send-ssh-public-key failed for {chosen}: {e}")
    os.execvp(
        "ssh",
        ["ssh", "-i", key_path, *_SSH_OPTS, f"{chosen}@{private_ip}"],
    )


if __name__ == "__main__":
    main()
