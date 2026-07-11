from __future__ import annotations

from collections.abc import Callable
import errno
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

FIRST_BOOT_RESERVED_CAPACITY_LOG_INTERVAL_S = 60 * 60
WORKDIR_CLEANUP_ATTEMPTS = 3
WORKDIR_CLEANUP_RETRY_DELAY_S = 2


class LogRateLimiter:
    def __init__(
        self,
        interval_s: int | float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._interval_s = max(0, interval_s)
        self._clock = clock
        self._last_log_at: float | None = None

    def should_log(self) -> bool:
        now = self._clock()
        if self._last_log_at is None or now - self._last_log_at >= self._interval_s:
            self._last_log_at = now
            return True
        return False


def resolve_praktika_base_venv(clone_dir: str | os.PathLike[str], log) -> str:
    """Read the shared Praktika base-venv selection from repo settings."""
    settings_file = Path(clone_dir) / "ci" / "settings" / "settings.py"
    base_venv = ""
    if settings_file.exists():
        try:
            spec = importlib.util.spec_from_file_location(
                "repo_settings", str(settings_file)
            )
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            base_venv = getattr(mod, "PRAKTIKA_BASE_VENV", "") or ""
        except Exception as e:
            log.warning(
                "Could not read Praktika runtime config from %s: %s",
                settings_file,
                e,
            )
    return base_venv


def configure_logging(name: str, instance_id: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{instance_id}] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger(name)


def get_github_token(region: str = "") -> str:
    """Mint a GitHub installation token by invoking the GH auth lambda."""
    import boto3

    region = (
        region
        or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        or os.environ.get("AWS_REGION", "").strip()
    )
    if not region:
        raise RuntimeError("AWS_DEFAULT_REGION or AWS_REGION must be set")

    lambda_name = os.environ.get("GH_AUTH_LAMBDA_NAME", "").strip() or (
        f"{os.environ.get('PRAKTIKA_PROJECT_SLUG', '').strip()}-gh-token"
        if os.environ.get("PRAKTIKA_PROJECT_SLUG", "").strip()
        else "gh-token"
    )
    client = boto3.client("lambda", region_name=region)
    response = client.invoke(
        FunctionName=lambda_name,
        InvocationType="RequestResponse",
        Payload=b"{}",
    )
    payload = response["Payload"].read().decode("utf-8")
    data = json.loads(payload)
    if "FunctionError" in response:
        raise RuntimeError(f"GH auth lambda [{lambda_name}] failed (payload redacted)")
    if isinstance(data, dict) and "statusCode" in data:
        if int(data.get("statusCode", 500)) >= 400:
            raise RuntimeError(
                f"GH auth lambda [{lambda_name}] returned statusCode={data.get('statusCode')} "
                "(body redacted)"
            )
        body = data.get("body", "{}")
        data = json.loads(body) if isinstance(body, str) else body
    token = data.get("token")
    if not token:
        raise RuntimeError(
            f"GH auth lambda [{lambda_name}] returned no token (payload redacted)"
        )
    return token


def imds_token() -> str:
    import requests

    resp = requests.put(
        "http://169.254.169.254/latest/api/token",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        timeout=2,
    )
    resp.raise_for_status()
    return resp.text


def instance_tag(tag_name: str, token: str | None = None) -> str:
    import requests

    token = token or imds_token()
    resp = requests.get(
        f"http://169.254.169.254/latest/meta-data/tags/instance/{tag_name}",
        headers={"X-aws-ec2-metadata-token": token},
        timeout=2,
    )
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return resp.text.strip()


def try_scale_in_if_idle(
    *,
    sqs,
    queue_url: str,
    queue_name: str,
    region: str,
    instance_id: str,
    has_received_message: bool = True,
    reserved_capacity_log_limiter: LogRateLimiter | None = None,
    log,
) -> bool:
    if not region or not instance_id:
        return False
    try:
        token = imds_token()
        if instance_tag("praktika_scaling", token=token) != "auto":
            return False
        asg_name = instance_tag("praktika_asg", token=token)
        if not asg_name:
            return False
        capacity_reserve = max(
            0,
            int(instance_tag("praktika_capacity_reserve", token=token) or "0"),
        )
        if capacity_reserve and not has_received_message:
            if (
                reserved_capacity_log_limiter is None
                or reserved_capacity_log_limiter.should_log()
            ):
                log.info(
                    "Queue %s is idle, preserving reserved instance %s until first job",
                    queue_name,
                    instance_id,
                )
            return False
        attrs = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        visible = int(attrs.get("ApproximateNumberOfMessages", "0"))
        in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0"))
        if visible != 0 or in_flight != 0:
            return False

        import boto3

        autoscaling = boto3.client("autoscaling", region_name=region)
        log.info(
            "Queue %s is idle, terminating %s and decrementing ASG %s",
            queue_name,
            instance_id,
            asg_name,
        )
        autoscaling.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=True,
        )
        subprocess.Popen(["/sbin/shutdown", "-h", "now"])
        return True
    except Exception:
        log.exception("Idle scale-in check failed")
        return False


def terminate_instance_for_replacement(
    *,
    region: str,
    instance_id: str,
    log,
    reason: str,
) -> None:
    """Terminate this runner without decrementing ASG desired capacity."""
    log.error("Terminating instance %s for replacement: %s", instance_id, reason)
    if region and instance_id and instance_id != "local-dev":
        try:
            import boto3

            autoscaling = boto3.client("autoscaling", region_name=region)
            autoscaling.terminate_instance_in_auto_scaling_group(
                InstanceId=instance_id,
                ShouldDecrementDesiredCapacity=False,
            )
            return
        except Exception:
            log.exception("Failed to request ASG replacement termination")

    subprocess.Popen(["/sbin/shutdown", "-h", "now"])


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def clean_work_root(
    work_dir: str | os.PathLike[str],
    log,
    *,
    attempts: int = WORKDIR_CLEANUP_ATTEMPTS,
    retry_delay_s: int | float = WORKDIR_CLEANUP_RETRY_DELAY_S,
) -> None:
    """Clean runner work root before accepting another task.

    The root itself is kept stable; every checkout/temp entry inside it is
    removed. ENOTEMPTY is retried because it usually means a just-finished
    child process was still creating files while rmtree walked the tree.
    """
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    for path in list(root.iterdir()):
        for attempt in range(1, max(1, attempts) + 1):
            try:
                _remove_path(path)
                break
            except OSError as e:
                is_last = attempt >= attempts
                if e.errno == errno.ENOENT:
                    break
                if e.errno == errno.ENOTEMPTY and not is_last:
                    log.warning(
                        "Workdir cleanup raced on %s (%s), retrying %s/%s",
                        path,
                        e,
                        attempt,
                        attempts,
                    )
                    time.sleep(retry_delay_s)
                    continue
                raise


def terminate_process_group(proc, log, *, grace_s: int | float = 10) -> None:
    """Terminate the process group rooted at ``proc`` if it still exists."""
    if proc is None or getattr(proc, "pid", None) is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        # The group leader may already be gone while background children still
        # exist in the session we created. start_new_session=True makes pid=pgid.
        pgid = proc.pid
    except Exception as e:
        log.warning("Could not resolve process group for %s: %s", proc.pid, e)
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception as e:
        log.warning("Could not SIGTERM process group %s: %s", pgid, e)
        return

    deadline = time.time() + max(0, grace_s)
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception as e:
        log.warning("Could not SIGKILL process group %s: %s", pgid, e)


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
                terminate_process_group(self._proc, self._log)
                return
            except Exception:
                pass


class Heartbeat:
    """Periodically write a per-job liveness payload to S3."""

    def __init__(
        self,
        s3_client,
        bucket,
        key,
        interval,
        status="running",
        fields=None,
        log=None,
    ):
        self._s3 = s3_client
        self._bucket = bucket
        self._key = key
        self._interval = max(1, int(interval or 30))
        self._status = status
        self._fields = dict(fields or {})
        self._stop = threading.Event()
        self._thread = None
        self._log = log or logging.getLogger(__name__)
        self._lock = threading.Lock()

    def start(self):
        self._beat()
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
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
            with self._lock:
                body = {"ts": time.time(), "status": self._status}
                body.update(self._fields)
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=json.dumps(body).encode(),
                ContentType="application/json",
            )
        except Exception as e:
            self._log.warning("heartbeat put failed: %s: %s", type(e).__name__, e)

    def _run(self):
        while not self._stop.wait(self._interval):
            self._beat()

    def update(self, *, status=None, **fields):
        with self._lock:
            if status is not None:
                self._status = status
            self._fields.update(fields)
        self._beat()


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


_GITHUB_API_BASE = "https://api.github.com"


def _github_api(method, url, token, body=None, timeout=15):
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def post_early_check(repo, head_sha, token, name, log=None, details_url=None):
    """Open an ``in_progress`` check run before the (slow) clone so the PR
    shows CI immediately and an interrupted clone still leaves a signal.

    Best-effort: returns the check-run id, or ``None`` on any failure (a fork
    head_sha not in the base repo, a transient API error, …). Posting the
    early check must never block the workflow.
    """
    body = {
        "name": name,
        "head_sha": head_sha,
        "status": "in_progress",
        "actions": [
            {
                "label": "Cancel",
                "description": "Cancel this CI run",
                "identifier": "cancel",
            }
        ],
    }
    if details_url:
        body["details_url"] = details_url
    try:
        data = _github_api(
            "POST", f"{_GITHUB_API_BASE}/repos/{repo}/check-runs", token, body
        )
        check_id = data.get("id")
        if log is not None:
            log.info("Opened early check run id=%s name=%r on %s", check_id, name, head_sha[:12])
        return check_id
    except Exception as e:
        if log is not None:
            log.warning("Could not open early check run (continuing): %s", e)
        return None


def finalize_check(repo, check_id, token, conclusion, title, summary, log=None):
    """Best-effort: mark an early check run completed. Used when the controller
    fails before the orchestrator subprocess takes over the check (e.g. the
    clone or runtime resolution fails), so the PR shows the failure instead of
    a check stuck ``in_progress``."""
    if not check_id:
        return
    body = {
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": title, "summary": summary},
    }
    try:
        _github_api(
            "PATCH",
            f"{_GITHUB_API_BASE}/repos/{repo}/check-runs/{check_id}",
            token,
            body,
        )
        if log is not None:
            log.info("Finalized early check run %s as %s", check_id, conclusion)
    except Exception as e:
        if log is not None:
            log.warning("Could not finalize early check run %s: %s", check_id, e)


def clone_repo(
    repo,
    head_sha,
    pr_number,
    token,
    work_dir,
    branch=None,
    log=None,
    clean_existing=True,
):
    """Clone repo into a per-event work dir."""
    work_dir = str(work_dir)
    if pr_number:
        clone_dir = os.path.join(work_dir, f"pr-{pr_number}")
    else:
        slug = (branch or head_sha[:12] or "push").replace("/", "_")
        clone_dir = os.path.join(work_dir, f"push-{slug}")
    if os.path.exists(clone_dir) and clean_existing:
        shutil.rmtree(clone_dir)
    elif os.path.exists(clone_dir) and any(Path(clone_dir).iterdir()):
        raise RuntimeError(
            f"Workdir {clone_dir} is not clean before clone; refusing in-task cleanup"
        )
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
