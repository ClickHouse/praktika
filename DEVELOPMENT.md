# Development

Notes for working on praktika itself (the Python package), not for adopting it
to drive your own CI.

## Publish the praktika package to S3

Orchestrators and runners install praktika from S3 at boot and before each run
— so any change to the package needs to be built and re-uploaded before
instances pick it up. The bucket and key are fixed: instances fetch from this
exact URL, baked into the runner / orchestrator user-data scripts.

```bash
# Build
python3 -m pip install build --quiet
python3 -m build --wheel --outdir dist/

# Upload
aws s3 cp dist/praktika-0.1-py3-none-any.whl \
  s3://praktika-artifacts-eu-north-1/packages/praktika-0.1-py3-none-any.whl \
  --profile Box

# Optionally, refresh the local install from the same URL
pip install --force-reinstall \
  "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1-py3-none-any.whl" \
  --break-system-packages
```
