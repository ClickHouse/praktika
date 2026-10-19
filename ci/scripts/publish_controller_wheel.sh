#!/bin/bash
# Build the praktika_controller wheel and upload it to the versioned S3 key used
# by the images and user-data bootstrap paths.
set -euo pipefail

# Controller version is kept in lockstep with praktika (single source of truth:
# `version` in the root pyproject.toml), so this resolves to that version.
VERSION="$(
  python3 -c "from praktika.version import current_praktika_controller_version; print(current_praktika_controller_version())"
)"
PRAKTIKA_COMPAT_VERSION="$(
  python3 -c "from praktika.version import compat_version, current_praktika_version; print(compat_version(current_praktika_version()))"
)"

python3 -m build --wheel --outdir bootstrap/dist/ ./bootstrap
aws s3 cp "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika_controller-${VERSION}-py3-none-any.whl"

# Also mirror to fixed, version-less aliases. The 0.0.0 in the key is a
# placeholder; pip reads the real version from the wheel's dist-info metadata.
aws s3 cp "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/latest/praktika_controller-0.0.0-py3-none-any.whl"
aws s3 cp "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/${PRAKTIKA_COMPAT_VERSION}/praktika_controller-0.0.0-py3-none-any.whl"
