# CodeQL as a native Praktika job

**Goal.** Run GitHub CodeQL code-scanning *inside* the Praktika PR pipeline —
one more `Job.Config` in `ci/workflows/praktika_pr_advanced.py` — instead of a
separate GitHub Actions workflow (`.github/workflows/codeql.yaml`, default or
advanced setup). Alerts still land in the repo's **Security → Code scanning**
tab; the run just happens on our runners, under the same gating, caching, and
report machinery as every other job.

## Why this works

GitHub's `codeql-action` is a thin wrapper around the **`codeql` CLI** — a
self-contained binary shipped as the *CodeQL bundle* (CLI + precompiled query
packs). A Praktika job runs an arbitrary shell command, so it can drive the CLI
directly. Nothing in CodeQL is Actions-specific except the SARIF upload auth,
which the CLI also handles (`codeql github upload-results`).

The pipeline maps 1:1 onto what the Action does under the hood:

1. `codeql database create` — one DB per language.
2. `codeql database analyze` — run a query suite → SARIF.
3. `codeql github upload-results` — push SARIF to code scanning, attached to the
   PR's head commit / ref.

## The script — `ci/scripts/run_codeql.py`

A thin wrapper (same shape as `run_ruff.py` / `run_ci_pytests.py`) that shells
out to the CLI and emits a `Result` so findings surface in the Praktika report,
not only in the Security tab:

```python
# ci/scripts/run_codeql.py  (sketch)
#   - locate the pre-baked bundle (see "Bundle caching" below), else fetch it
#   - codeql database create db-go      --language=go      --build-mode=autobuild
#   - codeql database create db-actions --language=actions --build-mode=none
#   - codeql database analyze <db> --format=sarif-latest --output=<lang>.sarif <suite>
#   - codeql github upload-results --sarif=<lang>.sarif \
#         --repository=$GITHUB_REPOSITORY --ref=$GITHUB_REF --commit=$GITHUB_SHA \
#         --github-auth-stdin      # token from gh auth (enable_gh_auth)
#   - wrap the SARIF summary in a praktika Result (counts per severity)
```

## The job — into `praktika_pr_advanced.py`

```python
# CodeQL code-scanning, driven via the CodeQL CLI (not the GH Action).
# Runs on an x86-64 runner: CodeQL ships no native ARM-Linux bundle (see note).
# allow_failure so a scanning hiccup never blocks merge; enable_gh_auth so the
# job can upload SARIF to code scanning (needs security-events: write).
Job.Config(
    name="CodeQL",
    runs_on=[RunnerLabels.SMALL_AMD_UBUNTU],
    command="python3 ./ci/scripts/run_codeql.py",
    allow_failure=True,
    enable_gh_auth=True,
    digest_config=Job.CacheDigestConfig(
        include_paths=[
            "./ci/scripts/run_codeql.py",
            "./praktika",          # the Go/actions sources we actually scan
            "./bootstrap/src",
            "./go.mod",            # if/when there is Go to scan
        ],
    ),
),
```

The cache digest means CodeQL re-runs only when scanned sources change — a full
scan is expensive, and the digest cache (`enable_cache=True`, already on for this
workflow) skips it on no-op pushes.

## Decisions / things that differ from the Action

1. **Runner architecture.** CodeQL ships `linux64` (x86-64), `osx64`, `win64` —
   **no native ARM-Linux bundle.** The existing PR jobs use `SMALL_ARM`; this job
   must run on an x86-64 label (`SMALL_AMD_UBUNTU`). This is the single hard
   constraint.

2. **Upload token / permission.** `upload-results` needs a token with
   `security-events: write` on the repo. In Actions that's the implicit
   `GITHUB_TOKEN`; here it comes from `enable_gh_auth=True` (same mechanism the
   `Code Review` job uses to post comments) — **provided the GitHub App
   installation grants the code-scanning-alerts write scope.** If the App lacks
   it, add that permission to the installation; no new Praktika secret is needed.

3. **Ref / commit.** Alerts attach to the PR via `--ref`/`--commit`. Praktika
   already resolves the PR head sha and ref; pass `GITHUB_REF` / `GITHUB_SHA`
   (or the Praktika run metadata) through to the CLI so findings land on the
   right PR, not on `main`. With `ENABLE_PR_EPHEMERAL_MERGE_COMMIT` on, decide
   deliberately whether to scan the merge commit or the raw head — upload the
   **head** sha so the Security tab's PR annotations line up.

4. **Bundle caching.** The bundle is ~500 MB+. Fetching it from github.com every
   run is slow and flaky. **Bake it into the x86-64 runner AMI** (preferred — it
   is a build-time dependency, like the ruff venv in
   `ci/infrastructure/projects.py::_runtime_prebuilt_venvs`), or cache it in S3
   via the existing artifact plumbing. Pin a specific `codeql-bundle-vX.Y.Z`
   release; do not track latest.

5. **What we scan.** This repo is mostly Python (CodeQL supports it) plus the
   `actions` language for workflow YAML, and Go only if/when present. Start with
   `python` + `actions`; add `go` when there is Go to analyze. Query suite:
   `default` first, `security-extended` once the baseline is clean.

## Tradeoff vs. GitHub-managed setup

| | Default/advanced GH setup | This proposal |
|---|---|---|
| Alerts in Security tab | ✅ | ✅ |
| Runs under our gating / report / cache | ❌ (separate GH run) | ✅ |
| We own token, runner, bundle, upload | ❌ (GH handles) | ✅ (more work) |
| x86-64 runner required | n/a (GH-hosted) | ✅ (no ARM bundle) |

Pick this **only if** we specifically want CodeQL folded into the Praktika
pipeline — same reporting, cache, and merge-ready status as the rest of CI. If
all we want is the alerts and don't care where the scan runs, GitHub's default
setup (no YAML, config via repo Settings) is far less work and self-maintaining.

## Scope

- New: `ci/scripts/run_codeql.py`; one `Job.Config` in `praktika_pr_advanced.py`;
  bundle baked into the x86-64 runner AMI.
- Confirm the GitHub App installation has code-scanning write.
- No new Praktika secret, no new Settings knob, no YAML.
