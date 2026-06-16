Build and upload both `praktika` and `praktika-controller` wheels to S3, then bump all image component versions so AMI rebuilds pick up the new wheels.

Wheel versions are **not** changed — existing S3 keys are overwritten in place.

## Steps

1. Ensure the build venv exists; create it if not:
   ```bash
   python3.12 -m venv .build-venv
   .build-venv/bin/python -m pip install setuptools wheel build
   ```

2. Build both wheels:
   ```bash
   .build-venv/bin/python -m build --wheel --no-isolation --outdir dist/
   .build-venv/bin/python -m build --wheel --no-isolation --outdir bootstrap/dist bootstrap
   ```

3. Upload both to S3 (overwrite existing keys). Read versions from `pyproject.toml` and `bootstrap/pyproject.toml` to construct filenames:
   ```bash
   aws --profile Box s3 cp \
     dist/praktika-0.1.1-py3-none-any.whl \
     s3://praktika-artifacts-eu-north-1/packages/praktika-0.1.1-py3-none-any.whl

   aws --profile Box s3 cp \
     bootstrap/dist/praktika_controller-0.1.1-py3-none-any.whl \
     s3://praktika-artifacts-eu-north-1/packages/praktika_controller-0.1.1-py3-none-any.whl
   ```

4. In `ci/infrastructure/projects.py`, increment the patch component of **only** `ci_version` inside `_image_builders()`:
   - `ci_version` — shared by the arm64 and x86_64 ci image recipes
   - Do **not** touch `base_ci_version`

## Notes

- `_PRAKTIKA_WHL` and `_PRAKTIKA_CONTROLLER_WHL` URLs in `projects.py` do not change.
- `_PRAKTIKA_BASE_WHL` is intentionally frozen; never touch it.
- Show the user both S3 cp commands before running them so they can confirm.
