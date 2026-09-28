#!/bin/bash
# Build the praktika wheel and upload it to the versioned S3 key the
# orchestrator + runner pools install from. Driven by the
# "Publish wheel" job in ci/workflows/praktika_push.py on push to main.
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
  python3 -c "from praktika.version import current_praktika_version; print(current_praktika_version())"
)"

python3 -m build --wheel --outdir dist/
aws s3 cp "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika-${VERSION}-py3-none-any.whl"

if [[ "${VERSIONED_ONLY}" == "1" ]]; then
  echo "versioned-only: skipping latest/ and compat aliases"
  exit 0
fi

PRAKTIKA_COMPAT_VERSION="$(
  VERSION="${VERSION}" python3 -c 'import os; from praktika.version import compat_version; print(compat_version(os.environ["VERSION"]))'
)"

# Also mirror to fixed, version-less aliases. The 0.0.0 in the key is a
# placeholder; pip reads the real version from the wheel's dist-info metadata.
aws s3 cp "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/latest/praktika-0.0.0-py3-none-any.whl"
aws s3 cp "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/${PRAKTIKA_COMPAT_VERSION}/praktika-0.0.0-py3-none-any.whl"
