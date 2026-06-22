#!/bin/bash
# Build the praktika_controller wheel and upload it to the versioned S3 key used
# by the images and user-data bootstrap paths.
set -euo pipefail

VERSION="$(
  python3 -c "from pathlib import Path; from praktika.version import current_praktika_controller_version; print(current_praktika_controller_version(Path('bootstrap/pyproject.toml')))"
)"

python3 -m build --wheel --outdir bootstrap/dist/ ./bootstrap
aws s3 cp "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika_controller-${VERSION}-py3-none-any.whl"
