"""
Build the praktika + praktika-controller distributions and upload them to PyPI.

Dispatch job for the "Build & Publish to PyPI" workflow (see
ci/workflows/ignition_dispatch.py). Reads an optional `sha` dispatch input: when
set, the worktree is checked out at that commit before building, so a release can
be cut from a specific sha of main rather than the dispatched ref's HEAD.

The PyPI token is read from the `praktika-pypi-token` AWS SSM parameter and passed
to twine as TWINE_PASSWORD (username is the literal `__token__`).
"""
import os

from praktika import Secret
from praktika.info import Info
from praktika.result import Result
from praktika.utils import Utils

VENV = "./ci/tmp/pypi-build-venv"
PY = f"{VENV}/bin/python"

PYPI_TOKEN_PARAMETER = "praktika-pypi-token"

# The runner exports PYTHONPATH pointing at the baked base venv. Left in place it
# leaks into our fresh build venv: pip then treats twine's deps (e.g. requests) as
# already satisfied and skips installing them INTO the venv, and `env=`-scoped
# steps that drop PYTHONPATH later fail to import them. Strip it so the venv is
# self-contained for both the build and the twine upload.
BUILD_ENV = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

if __name__ == "__main__":
    sw = Utils.Stopwatch()
    results = []

    sha = Info.get_workflow_input_value("sha")
    if sha:
        results.append(
            Result.from_commands_run(
                name=f"Checkout {sha}",
                command=[
                    f"git fetch --depth=1 origin {sha}",
                    f"git checkout --force {sha}",
                ],
                fail_fast=True,
            )
        )

    results.append(
        Result.from_commands_run(
            name="Build distributions",
            command=[
                f"python3 -m venv {VENV}",
                f"{PY} -m pip install --upgrade pip setuptools wheel build twine",
                # praktika (root project): sdist + wheel.
                f"{PY} -m build --outdir dist/",
                # praktika-controller (bootstrap project): WHEEL ONLY, built
                # straight from the worktree. --no-isolation + --wheel keeps the
                # root pyproject one level up so bootstrap/setup.py can read the
                # version from it. A plain `build` would build the wheel from an
                # extracted sdist in a temp dir where the root pyproject is absent,
                # so setup.py's version lookup fails.
                f"{PY} -m build --wheel --no-isolation --outdir bootstrap/dist bootstrap",
            ],
            env=BUILD_ENV,
            fail_fast=True,
        )
    )

    if all(r.is_ok() for r in results):
        token = Secret.Config(
            name=PYPI_TOKEN_PARAMETER,
            type=Secret.Type.AWS_SSM_PARAMETER,
        ).get_value()
        twine_env = {
            **BUILD_ENV,
            "TWINE_USERNAME": "__token__",
            "TWINE_PASSWORD": token,
        }
        # Upload each package as its own step so they report independently: a
        # failure publishing one (e.g. a permissions 403 on praktika) does not
        # stop the other from being attempted and surfaced in the result tree.
        results.append(
            Result.from_commands_run(
                name="Upload praktika to PyPI",
                command=[
                    f"{PY} -m twine check dist/*",
                    f"{PY} -m twine upload --non-interactive dist/*",
                ],
                env=twine_env,
                fail_fast=True,
            )
        )
        results.append(
            Result.from_commands_run(
                name="Upload praktika-controller to PyPI",
                command=[
                    f"{PY} -m twine check bootstrap/dist/*",
                    f"{PY} -m twine upload --non-interactive bootstrap/dist/*",
                ],
                env=twine_env,
                fail_fast=True,
            )
        )

    overall = (
        Result.Status.OK if all(r.is_ok() for r in results) else Result.Status.FAIL
    )
    Result.get().set_results(results=results).set_status(overall).complete_job()
