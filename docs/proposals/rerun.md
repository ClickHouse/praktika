# Re-run of a push / branch check is silently dropped

**Status:** open — feature request.
**Area:** `infrastructure/native/lambda_gh_trigger.py` (`_handle_rerun`, `_handle_partial_rerun`, `_handle_full_rerun`), `orchestrator/__init__.py` (`_orchestrate_resume`).

## Problem

Clicking "Re-run" (single job or "re-run all") on a check that belongs to a
**push / branch run** (e.g. a `master` push) does nothing. The webhook reaches
the Lambda and is silently skipped; GitHub shows no error because the Lambda
returns HTTP 200 and only prints to CloudWatch.

Both re-run paths assume every re-run targets a pull request:

```python
# _handle_partial_rerun
prs = check_obj.get("pull_requests", [])
pr_number = prs[0].get("number") if prs else None
if not pr_number or not rerun_sha:
    print(f"SKIP: partial rerun run={run_id} - missing PR number or head sha")
    return
```

```python
# _handle_full_rerun
prs = check_obj.get("pull_requests", [])
if not prs:
    print(f"SKIP: {source}.rerequested - no associated PR")
    return
```

A push / branch check never carries a `pull_requests` entry, so `pr_number` is
always `None` and every such re-run is dropped before any work is scheduled. The
stale-guard that follows is also PR-only: it calls `_fetch_pr` and compares
against the PR head, with no branch-head equivalent. `_orchestrate_resume`
compounds this by forcing `event["type"] = "pull_request"` on every resume.

### Observed incident

Re-run requested on `Main / Test AMD (tsan)`
(`https://github.com/ClickHouse/amber/runs/103695235322`), a failed job of a
`master` push run:

- check `external_id`: `run_id=103695035456`, `job="Test AMD (tsan)"`
- `head_branch=master`, `head_sha=b50d34d...`, `pull_requests=[]`

Lambda log:

```
SKIP: partial rerun run=103695035456 - missing PR number or head sha
SKIP: partial rerun run=103695035456 - missing PR number or head sha
```

Nothing was enqueued; no job re-ran.

## Why the current code is this way

The re-run feature was designed and tested exclusively for pull requests: the PR
is the authoritative source of fork status (internal vs external routing) and of
the labels / title / draft the DAG is filtered on, and the stale-guard exists to
stop an old check from running current (possibly unapproved fork) code. Push
runs have none of these concerns but also fit none of the PR-shaped plumbing, so
they fall through the `pull_requests` guard. All re-run tests in
`ci/tests/test_lambda_gh_trigger.py` use `pull_requests: [{number: ...}]`; the
push case was never covered.

Two aggravating factors:

1. **Silent failure.** The Lambda returns 200 and logs only to CloudWatch, so
   the user sees no feedback - "nothing was triggered".
2. **Misleading message.** "missing PR number or head sha" is printed even when
   `head_sha` is present; only the PR association is (permanently) absent.

## Feature request

Support partial and full re-run for push / branch checks.

- **Route on branch, not PR.** In the partial path, when the check carries no
  `pull_requests`, resolve the branch from `head_branch` (check / check_suite)
  and take the push route instead of skipping.
- **Branch-head stale-guard.** Compare the check's `head_sha` against the current
  branch head (`GET /repos/{repo}/branches/{branch}`) rather than `_fetch_pr`;
  reject when the branch has advanced, mirroring the PR guard.
- **Push-flavored resume.** Build a `type="rerun"` message that carries
  `head_ref` (branch) and no `pr_number`; teach `_orchestrate_resume` to leave a
  push run classified as `push` instead of unconditionally forcing
  `pull_request`. The snapshot already holds the per-job environment, so the DAG
  reshaping the PR path needs is unnecessary here.
- **Full re-run.** Give `_handle_full_rerun` the same branch fallback so "re-run
  all" on a push check mints a fresh push run.
- **Visible feedback.** On any re-run the Lambda declines (stale, disallowed,
  unsupported), post a check-run message so the user sees why, instead of a
  silent 200.
- **Tests.** Cover a `master` push check re-run end to end - partial and full,
  fresh and stale branch head.
