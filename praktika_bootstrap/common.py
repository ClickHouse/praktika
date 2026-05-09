from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_PRAKTIKA_SOURCE = os.environ.get(
    "PRAKTIKA_DEFAULT_INSTALL_SOURCE",
    (
        "https://praktika-artifacts-eu-north-1.s3.amazonaws.com"
        "/packages/praktika-0.1-py3-none-any.whl"
    ),
)


def configure_logging(name: str, instance_id: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{instance_id}] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger(name)


def get_github_token(region: str) -> str:
    """Mint a GitHub installation token for cloning and gh-auth."""
    import jwt
    import requests as _requests

    sm = __import__("boto3").client("secretsmanager", region_name=region)
    secret = json.loads(
        sm.get_secret_value(SecretId="praktika-gh-app")["SecretString"]
    )
    app_id = secret["app-id"]
    app_key = secret["app-key"]
    installation_id = secret["app-installation-id"]

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
    jwt_token = jwt.encode(payload, app_key, algorithm="RS256")

    resp = _requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


class VisibilityHeartbeat:
    """Extend SQS message visibility while we process it."""

    def __init__(
        self, sqs_client, queue_url, receipt_handle, visibility_timeout, interval=None
    ):
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._receipt = receipt_handle
        self._visibility = visibility_timeout
        self._interval = interval or max(30, visibility_timeout * 6 // 10)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sqs-heartbeat"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt,
                    VisibilityTimeout=self._visibility,
                )
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "change_message_visibility failed: %s: %s",
                    type(e).__name__,
                    e,
                )


class CancelWatchdog:
    """Kill a subprocess if the per-run S3 cancel flag appears."""

    def __init__(self, s3_client, bucket, key, proc, interval=10, log=None):
        self._s3 = s3_client
        self._bucket = bucket
        self._key = key
        self._proc = proc
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._log = log or logging.getLogger(__name__)

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="cancel-watchdog"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self._s3.head_object(Bucket=self._bucket, Key=self._key)
                self._log.info(
                    "Cancel flag found at s3://%s/%s - killing job",
                    self._bucket,
                    self._key,
                )
                self._proc.kill()
                return
            except Exception:
                pass


class Heartbeat:
    """Periodically write {ts, status} to the per-job S3 heartbeat key."""

    def __init__(self, s3_client, bucket, key, interval, status="running", log=None):
        self._s3 = s3_client
        self._bucket = bucket
        self._key = key
        self._interval = max(1, int(interval or 30))
        self._status = status
        self._stop = threading.Event()
        self._thread = None
        self._log = log or logging.getLogger(__name__)

    def start(self):
        self._beat()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="heartbeat"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _beat(self):
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=json.dumps({"ts": time.time(), "status": self._status}).encode(),
                ContentType="application/json",
            )
        except Exception as e:
            self._log.warning("heartbeat put failed: %s: %s", type(e).__name__, e)

    def _run(self):
        while not self._stop.wait(self._interval):
            self._beat()


def resolve_praktika_install_source(clone_dir: str | os.PathLike[str], log) -> str:
    """Read PRAKTIKA_INSTALL_SOURCE from the checked out repo or fall back."""
    settings_file = Path(clone_dir) / "ci" / "settings" / "settings.py"
    src = ""
    if settings_file.exists():
        try:
            spec = importlib.util.spec_from_file_location(
                "repo_settings", str(settings_file)
            )
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            src = getattr(mod, "PRAKTIKA_INSTALL_SOURCE", "") or ""
        except Exception as e:
            log.warning(
                "Could not read PRAKTIKA_INSTALL_SOURCE from %s: %s",
                settings_file,
                e,
            )

    if not src:
        src = DEFAULT_PRAKTIKA_SOURCE

    if src.startswith(("http://", "https://")):
        return src
    src_path = Path(src)
    if not src_path.is_absolute():
        src_path = Path(clone_dir) / src_path
    return str(src_path.resolve())


def git(args, cwd=None) -> str:
    result = subprocess.run(
        ["git", *(["-C", cwd] if cwd else []), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result.stdout


def clone_repo(repo, head_sha, pr_number, token, work_dir, branch=None, log=None):
    """Clone repo into a per-event work dir."""
    work_dir = str(work_dir)
    if pr_number:
        clone_dir = os.path.join(work_dir, f"pr-{pr_number}")
    else:
        slug = (branch or head_sha[:12] or "push").replace("/", "_")
        clone_dir = os.path.join(work_dir, f"push-{slug}")
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)
    os.makedirs(clone_dir, exist_ok=True)

    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    if log is not None:
        if pr_number:
            log.info("Cloning %s PR#%s at %s", repo, pr_number, head_sha[:12])
        else:
            log.info("Cloning %s branch=%s at %s", repo, branch, head_sha[:12])

    git(["init", clone_dir])
    git(["remote", "add", "origin", clone_url], cwd=clone_dir)
    if pr_number:
        git(
            [
                "fetch",
                "--depth=1",
                "origin",
                f"+refs/pull/{pr_number}/head:refs/heads/pr-head",
            ],
            cwd=clone_dir,
        )
        git(["checkout", "pr-head"], cwd=clone_dir)
    else:
        git(["fetch", "--depth=1", "origin", head_sha], cwd=clone_dir)
        git(["checkout", head_sha], cwd=clone_dir)

    actual_sha = git(["rev-parse", "HEAD"], cwd=clone_dir).strip()
    if log is not None:
        log.info("Checked out %s in %s", actual_sha[:12], clone_dir)
    return clone_dir, actual_sha


def upload_log(s3, bucket, prefix, instance_id, message, log):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    key = f"{prefix}/{now:%Y-%m-%d}/{instance_id}/{now:%H-%M-%S-%f}.json"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(message, indent=2),
        ContentType="application/json",
    )
    log.info("Log uploaded to s3://%s/%s", bucket, key)

