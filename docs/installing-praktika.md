# Installing Praktika in CI

CI instances run two separate Python packages, installed independently:

- **`praktika-controller`** — the always-on agent baked into the AMI's system
  Python. It polls SQS, clones/restores the repo, resolves which Praktika runtime
  to use, and shells out to `python -m praktika orchestrate …`.
- **`praktika`** — the CI engine the controller runs. This is what actually
  executes the workflow (orchestrator role) or a job (runner role), and is the
  package these install methods select.

There are three ways `praktika` reaches a running instance. Methods 1 and 2 are
about the version baked into the machine image; method 3 installs it at run time.

| # | Method | Where it's set | When to use |
|---|---|---|---|
| 1 | Baked AMI base venv (default) | `ImageBuilder.PrebuiltVenv` in `ci/infrastructure/projects.py` | Production. Reproducible, fast, pinned. |
| 2 | Rebuild the AMI with a new pin | bump the wheel version, redeploy the image | Ship a new released Praktika version to a project. |
| 3 | Runtime source override | pool `ext["runtime_source"]` → `praktika_runtime_source` tag | Praktika development inside the repo; per-branch diverged Praktika. |

## 1. Baked AMI base venv (default)

Each AMI ships a named base virtual environment with Praktika (and the shared
runtime deps) preinstalled. Nothing is installed at run time — the controller
just points at the baked venv, which is the fastest and most reproducible path.

```python
# ci/infrastructure/projects.py
ImageBuilder.PrebuiltVenv(
    name="praktika-runtime",
    packages=[..., f"praktika[infrastructure] @ {_PRAKTIKA_BASE_WHL}"],
)
```

- The base venv name is `Settings.PRAKTIKA_BASE_VENV` (default `praktika-runtime`).
  The controller reads it from the checked-out `ci/settings/settings.py`
  (`resolve_praktika_base_venv`) and uses that baked venv as-is.
- The wheel is pinned to an **exact** version (`_PRAKTIKA_BASE_VERSION`) so image
  builds are reproducible and a bump forces a fresh AMI.
- Both the orchestrator and job-runner pools use the same base venv.

## 2. Rebuild the AMI with a new pin

To move a project onto a newly released Praktika, bump `_PRAKTIKA_BASE_VERSION`
(the pinned wheel URL) and rebuild/redeploy the image. This is method 1 with a
new version — still baked, still reproducible, but it costs an image build. Use
it for promoting a released version, not for iterating.

## 3. Runtime source override

A pool can install Praktika **at run time** instead of using the baked version,
by setting `ext["runtime_source"]`:

```python
# ci/infrastructure/projects.py
RunnerPool(name="...", ext={"runtime_source": "."})
```

This is surfaced as the `praktika_runtime_source` instance tag. On **every task**,
the controller reinstalls Praktika from `<source>` into an overlay of the prebaked
base venv (`ensure_praktika_runtime` in
`bootstrap/src/praktika_controller/venv_manager.py`) and runs Praktika from that
overlay. The overlay (a copy of the base venv) is created once per instance; the
install then runs each task with `--force-reinstall` (so a checkout change takes
effect even when the version string is unchanged) and `--no-deps` (so the base
venv's baked dependencies are reused rather than re-fetched — adding a *new*
runtime dependency means rebaking the base venv). The base venv itself is never
mutated, so base pools stay pinned.

`<source>` is a filesystem path:

- a path **relative to the cloned repo** — resolved against the checkout, so
  `.` installs Praktika from the repo you are testing;
- or an absolute path on the instance.

Applies to both pool types: `RunnerPool.ext` for job runners,
`OrchestratorPool.ext` for the orchestrator.

### Purpose

This method exists for **developing Praktika when it lives in the repo under
test**:

- **Instant application of Praktika changes.** With `runtime_source="."`, a CI
  run uses the Praktika from that run's own checkout. A change to Praktika lands
  in CI immediately — no wheel to publish, no AMI to rebuild.
- **Diverged Praktika across branches.** Each branch carries its own Praktika
  state in its checkout, and each run installs whatever its checkout holds. Two
  branches with different Praktika code both get the right version automatically,
  which makes parallel Praktika development across branches practical.

Unlike the baked pin, a runtime source is deliberately **not** pinned: the pool
tracks whatever the source (the branch's `.`) currently holds, reinstalled fresh
each task.

### Limitation

Only the **`praktika` runtime** is swapped. The **`praktika-controller`** stays
whatever the AMI baked (it is installed into system Python at image-build time and
is not overridable at run time). So changes to controller-side behaviour — the
boot flow, clone/restore, SQS handling, runtime resolution itself — still require
publishing a new controller wheel and rebuilding the AMI. Runtime source override
covers engine (`praktika`) changes only.

## Resolution order

For a given instance the controller resolves the runtime as:

1. `praktika_runtime_source` tag set → install that source over a copy of the
   base venv (method 3).
2. otherwise → use the baked base venv named by `PRAKTIKA_BASE_VENV` as-is
   (methods 1/2). If that venv somehow lacks Praktika, the boot fails with a
   clear error rather than silently running nothing.

## Components

| Concern | Location |
|---|---|
| Baked base venv definition | `ci/infrastructure/projects.py` (`_runtime_prebuilt_venvs`) |
| Base venv name setting | `ci/settings/settings.py` (`PRAKTIKA_BASE_VENV`) |
| Image / venv baking | `praktika/infrastructure/image_builder.py` |
| Controller (system Python) install | `praktika/infrastructure/native/image_builder.py` |
| Runtime source config | `RunnerPool.ext` / `OrchestratorPool.ext` (`runner_pool.py`, `orchestrator_pool.py`) |
| Tag → runtime resolution | `bootstrap/src/praktika_controller/controller.py` (`_resolve_runtime_source`, `_resolve_runtime`) |
| Install / overlay machinery | `bootstrap/src/praktika_controller/venv_manager.py` |
