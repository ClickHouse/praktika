# Praktika code-review guidance

Project-specific guidance for the `praktika review` job. This is appended to the
fixed review protocol; keep it focused on what a generic reviewer would not know
about this repository.

## What this repo is
Praktika is a self-hosted CI system: pipelines and cloud infrastructure defined
in Python. The runtime library lives under `praktika/`; project CI config,
workflows, and tests live under `ci/`.

## What to prioritise
- **Correctness of CI control flow**: job status handling, result propagation,
  retries, caching, and anything that decides whether a PR can merge. A wrong
  status here silently breaks every consuming project.
- **AWS / GitHub side effects**: calls that create, delete, or mutate cloud
  resources, secrets, commit statuses, comments, or review threads. Flag missing
  idempotency, missing error handling, and destructive actions without guards.
- **Backward compatibility**: `Job.Config` / `Workflow.Config` fields, Settings
  names, and CLI arguments are a public API for consuming repos. Call out
  renames or removals that would break them.

## What to skip
- Formatting, import ordering, and lint nits — other jobs cover these.
- Speculative refactors unrelated to the diff.

## Style
- Prefer a small number of high-signal findings over many low-value ones.
- When you flag something, say concretely how it fails (inputs → wrong outcome).
