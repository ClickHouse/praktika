#!/usr/bin/env bash
# Build and upload both Praktika wheels to the S3 keys used by runners,
# orchestrators, and AMI builds.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_PYTHON="${BUILD_PYTHON:-python3.12}"
BUILD_VENV="${BUILD_VENV:-${ROOT_DIR}/.build-venv}"
AWS_PROFILE="${AWS_PROFILE:-Box}"
S3_PACKAGES_URI="${S3_PACKAGES_URI:-s3://praktika-artifacts-eu-north-1/packages}"

cd "${ROOT_DIR}"

if [[ ! -x "${BUILD_VENV}/bin/python" ]]; then
  "${BUILD_PYTHON}" -m venv "${BUILD_VENV}"
fi

"${BUILD_VENV}/bin/python" -m pip install setuptools wheel build

PRAKTIKA_VERSION="$(
  "${BUILD_VENV}/bin/python" -c 'from praktika.version import current_praktika_version; print(current_praktika_version())'
)"
PRAKTIKA_WHEEL="praktika-${PRAKTIKA_VERSION}-py3-none-any.whl"

"${BUILD_VENV}/bin/python" -m build --wheel --no-isolation --outdir dist/
aws --profile "${AWS_PROFILE}" s3 cp \
  "dist/${PRAKTIKA_WHEEL}" \
  "${S3_PACKAGES_URI}/${PRAKTIKA_WHEEL}"

CONTROLLER_VERSION="$(
  "${BUILD_VENV}/bin/python" -c 'from pathlib import Path; from praktika.version import current_praktika_controller_version; print(current_praktika_controller_version(Path("bootstrap/pyproject.toml")))'
)"
CONTROLLER_WHEEL="praktika_controller-${CONTROLLER_VERSION}-py3-none-any.whl"

"${BUILD_VENV}/bin/python" -m build --wheel --no-isolation --outdir bootstrap/dist bootstrap
aws --profile "${AWS_PROFILE}" s3 cp \
  "bootstrap/dist/${CONTROLLER_WHEEL}" \
  "${S3_PACKAGES_URI}/${CONTROLLER_WHEEL}"
