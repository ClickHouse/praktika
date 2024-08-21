class RunnerLabels:
    SMALL = "maxs-small"
    SMALL_FIXED = "maxs-small-fixed"


S3_ARTIFACT_PATH = "clickhouse-builds/artifacts"
CACHE_CONFIG_RUNS_ON = [RunnerLabels.SMALL_FIXED]
CACHE_S3_PATH = "clickhouse-builds/ci_cache"
