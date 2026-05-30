class RunnerLabels:
    SMALL_ARM = "arm-2xsmall"
    SMALL_AMD = "amd-2xsmall"

CI_CONFIG_RUNS_ON = [RunnerLabels.SMALL_ARM]

AWS_REGION = "eu-north-1"
AWS_ACCOUNT_ID = "420943511422"
AWS_PROFILE = "Box"
#TODO: make it default
CLOUD_INFRASTRUCTURE_CONFIG_PATH = "./ci/infra/cloud.py"
#TODO: rename variable to *_BUCKET
S3_ARTIFACT_PATH = "praktika-artifacts-eu-north-1"
S3_REPORT_BUCKET = S3_ARTIFACT_PATH

CACHE_S3_PATH = "{S3_ARTIFACT_PATH}/ci_cache"

S3_BUCKET_TO_HTTP_ENDPOINT = {S3_ARTIFACT_PATH: f"{S3_ARTIFACT_PATH}.s3.amazonaws.com", S3_REPORT_BUCKET: f"{S3_REPORT_BUCKET}.s3.amazonaws.com"}

DOCKER_MERGE_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_ARM_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_AMD_RUNS_ON = [RunnerLabels.SMALL_AMD]

SECRET_DOCKER_REGISTRY = "praktika-docker-registry-connection"
USE_CUSTOM_GH_AUTH = True

# Install praktika from the cloned PR tree (packaging metadata lives at the repo root).
# Per-PR praktika changes take effect on the dispatch that picked the PR up.
# The base venv differs by side: workflow gets a minimal env, jobs get pytest too.
PRAKTIKA_INSTALL_SOURCE = "."
PRAKTIKA_WORKFLOW_BASE_VENV = "praktika-orchestrator"
PRAKTIKA_JOB_BASE_VENV = "praktika-runner-pytest"
GH_AUTH_LAMBDA_NAME = "praktika-gh-token"
GH_AUTH_LAMBDA_REGION = AWS_REGION

CI_DB_DB_NAME = "default"
CI_DB_TABLE_NAME = "checks"
# JSON connection blob auto-published by NativeComponents.CIDBCluster.deploy().
SECRET_CI_DB_CONNECTION = "praktika-cidb-connection"
