import dataclasses
import importlib.util
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclasses.dataclass
class _Settings:
    ######################################
    #    Pipeline generation settings    #
    ######################################
    MAIN_BRANCH = "main"
    CI_PATH = "./ci"
    WORKFLOW_PATH_PREFIX: str = "./.github/workflows"
    WORKFLOWS_DIRECTORY: str = f"{CI_PATH}/workflows"
    SETTINGS_DIRECTORY: str = "./ci/settings"
    CI_CONFIG_JOB_NAME = "Config Workflow"

    # Enables a single job (DOCKER_BUILD_MANIFEST_JOB_NAME) for building all platforms and merge
    ENABLE_MULTIPLATFORM_DOCKER_IN_ONE_JOB = False
    DOCKER_BUILD_ARM_LINUX_JOB_NAME = "Dockers Build (arm)"
    DOCKER_BUILD_AMD_LINUX_JOB_NAME = "Dockers Build (amd)"
    DOCKER_BUILD_AMD_LINUX_AND_MERGE_JOB_NAME = "Dockers Build and Merge (amd)"
    DOCKER_BUILD_MANIFEST_JOB_NAME = "Dockers Build (multiplatform manifest)"
    DOCKER_MERGE_RUNS_ON: Optional[List[str]] = None
    DOCKER_BUILD_ARM_RUNS_ON: Optional[List[str]] = None
    DOCKER_BUILD_AMD_RUNS_ON: Optional[List[str]] = None

    FINISH_WORKFLOW_JOB_NAME = "Finish Workflow"
    READY_FOR_MERGE_CUSTOM_STATUS_NAME = ""
    CI_CONFIG_RUNS_ON: Optional[List[str]] = None
    VALIDATE_FILE_PATHS: bool = True
    DISABLED_WORKFLOWS: Optional[List[str]] = None
    ENABLED_WORKFLOWS: Optional[List[str]] = None
    DEFAULT_LOCAL_TEST_WORKFLOW: str = ""

    ######################################
    #    Runtime Settings                #
    ######################################
    MAX_RETRIES_S3 = 3
    MAX_RETRIES_GH = 3

    ######################################
    #   S3 (artifact storage) settings   #
    ######################################
    S3_ARTIFACT_PATH: str = ""

    ######################################
    #        CI workspace settings       #
    ######################################
    TEMP_DIR: str = "./ci/tmp"
    # TODO: remove if using temp dir for in and out is ok
    OUTPUT_DIR: str = f"{TEMP_DIR}"
    INPUT_DIR: str = f"{TEMP_DIR}"
    PYTHON_INTERPRETER: str = "python3"
    PYTHON_PACKET_MANAGER: str = "pip3"
    ENVIRONMENT_VAR_FILE: str = f"{TEMP_DIR}/environment.json"
    RUN_LOG: str = f"{TEMP_DIR}/job.log"

    USE_CUSTOM_GH_AUTH: bool = False
    SECRET_GH_APP: str = "praktika-gh-app"
    GH_AUTH_LAMBDA_NAME: str = ""
    GH_AUTH_LAMBDA_REGION: str = ""

    ENV_SETUP_SCRIPT: str = f"{TEMP_DIR}/praktika_setup_env.sh"
    WORKFLOW_JOB_FILE: str = f"{TEMP_DIR}/workflow_job.json"
    WORKFLOW_STATUS_FILE: str = f"{TEMP_DIR}/workflow_status.json"
    WORKFLOW_INPUTS_FILE: str = f"{TEMP_DIR}/workflow_inputs.json"
    ARTIFACT_URLS_FILE: str = f"{TEMP_DIR}/artifact_urls.json"

    ######################################
    #        CI Cache settings           #
    ######################################
    # If enabled, Config Workflow creates a content-addressed .git/modules/ archive
    # in S3. Jobs with needs_submodules=True download it instead of cloning from GitHub.
    ENABLE_SUBMODULE_CACHE: bool = False

    CACHE_VERSION: int = 1
    CACHE_DIGEST_LEN: int = 20
    CACHE_S3_PATH: str = ""
    CACHE_LOCAL_PATH: str = f"{TEMP_DIR}/ci_cache"

    ######################################
    #        Report settings             #
    ######################################
    S3_REPORT_BUCKET: str = ""
    # Optional: upstream report bucket to merge issue catalogs from (e.g. "clickhouse-test-reports")
    S3_UPSTREAM_REPORT_BUCKET: str = ""
    HTML_PAGE_FILE: str = "./ci/praktika/json.html"
    S3_BUCKET_TO_HTTP_ENDPOINT: Optional[Dict[str, str]] = None
    TEXT_CONTENT_EXTENSIONS: Iterable[str] = frozenset([".txt", ".log"])
    # Compress if text file size exceeds this threshold (in MB, 0 - disable compression)
    COMPRESS_THRESHOLD_MB: int = 0

    SECRET_DOCKER_REGISTRY: str = ""

    ######################################
    #        CI DB Settings              #
    ######################################
    # SSM/secret name holding a JSON connection blob:
    #   {"url": "http://host:8123", "user": null, "password": null}
    # Auto-published by NativeComponents.CIDBCluster.deploy() for CIDB
    # instances praktika manages. Null/empty user+password means "send no
    # auth header" — runners rely on the server-side <no_password/> ACL
    # gated by VPC CIDR.
    SECRET_CI_DB_CONNECTION: str = ""
    CI_DB_DB_NAME = ""
    CI_DB_TABLE_NAME = ""
    KEEPER_STRESS_METRICS_DB_NAME = "keeper_stress_tests"
    KEEPER_STRESS_METRICS_TABLE_NAME = "keeper_metrics_ts"
    CI_DB_INSERT_TIMEOUT_SEC = 20
    CI_DB_QUERY_TIMEOUT_SEC = 60

    # to post links for reading statistics in html report (with read-only user)
    CI_DB_READ_USER: str = ""
    CI_DB_READ_URL: str = ""

    # Substrings to classify test failures. Used to generate helper queries for checking failure history.
    # Not required to cover all failures, but recommended to maximize coverage.
    # Choose values wisely to effectively differentiate between different failure types.
    TEST_FAILURE_PATTERNS: Optional[List[str]] = None

    ######################################
    #        Infrastructure Settings     #
    ######################################
    CLOUD_INFRASTRUCTURE_CONFIG_PATH: str = "./ci/infrastructure/projects.py"
    AWS_REGION: str = ""
    AWS_ACCOUNT_ID: str = ""
    AWS_PROFILE: str = ""
    # S3 path for Slack feed events storage (format: bucket/prefix)
    # Used by EventFeed and FeedSubscription for PR notification subscriptions
    EVENT_FEED_S3_PATH: str = ""
    # Where the workflow/job agents should install praktika from on every
    # dispatch. Three forms:
    #   ""             — no source override; if a side-specific base venv
    #                    is set, run whatever praktika is already installed
    #                    there. If all base/source settings are empty, the
    #                    bootstrapper falls back to its default praktika
    #                    wheel URL.
    #   "https://..."  — pip install <url>; pulls a wheel from that URL.
    #   "<rel/path>"   — pip install <clone_dir>/<rel/path>; resolves
    #                    relative to the cloned PR tree, so a PR's praktika
    #                    changes take effect on the very dispatch that
    #                    picked the PR up. If PRAKTIKA_BASE_VENV is also
    #                    set, the bootstrapper creates/reuses a derived env
    #                    from that prebaked base and installs praktika on top.
    PRAKTIKA_INSTALL_SOURCE: str = ""
    # Optional fallback base venv name used by both workflow and job sides
    # unless a side-specific value below is set.
    PRAKTIKA_BASE_VENV: str = ""
    # Optional prebaked base venv name for the workflow/orchestrator side.
    PRAKTIKA_WORKFLOW_BASE_VENV: str = ""
    # Optional prebaked base venv name for the job/runner side.
    PRAKTIKA_JOB_BASE_VENV: str = ""


_USER_DEFINED_SETTINGS = [
    "S3_ARTIFACT_PATH",
    "CACHE_S3_PATH",
    "S3_REPORT_BUCKET",
    "S3_UPSTREAM_REPORT_BUCKET",
    "CLOUD_INFRASTRUCTURE_CONFIG_PATH",
    "EVENT_FEED_S3_PATH",
    "AWS_REGION",
    "AWS_ACCOUNT_ID",
    "AWS_PROFILE",
    "S3_BUCKET_TO_HTTP_ENDPOINT",
    "TEXT_CONTENT_EXTENSIONS",
    "TEMP_DIR",
    "OUTPUT_DIR",
    "INPUT_DIR",
    "CI_CONFIG_RUNS_ON",
    "DOCKER_MERGE_RUNS_ON",
    "DOCKER_BUILD_ARM_RUNS_ON",
    "DOCKER_BUILD_AMD_RUNS_ON",
    "ENABLE_MULTIPLATFORM_DOCKER_IN_ONE_JOB",
    "CI_CONFIG_JOB_NAME",
    "PYTHON_INTERPRETER",
    "PYTHON_PACKET_MANAGER",
    "MAX_RETRIES_S3",
    "MAX_RETRIES_GH",
    "VALIDATE_FILE_PATHS",
    "SECRET_DOCKER_REGISTRY",
    "READY_FOR_MERGE_CUSTOM_STATUS_NAME",
    "SECRET_CI_DB_CONNECTION",
    "CI_DB_DB_NAME",
    "CI_DB_TABLE_NAME",
    "KEEPER_STRESS_METRICS_DB_NAME",
    "KEEPER_STRESS_METRICS_TABLE_NAME",
    "CI_DB_INSERT_TIMEOUT_SEC",
    "USE_CUSTOM_GH_AUTH",
    "GH_AUTH_LAMBDA_NAME",
    "GH_AUTH_LAMBDA_REGION",
    "MAIN_BRANCH",
    "DISABLED_WORKFLOWS",
    "ENABLED_WORKFLOWS",
    "DEFAULT_LOCAL_TEST_WORKFLOW",
    "COMPRESS_THRESHOLD_MB",
    "ENABLE_SUBMODULE_CACHE",
    "CI_DB_READ_USER",
    "CI_DB_READ_URL",
    "TEST_FAILURE_PATTERNS",
    "PRAKTIKA_INSTALL_SOURCE",
    "PRAKTIKA_BASE_VENV",
    "PRAKTIKA_WORKFLOW_BASE_VENV",
    "PRAKTIKA_JOB_BASE_VENV",
]


def _load_settings_module(path: Path, res: "_Settings") -> None:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    foo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(foo)
    for setting in _USER_DEFINED_SETTINGS:
        try:
            res.__setattr__(setting, getattr(foo, setting))
        except AttributeError:
            pass


def _get_settings() -> _Settings:
    res = _Settings()
    settings_dir = Path(_Settings.SETTINGS_DIRECTORY)

    # Primary settings file
    primary = settings_dir / "settings.py"
    if primary.is_file():
        _load_settings_module(primary, res)

    # Optional override files, applied in sorted order
    for override in sorted(settings_dir.glob("*_overrides.py")):
        _load_settings_module(override, res)

    return res


class GHRunners:
    ubuntu = "ubuntu-latest"


Settings = _get_settings()
