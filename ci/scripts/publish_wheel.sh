#!/bin/bash
# Build the praktika wheel and upload it to the versioned S3 key the
# orchestrator + runner pools install from. Driven by the
# "Publish wheel" job in ci/workflows/praktika_push.py on push to main.
set -euo pipefail

VERSION="$(
  python3 -c "from praktika.version import current_praktika_version; print(current_praktika_version())"
)"

python3 -m build --wheel --outdir dist/
aws s3 cp "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika-${VERSION}-py3-none-any.whl"
