# Tag-push workflows on the praktika engine

**Goal.** Let a workflow be triggered by a pushed git **tag** (e.g. `v26.11`),
so cutting a release runs CI/release jobs. Tag push is chosen over the GitHub
`release` event because it is git-platform-agnostic and is the lower-level
primitive: publishing a GitHub Release that creates a new tag also emits a
`push` webhook with `ref=refs/tags/<tag>`, so a tag trigger covers the
"new release created" case without binding to GitHub's `release` event shape.

Scope is the **native praktika engine only**. The GitHub Actions YAML generator
(`yaml_generator.py`) and the `GITHUB_EVENT_PATH` parser (`_environment.py`) are
not on the native path and are intentionally out of scope here.

## What already exists (do not reinvent)

- **Trigger lambda** `praktika/infrastructure/native/lambda_gh_trigger.py` —
  verifies the webhook and `send_message`s a JSON trigger into the
  `workflow-orchestrator` SQS. It does **no** workflow matching; it only parses
  the payload, applies a coarse allow-list, and enqueues. A tag push arrives as
  the **same** `X-GitHub-Event: push` as a branch push, differing only in
  `payload["ref"]` (`refs/tags/<tag>` vs `refs/heads/<branch>`) and carrying the
  tagged commit in `payload["after"]`.
- **`_build_push_workflow`** (`lambda_gh_trigger.py:490-509`) is the gate that
  rejects tags today:
  ```python
  ref = payload.get("ref", "")
  if not ref.startswith("refs/heads/"):
      return None            # a refs/tags/... push is dropped here
  ```
  It returns `{"type":"push","head_ref":<branch>,"head_sha":<after>,"repo",
  "sender","event_ts"}`.
- **Orchestrator** `praktika/orchestrator/__init__.py` —
  `find_workflows_for_event` (lines 51-125) maps the SQS `type` → `Workflow.Event`
  via `_EVENT_MAP` (line 15), reads the branch from `head_ref` for the
  `_HEAD_REF_EVENTS` set (line 27), skips `engine == GH_ACTIONS`, filters
  `wf.event != workflow_event` (line 103), then matches `wf.branches` for push
  via `_branch_matches` (exact string or `re.fullmatch`, lines 30-37). It already
  honors a `workflow_name=` filter.
- **Ref/sha plumbing is SHA-based.** The tag name rides the single `head_ref`
  field end-to-end (SQS → `state.py:1357` `BRANCH` env, snapshots at
  `state.py:1901/2647`), while checkout resolves by `head_sha` (`job_runner.py`,
  `pr_number=0` for non-PR events). For a tag push `head_sha` (from `after`)
  already points at the tagged commit, so checkout works unchanged.

So the whole "trigger → SQS → orchestrator → jobs" path exists. What is missing
is a distinct `tag` event type threaded through three layers.

## Design decision: a new `TAG` event, not reuse of `push`

A tag is modeled as its own `Workflow.Event.TAG` with a dedicated
`tags: List[str]` config field — mirroring how `merge_queue` is a distinct type.

Reusing `push` and matching tags against `wf.branches` was rejected: a workflow
with `branches=["v.*"]` would then fire on **both** a branch named `v…` and a
tag `v…`, conflating two namespaces. A separate field keeps them unambiguous,
and `tags` defaults to empty (= off), matching the gating-default-off convention.

## Changes (3 files)

### 1. `praktika/workflow.py`
- Add `TAG = "tag"` to `Workflow.Event` (lines 11-16).
- Add `tags: List[str] = field(default_factory=list)` to `Config`, next to
  `branches` (line 111); extend the docstring ("`tags` — tag names/patterns, for
  tag trigger only").
- Add an `is_event_tag()` predicate (lines 186-196).

### 2. `praktika/infrastructure/native/lambda_gh_trigger.py`
Relax `_build_push_workflow` (lines 490-509) to branch on the ref kind before the
`refs/heads/` check:
```python
ref = payload.get("ref", "")
head_sha = payload.get("after") or payload.get("head_commit", {}).get("id", "")
if ref.startswith("refs/tags/"):
    if not head_sha:
        return None
    return {
        "type": "tag",
        "event_ts": event_ts,
        "head_ref": ref[len("refs/tags/"):],   # tag name
        "head_sha": head_sha,
        "repo": payload.get("repository", {}).get("full_name", ""),
        "sender": payload.get("sender", {}).get("login", ""),
    }
# ...existing refs/heads/ branch path unchanged
```
The top-level `push` routing block (line 1334) needs **no** change — tags arrive
under the same `X-GitHub-Event: push`.

**Allow-list — decision:** do **not** add a parallel `ALLOWED_PUSH_TAGS` env knob.
The lambda accepts all `refs/tags/*` and lets `wf.tags` in the orchestrator be the
single source of truth for which tags run which workflow. Tag pushes are rare, so
the extra SQS messages for non-matching tags are negligible, and it avoids a
second place to configure the same intent. (An edge gate can be added later if a
deployment proves noisy.)

### 3. `praktika/orchestrator/__init__.py`
- `_EVENT_MAP["tag"] = Workflow.Event.TAG` (line 15).
- Add `"tag"` to `_HEAD_REF_EVENTS` (line 27) so the tag name is read from
  `head_ref`.
- Add a matching clause beside the push one (near line 108):
  ```python
  elif event_type == "tag" and wf.tags:
      if _branch_matches(tag, wf.tags):
          matched.append(wf)
  ```
  (reusing `_branch_matches` — exact or `re.fullmatch`, same as branches).

Everything downstream (`build_job_dag`, `WorkflowState`, SQS dispatch to
`praktika-<runs_on>`) is unchanged. The job sees the tag in the `BRANCH` env var
(= `head_ref`).

Example workflow:
```python
Workflow.Config(
    name="release",
    event=Workflow.Event.TAG,
    tags=["v.*"],
    jobs=[...],
)
```

## Build order

1. `workflow.py` — `Event.TAG`, `Config.tags`, `is_event_tag()`.
2. `orchestrator/__init__.py` — `_EVENT_MAP`, `_HEAD_REF_EVENTS`, match clause.
   Testable locally with a hand-written tag event
   (`praktika orchestrate workflow event.json --name release`).
3. `lambda_gh_trigger.py` — tag branch in `_build_push_workflow`.
4. Deploy the trigger lambda; push a test tag, confirm the SQS `{"type":"tag"}`
   message and the orchestrator run end-to-end before committing.

## Caveats

- **`BRANCH` env holds the tag name.** Jobs that assume `BRANCH` is a branch
  (e.g. to push back to it) must special-case tag runs. If a clearer accessor is
  wanted, expose `is_event_tag` / the tag via `Info`, but that is additive and
  not required for the trigger itself.
- **Annotated vs lightweight tags.** For an annotated tag, `payload["after"]` is
  still the target commit sha, so checkout is unaffected; the annotation object
  sha is not used.
- **Deploy-gated.** The lambda change is deployable infra — validate on real AWS
  (push a throwaway tag) before committing.

## Status

Design only — nothing implemented. New `TAG` event agreed; no new lambda
allow-list env knob (`wf.tags` is the single filter).
