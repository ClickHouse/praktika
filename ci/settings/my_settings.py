class RunnerLabels:
    SMALL = "maxs-small"
    SMALL_FIXED = "maxs-small-fixed"


S3_ARTIFACT_PATH = "clickhouse-builds/artifacts"
CI_CONFIG_RUNS_ON = [RunnerLabels.SMALL_FIXED]
DOCKER_BUILD_RUNS_ON = [RunnerLabels.SMALL_FIXED]
CACHE_S3_PATH = "clickhouse-builds/ci_cache"
HTML_S3_PATH = "clickhouse-builds/reports"
S3_REPORT_BUCKET = "reports"
S3_BUCKET_TO_HTTP_ENDPOINT = {"clickhouse-builds": "clickhouse-builds.s3.amazonaws.com", "reports": "reports.s3.amazonaws.com"}

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
