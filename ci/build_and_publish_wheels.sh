#!/usr/bin/env bash
# Build and upload the Praktika wheels to the S3 keys used by runners,
# orchestrators, and AMI builds.
#
# Usage: build_and_publish_wheels.sh [--controller-only | --praktika-only]
#   (default: publish both wheels)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_PYTHON="${BUILD_PYTHON:-python3.12}"
BUILD_VENV="${BUILD_VENV:-${ROOT_DIR}/.build-venv}"
AWS_PROFILE="${AWS_PROFILE:-Box}"
S3_PACKAGES_URI="${S3_PACKAGES_URI:-s3://praktika-artifacts-eu-north-1/packages}"

PUBLISH_PRAKTIKA=1
PUBLISH_CONTROLLER=1
for arg in "$@"; do
  case "${arg}" in
    --controller-only) PUBLISH_PRAKTIKA=0 ;;
    --praktika-only) PUBLISH_CONTROLLER=0 ;;
    -h|--help)
      sed -n '2,6p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "Unknown argument: ${arg}" >&2
      echo "Usage: $(basename "${BASH_SOURCE[0]}") [--controller-only | --praktika-only]" >&2
      exit 2
      ;;
  esac
done

cd "${ROOT_DIR}"

if [[ ! -x "${BUILD_VENV}/bin/python" ]]; then
  "${BUILD_PYTHON}" -m venv "${BUILD_VENV}"
fi

"${BUILD_VENV}/bin/python" -m pip install setuptools wheel build

if [[ "${PUBLISH_PRAKTIKA}" == "1" ]]; then
  PRAKTIKA_VERSION="$(
    "${BUILD_VENV}/bin/python" -c 'from praktika.version import current_praktika_version; print(current_praktika_version())'
  )"
  PRAKTIKA_WHEEL="praktika-${PRAKTIKA_VERSION}-py3-none-any.whl"

  "${BUILD_VENV}/bin/python" -m build --wheel --no-isolation --outdir dist/
  aws --profile "${AWS_PROFILE}" s3 cp \
    "dist/${PRAKTIKA_WHEEL}" \
    "${S3_PACKAGES_URI}/${PRAKTIKA_WHEEL}"

  # Also mirror to the fixed, version-less latest alias. The 0.0.0 in the key is a
  # placeholder; pip reads the real version from the wheel's dist-info metadata.
  aws --profile "${AWS_PROFILE}" s3 cp \
    "dist/${PRAKTIKA_WHEEL}" \
    "${S3_PACKAGES_URI}/latest/praktika-0.0.0-py3-none-any.whl"
fi

if [[ "${PUBLISH_CONTROLLER}" != "1" ]]; then
  exit 0
fi

CONTROLLER_VERSION="$(
  "${BUILD_VENV}/bin/python" -c 'from pathlib import Path; from praktika.version import current_praktika_controller_version; print(current_praktika_controller_version(Path("bootstrap/pyproject.toml")))'
)"
CONTROLLER_WHEEL="praktika_controller-${CONTROLLER_VERSION}-py3-none-any.whl"

"${BUILD_VENV}/bin/python" -m build --wheel --no-isolation --outdir bootstrap/dist bootstrap
aws --profile "${AWS_PROFILE}" s3 cp \
  "bootstrap/dist/${CONTROLLER_WHEEL}" \
  "${S3_PACKAGES_URI}/${CONTROLLER_WHEEL}"

# Also mirror to the fixed, version-less latest alias. The 0.0.0 in the key is a
# placeholder; pip reads the real version from the wheel's dist-info metadata.
aws --profile "${AWS_PROFILE}" s3 cp \
  "bootstrap/dist/${CONTROLLER_WHEEL}" \
  "${S3_PACKAGES_URI}/latest/praktika_controller-0.0.0-py3-none-any.whl"
