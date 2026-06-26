PROJECT_SLUG = "praktika"

class RunnerLabels:
    SMALL_ARM = "arm-2xsmall"
    SMALL_ARM_BASE = "arm-2xsmall-base"
    SMALL_AMD = "amd-2xsmall"
    SMALL_AMD_UBUNTU = "amd-2xsmall-ubuntu"

CI_CONFIG_RUNS_ON = [RunnerLabels.SMALL_ARM]

AWS_REGION = "eu-north-1"
AWS_ACCOUNT_ID = "420943511422"
AWS_PROFILE = "Box"

S3_ARTIFACT_BUCKET = "praktika-artifacts-eu-north-1"
S3_REPORT_BUCKET = S3_ARTIFACT_BUCKET

CACHE_S3_PATH = f"{S3_ARTIFACT_BUCKET}/ci_cache"

S3_BUCKET_TO_HTTP_ENDPOINT = {S3_ARTIFACT_BUCKET: f"{S3_ARTIFACT_BUCKET}.s3.amazonaws.com", S3_REPORT_BUCKET: f"{S3_REPORT_BUCKET}.s3.amazonaws.com"}

DOCKER_MERGE_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_ARM_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_AMD_RUNS_ON = [RunnerLabels.SMALL_AMD]

SECRET_DOCKER_REGISTRY = "praktika-docker-registry-connection"
USE_CUSTOM_GH_AUTH = True

# Runner controller heartbeat write interval, in seconds.
HEARTBEAT_INTERVAL_S = 30
# Maximum time a dispatched job may stay QUEUED before a runner heartbeat.
RUNNER_PICKUP_TIMEOUT_S = 3600
# Maximum time a RUNNING job may go without a fresh heartbeat.
HEARTBEAT_TIMEOUT_S = 300

# AI orchestration (skeleton). Enabled with the no-op mock provider so the
# advisor flow runs end-to-end; the mock makes no decisions and costs nothing.
AI_ORCHESTRATION_ENABLED = True
AI_PROVIDER = "mock"

PRAKTIKA_BASE_VENV = "praktika-runtime"
GH_AUTH_LAMBDA_NAME = "praktika-gh-token"
GH_AUTH_LAMBDA_REGION = AWS_REGION

CI_DB_DB_NAME = "default"
CI_DB_TABLE_NAME = "checks"
# JSON connection blob auto-published by Components.CIDBCluster.deploy().
SECRET_CI_DB_CONNECTION = "praktika-cidb-connection"

DEFAULT_LOCAL_TEST_WORKFLOW = "Praktika CI Advanced"
