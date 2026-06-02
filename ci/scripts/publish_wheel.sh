#!/bin/bash
# Build the praktika wheel and overwrite the fixed S3 key the
# orchestrator + runner pools install from. Driven by the
# "Publish wheel" job in ci/workflows/praktika_push.py on push to main.
set -euo pipefail

python3 -m build --wheel --outdir dist/
aws s3 cp dist/praktika-0.1-py3-none-any.whl \
  s3://praktika-artifacts-eu-north-1/packages/praktika-0.1-py3-none-any.whl
