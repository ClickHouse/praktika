class RunnerLabels:
    SMALL_ARM = "arm-2xsmall"
    SMALL_ARM_BASE = "arm-2xsmall-base"
    SMALL_AMD = "amd-2xsmall"

CI_CONFIG_RUNS_ON = [RunnerLabels.SMALL_ARM]

AWS_REGION = "eu-north-1"
AWS_ACCOUNT_ID = "420943511422"
AWS_PROFILE = "Box"
#TODO: make it default
CLOUD_INFRASTRUCTURE_CONFIG_PATH = "./ci/infrastructure/projects.py"
S3_ARTIFACT_BUCKET = "praktika-artifacts-eu-north-1"
S3_REPORT_BUCKET = S3_ARTIFACT_BUCKET

CACHE_S3_PATH = "{S3_ARTIFACT_BUCKET}/ci_cache"

S3_BUCKET_TO_HTTP_ENDPOINT = {S3_ARTIFACT_BUCKET: f"{S3_ARTIFACT_BUCKET}.s3.amazonaws.com", S3_REPORT_BUCKET: f"{S3_REPORT_BUCKET}.s3.amazonaws.com"}

DOCKER_MERGE_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_ARM_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_AMD_RUNS_ON = [RunnerLabels.SMALL_AMD]

SECRET_DOCKER_REGISTRY = "praktika-docker-registry-connection"
USE_CUSTOM_GH_AUTH = True

# Install praktika from the cloned PR tree (packaging metadata lives at the repo root).
# If the selected base venv already has praktika installed, bootstrap uses it
# directly and ignores PRAKTIKA_INSTALL_SOURCE. Otherwise bootstrap clones that
# base env and installs praktika from the source below for this dispatch.
PRAKTIKA_INSTALL_SOURCE = "."
PRAKTIKA_BASE_VENV = "praktika-runtime"
GH_AUTH_LAMBDA_NAME = "praktika-gh-token"
GH_AUTH_LAMBDA_REGION = AWS_REGION

CI_DB_DB_NAME = "default"
CI_DB_TABLE_NAME = "checks"
# JSON connection blob auto-published by NativeComponents.CIDBCluster.deploy().
SECRET_CI_DB_CONNECTION = "praktika-cidb-connection"

DEFAULT_LOCAL_TEST_WORKFLOW = "Praktika CI Advanced"
