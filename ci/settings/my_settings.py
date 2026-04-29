class RunnerLabels:
    SMALL = "arm-2xsmall"
    SMALL_FIXED = "arm-2xsmall"

CI_CONFIG_RUNS_ON = [RunnerLabels.SMALL_FIXED]
DOCKER_BUILD_RUNS_ON = [RunnerLabels.SMALL_FIXED]

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

DOCKER_MERGE_RUNS_ON = [RunnerLabels.SMALL_FIXED]
DOCKER_BUILD_ARM_RUNS_ON = [RunnerLabels.SMALL]
DOCKER_BUILD_AMD_RUNS_ON = [RunnerLabels.SMALL_FIXED]

DOCKERHUB_USERNAME = "robotclickhouse"
DOCKERHUB_SECRET = "dockerhub_robot_password"

CI_DB_DB_NAME = "default"
CI_DB_TABLE_NAME = "checks"
SECRET_CI_DB_URL = "CI_DB_URL"
SECRET_CI_DB_USER = "CI_DB_USER"
SECRET_CI_DB_PASSWORD = "CI_DB_PASSWORD"

