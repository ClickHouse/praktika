#!/bin/bash
# Build the praktika_controller wheel and upload it to the versioned S3 key used
# by the images and user-data bootstrap paths.
#
# With --versioned-only, upload ONLY the versioned key and skip the fixed
# latest/ + <major.minor>/ aliases. PR runs use this so a not-yet-merged wheel
# can never repoint the fleet's install source (ci/workflows/praktika_pr_advanced.py).
set -euo pipefail

VERSIONED_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --versioned-only) VERSIONED_ONLY=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

VERSION="$(
  python3 -c "from pathlib import Path; from praktika.version import current_praktika_controller_version; print(current_praktika_controller_version(Path('bootstrap/pyproject.toml')))"
)"

python3 -m build --wheel --outdir bootstrap/dist/ ./bootstrap
aws s3 cp "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika_controller-${VERSION}-py3-none-any.whl"

if [[ "${VERSIONED_ONLY}" == "1" ]]; then
  echo "versioned-only: skipping latest/ and compat aliases"
  exit 0
fi

PRAKTIKA_COMPAT_VERSION="$(
  python3 -c "from praktika.version import compat_version, current_praktika_version; print(compat_version(current_praktika_version()))"
)"

# Also mirror to fixed, version-less aliases. The 0.0.0 in the key is a
# placeholder; pip reads the real version from the wheel's dist-info metadata.
aws s3 cp "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/latest/praktika_controller-0.0.0-py3-none-any.whl"
aws s3 cp "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/${PRAKTIKA_COMPAT_VERSION}/praktika_controller-0.0.0-py3-none-any.whl"
