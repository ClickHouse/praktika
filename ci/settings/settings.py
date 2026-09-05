PROJECT_SLUG = "praktika"

class RunnerLabels:
    SMALL_ARM = "arm-2xsmall"
    SMALL_ARM_BASE = "arm-2xsmall-base"
    # Dedicated pool whose instance role can call Bedrock (for the Code Review job).
    SMALL_ARM_BEDROCK = "arm-2xsmall-bedrock"
    SMALL_AMD = "amd-2xsmall"
    SMALL_AMD_UBUNTU = "amd-2xsmall-ubuntu"

CI_CONFIG_RUNS_ON = [RunnerLabels.SMALL_ARM]

# Attach each job's full controller log (clone/restore/dispatch/teardown) to the
# job result for debugging. See Settings.PRAKTIKA_DEBUG.
PRAKTIKA_DEBUG = True

# Sticky merge base: within this many hours of a PR's previous run, reuse the same
# pinned target-branch commit for the merge (keeps the digest cache warm across
# rapid iterations). 0 disables. Tune to taste — see Settings.STICKY_MERGE_BASE_HOURS.
STICKY_MERGE_BASE_HOURS = 6

AWS_REGION = "eu-north-1"
AWS_PROFILE = "Box"

S3_ARTIFACT_BUCKET = "praktika-artifacts-eu-north-1"
S3_REPORT_BUCKET = S3_ARTIFACT_BUCKET

CACHE_S3_PATH = f"{S3_ARTIFACT_BUCKET}/ci_cache"

S3_BUCKET_TO_HTTP_ENDPOINT = {S3_ARTIFACT_BUCKET: f"{S3_ARTIFACT_BUCKET}.s3.amazonaws.com", S3_REPORT_BUCKET: f"{S3_REPORT_BUCKET}.s3.amazonaws.com"}

DOCKER_MERGE_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_ARM_RUNS_ON = [RunnerLabels.SMALL_ARM]
DOCKER_BUILD_AMD_RUNS_ON = [RunnerLabels.SMALL_AMD]

SECRET_DOCKER_REGISTRY = "praktika-docker-registry-connection"

# Runner controller heartbeat write interval, in seconds.
HEARTBEAT_INTERVAL_S = 30
# Maximum time a dispatched job may stay QUEUED before a runner heartbeat.
RUNNER_PICKUP_TIMEOUT_S = 3600
# RUNNING liveness is two-stage. HEARTBEAT_STALL_S: flag the runner
# unresponsive and show a pending retry on the check, without failing it.
HEARTBEAT_STALL_S = 300
# HEARTBEAT_TIMEOUT_S: hard-fail deadline, held well above the runner queue
# visibility timeout so a job whose runner died mid-job is redelivered and
# re-heartbeats first; the gap budgets redelivery plus a cold ASG launch.
HEARTBEAT_TIMEOUT_S = 900

PRAKTIKA_BASE_VENV = "praktika-runtime"
GH_AUTH_LAMBDA_NAME = "praktika-gh-token"
GH_AUTH_LAMBDA_REGION = AWS_REGION

CI_DB_DB_NAME = "default"
CI_DB_TABLE_NAME = "checks"
# JSON connection blob auto-published by Components.CIDBCluster.deploy().
SECRET_CI_DB_CONNECTION = "praktika-cidb-connection"

DEFAULT_LOCAL_TEST_WORKFLOW = "Praktika CI Advanced"
