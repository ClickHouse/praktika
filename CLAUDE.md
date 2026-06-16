# Praktika CI Agent Instructions

## Fetching CI results

Whenever the user asks to investigate, address, or fix CI failures — or refers to a PR without specifying what's broken — **always fetch the CI result JSON first** before reading code or proposing fixes.

### Determining PR, sha, and workflow name

**From a Praktika report URL** (e.g. `https://praktika-artifacts-eu-north-1.s3.amazonaws.com/json.html?PR=124&sha=abc123&name_0=Praktika%20CI`):
- Extract `PR`, `sha`, `name_0` from query params
- Normalize `name_0`: lowercase + spaces → underscores (e.g. `"Praktika CI"` → `"praktika_ci"`)

**From a bare PR number:**
- Run `gh pr view {PR} --json headRefOid --jq '.headRefOid'` to get the latest sha
- Use `praktika_ci_advanced` as the normalized workflow name

### Building the JSON URL

```
https://praktika-artifacts-eu-north-1.s3.amazonaws.com/PRs/{PR}/{sha}/{normalized}/result_{normalized}.json
```

Fetch this URL with WebFetch.

### Result structure

The JSON is a serialized `praktika.Result`:

```
{
  "name": str,
  "status": str,       # OK | FAIL | ERROR | SKIPPED | UNKNOWN | XFAIL | XPASS | PENDING | RUNNING | DROPPED
  "start_time": float?,
  "duration": float?,
  "info": str,
  "results": [...],    # nested Result objects, same shape, recursive
  "files": [...],
  "links": [...],
  "ext": {
    "labels": [{"name": str, "link": str?, "hint": str?}, ...],
    "warnings": [...],
    "errors": [...],
    "report_url": str?,
    ...
  }
}
```

Walk the `results` tree recursively to find all failing jobs and sub-jobs. Use the `info` field of failing nodes as the primary signal for what went wrong before touching any code.

## Troubleshooting runner instance failures

When a runner instance fails to start or pick up jobs, use the following diagnostic sequence. AWS profile is `Box`, region `eu-north-1`.

### Step 1 — Check for a CloudWatch log stream

```bash
aws logs get-log-events \
  --log-group-name "/{project}/praktika-controller" \
  --log-stream-name "{instance-id}" \
  --region eu-north-1 --profile Box --limit 200
```

**No stream exists** (`ResourceNotFoundException`) means the controller never started at all — the failure is in `user_data` (boot script), before the CloudWatch agent was configured. Skip to Step 3.

**Stream exists but empty or cut short** means the controller started then died — read the log for tracebacks.

### Step 2 — List recent streams to confirm

```bash
aws logs describe-log-streams \
  --log-group-name "/{project}/praktika-controller" \
  --region eu-north-1 --profile Box \
  --order-by LastEventTime --descending --limit 10
```

Confirms whether the instance ever logged anything vs. other instances that did.

### Step 3 — EC2 console output (user_data / cloud-init failures)

When there is no CloudWatch stream, EC2 console output is the only window into boot. It captures cloud-init stdout/stderr including `set -x` traces from `user_data`:

```bash
aws ec2 get-console-output \
  --instance-id {instance-id} \
  --region eu-north-1 --profile Box \
  --latest --output text | tail -100
```

Look for lines like:
- `cloud-init[...]: + <command>` — the `set -x` trace of what ran
- `ERROR: HTTP error 404` — bad URL in pip install
- `No such file or directory` — missing venv or script path
- `Failed to run module scripts_user` — cloud-init caught a non-zero exit from user_data

### Common root causes

| Symptom | Cause |
|---|---|
| pip 404 on `{project}-artifacts-...` URL | `_replace_recursive` mangled an external wheel URL — the project's storage name is a substring of the URL |
| `No such file or directory` on venv path | AMI was built with a different `PRAKTIKA_BASE_VENV` name than what user_data references |
| Controller starts but immediately exits | `praktika-controller` service unit misconfigured or wrong wheel installed |
| No console output at all | Instance terminated before cloud-init ran (spot interruption, capacity issue) |
