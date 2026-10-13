# Push trigger: support glob patterns in `allowed_push_branches`

**Problem.** The webhook push-trigger allow-list only does exact string
matching. `ext["allowed_push_branches"]` is serialized into the
`ALLOWED_PUSH_BRANCHES` env var (`orchestrator_pool.py`), parsed back into a set
of literal branch names in `lambda_gh_trigger._parse_allowed_push_branches`
(only `.strip()`, no pattern compilation), and checked with plain set
membership:

```python
# lambda_gh_trigger.py:386
if branch not in ALLOWED_PUSH_BRANCHES:
    return None  # skip the push
```

So a configured pattern like `release/*` would only fire for a branch literally
named `release/*`, never for `release/26.12`.

**Why it matters.** Release branches are created every month
(`release/26.11`, `release/26.12`, ...). With exact matching, every new release
branch requires a config change + redeploy of the trigger lambda before pushes
to it run CI. We want the allow-list to accept a pattern that covers all present
and future release branches without redeploying.

**Status.** Open — exact match only; no glob support.

**Direction to consider.** Match each branch against the allow-list as a set of
shell-style glob patterns instead of literal names. Keep exact names working
(a name with no wildcard chars matches only itself under `fnmatch`), so this is
backward compatible:

```python
import fnmatch
if not any(fnmatch.fnmatch(branch, pattern) for pattern in ALLOWED_PUSH_BRANCHES):
    return None
```

Then a deployment can configure
`ext={"allowed_push_branches": ["master", "release/*"]}` and every monthly
`release/<version>` branch is covered automatically.

Caveats:
- `fnmatch` `*` also matches `/`, so `release/*` matches `release/a/b`; use a
  stricter matcher if nested refs must be excluded.
- Validation in `orchestrator_pool` (`_validate` of `allowed_push_branches`)
  stays as-is — it already only requires non-empty strings.
- The same list is used to gate push webhook dispatch only; no other consumer
  relies on exact membership.
