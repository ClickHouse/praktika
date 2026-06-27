#!/bin/bash
# Build the praktika wheel and upload it to the versioned S3 key the
# orchestrator + runner pools install from. Driven by the
# "Publish wheel" job in ci/workflows/praktika_push.py on push to main.
set -euo pipefail

VERSION="$(
  python3 -c "from praktika.version import current_praktika_version; print(current_praktika_version())"
)"
PRAKTIKA_COMPAT_VERSION="$(
  VERSION="${VERSION}" python3 -c 'import os; from praktika.version import compat_version; print(compat_version(os.environ["VERSION"]))'
)"

python3 -m build --wheel --outdir dist/
aws s3 cp "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika-${VERSION}-py3-none-any.whl"

# Also mirror to fixed, version-less aliases. The 0.0.0 in the key is a
# placeholder; pip reads the real version from the wheel's dist-info metadata.
aws s3 cp "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/latest/praktika-0.0.0-py3-none-any.whl"
aws s3 cp "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/${PRAKTIKA_COMPAT_VERSION}/praktika-0.0.0-py3-none-any.whl"
