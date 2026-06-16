Bump the `praktika` wheel version across all files that reference it.

The new version must be passed as the argument to this command (e.g. `/bump-praktika 0.1.3`).

## Steps

1. Read the current version from `pyproject.toml` (`version = "X.Y.Z"`).

2. Update `version` in `pyproject.toml`:
   ```
   version = "NEW_VERSION"
   ```

3. Update `_PRAKTIKA_WHL` in `ci/infrastructure/projects.py`:
   ```python
   _PRAKTIKA_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-NEW_VERSION-py3-none-any.whl"
   ```
   Do **not** touch `_PRAKTIKA_BASE_WHL` or `_PRAKTIKA_CONTROLLER_WHL`.

4. Update the version assertion in `ci/tests/test_version.py`:
   ```python
   assert current_praktika_version() == "NEW_VERSION"
   ```

5. Update the `praktika-OLD_VERSION-py3-none-any.whl` string in `ci/tests/test_infra_projects.py`.
   Replace all occurrences of `praktika-OLD_VERSION-py3-none-any.whl` with `praktika-NEW_VERSION-py3-none-any.whl`.
   Be careful **not** to touch lines that reference `praktika_controller-` (underscore, controller wheel) or `praktika-0.0.`  (the frozen base wheel).

6. Run the tests to confirm everything passes:
   ```bash
   python -m pytest ci/tests/test_version.py ci/tests/test_infra_projects.py -x -q
   ```

## Notes

- `_PRAKTIKA_BASE_WHL` (`praktika-0.0.1`) is intentionally frozen — never touch it.
- `_PRAKTIKA_CONTROLLER_WHL` has its own separate versioning — never touch it here.
