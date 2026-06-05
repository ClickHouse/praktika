#!/bin/bash
# Build the praktika_bootstrap wheel and upload it to the fixed S3 key used
# by the images and user-data bootstrap paths.
set -euo pipefail

python3 -m build --wheel --outdir bootstrap/dist/ ./bootstrap
aws s3 cp bootstrap/dist/praktika_bootstrap-0.1.1-py3-none-any.whl \
  s3://praktika-artifacts-eu-north-1/packages/praktika_bootstrap-0.1.1-py3-none-any.whl
