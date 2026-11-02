"""
Build the praktika + praktika-controller distributions and upload them to PyPI.

Dispatch job for the "Build & Publish to PyPI" workflow (see
ci/workflows/ignition_dispatch.py). Reads an optional `sha` dispatch input: when
set, the worktree is checked out at that commit before building, so a release can
be cut from a specific sha of main rather than the dispatched ref's HEAD.

The PyPI token is read from the `praktika-pypi-token` AWS SSM parameter and passed
to twine as TWINE_PASSWORD (username is the literal `__token__`).
"""
from praktika import Secret
from praktika.info import Info
from praktika.result import Result
from praktika.utils import Utils

VENV = "./ci/tmp/pypi-build-venv"
PY = f"{VENV}/bin/python"

PYPI_TOKEN_PARAMETER = "praktika-pypi-token"

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
                f"{PY} -m pip install --upgrade pip build twine",
                # praktika (root project).
                f"{PY} -m build --outdir dist/",
                # praktika-controller (bootstrap project). --no-isolation lets
                # bootstrap/setup.py read the version from the root pyproject.
                f"{PY} -m build --no-isolation --outdir bootstrap/dist bootstrap",
            ],
            fail_fast=True,
        )
    )

    if all(r.is_ok() for r in results):
        token = Secret.Config(
            name=PYPI_TOKEN_PARAMETER,
            type=Secret.Type.AWS_SSM_PARAMETER,
        ).get_value()
        results.append(
            Result.from_commands_run(
                name="Upload to PyPI",
                command=[
                    f"{PY} -m twine check dist/* bootstrap/dist/*",
                    f"{PY} -m twine upload --non-interactive dist/* bootstrap/dist/*",
                ],
                env={
                    "TWINE_USERNAME": "__token__",
                    "TWINE_PASSWORD": token,
                },
                fail_fast=True,
            )
        )

    overall = (
        Result.Status.OK if all(r.is_ok() for r in results) else Result.Status.FAIL
    )
    Result.get().set_results(results=results).set_status(overall).complete_job()
