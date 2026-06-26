Build and publish both `praktika` and `praktika-controller` wheels to S3, then
(if needed) bump image component versions so AMI rebuilds pick up the new wheels.

Wheel versions are **not** changed here — existing S3 keys are overwritten in place.

## Build + publish

The canonical path is the script, which builds both wheels, uploads each to its
versioned key, and additionally mirrors both `praktika` and `praktika-controller`
to their fixed, version-less "latest" keys:

```bash
bash ./ci/scripts/build_and_publish_wheels.sh
```

It reads versions from `pyproject.toml` / `bootstrap/pyproject.toml`, so it
never needs editing on a bump. It uploads to (current versions shown):

- `s3://praktika-artifacts-eu-north-1/packages/praktika-<version>-py3-none-any.whl` (versioned)
- `s3://praktika-artifacts-eu-north-1/packages/latest/praktika-0.0.0-py3-none-any.whl` (fixed "latest" — what `_PRAKTIKA_WHL` points at; `0.0.0` is a placeholder, pip reads the real version from the wheel metadata)
- `s3://praktika-artifacts-eu-north-1/packages/praktika_controller-<version>-py3-none-any.whl` (versioned)
- `s3://praktika-artifacts-eu-north-1/packages/latest/praktika_controller-0.0.0-py3-none-any.whl` (fixed "latest" — what `_PRAKTIKA_CONTROLLER_WHL` points at; `0.0.0` is a placeholder, pip reads the real version from the wheel metadata)

This is outward-facing (overwrites prod S3). **Show the user the resolved `aws
s3 cp` destinations and confirm before running**, e.g. by printing the versions
first:

```bash
python3 -c "from praktika.version import current_praktika_version as v; print('praktika', v())"
python3 -c "from pathlib import Path; from praktika.version import current_praktika_controller_version as v; print('controller', v(Path('bootstrap/pyproject.toml')))"
```

## Image rebuilds (only if you changed baked-in wheels)

The latest `praktika` and `praktika_controller` wheels are force-reinstalled at
runtime from their fixed "latest" keys (controller on the host, praktika in the
runtime venv), so a new version of either reaches the non-base runner/orchestrator
pools **without** an AMI rebuild.

The base runtime venv (`praktika`) and the base `praktika_controller` wheel ARE
baked into the AMIs and are what the `-base` pools run. To get an updated
controller baked into the images, bump the patch of the relevant image version
in `_image_builders()` in `ci/infrastructure/projects.py`:

- `ci_version` — shared by `ci-arm64-image` + `ci-x86_64-image`
- `ubuntu_ci_version` — `ci-ubuntu-x86_64-image`

All three recipes bake `_PRAKTIKA_CONTROLLER_BASE_WHL`, so bump every version
whose AMI you need rebuilt (typically both).

## Notes

- `_PRAKTIKA_WHL` and `_PRAKTIKA_CONTROLLER_WHL` both point at the fixed "latest" keys and never change on a bump.
- The base runtime venv is pinned to `_PRAKTIKA_BASE_VERSION` (`0.1.4`) — intentionally frozen, never touch it.
- `_PRAKTIKA_CONTROLLER_BASE_WHL` (`0.1.1`) is the controller baked into the AMIs and run by the `-base` pools — bump via image version, not the "latest" key.
- Use `python3`, not `python` (no `python` on PATH in this environment).
