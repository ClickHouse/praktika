import dataclasses
from typing import Optional, List, Dict, Iterable


@dataclasses.dataclass
class _Settings:
    ######################################
    ###  Pipeline generation settings  ###
    ######################################
    MAIN_BRANCH_NAME: str = "main"
    WORKFLOW_PATH_PREFIX: str = "./.github/workflows"
    WORKFLOWS_DIRECTORY: str = "./ci/workflows"
    SETTINGS_DIRECTORY: str = "./ci/settings"
    CI_CONFIG_JOB_NAME = "WorkflowConfig"
    CI_CONFIG_RUNS_ON: Optional[List[str]] = None
    VALIDATE_FILE_PATHS_IN_RUN_COMMAND: bool = True

    ######################################
    ### Runtime Settings               ###
    ######################################
    MAX_RETRIES_S3 = 3
    MAX_RETRIES_GH = 3

    ######################################
    ### S3 (artifact storage) settings ###
    ######################################
    S3_ARTIFACT_PATH: str = ""

    ######################################
    ###      CI workspace settings     ###
    ######################################
    TEMP_DIR: str = "/tmp/tmp_ci"
    OUTPUT_DIR: str = f"{TEMP_DIR}/output"
    INPUT_DIR: str = f"{TEMP_DIR}/input"
    RESULTS_DIR: str = f"{TEMP_DIR}/results"
    PYTHON_INTERPRETER: str = "python3"
    PYTHON_VERSION: str = "3.9"
    WORKFLOW_CONFIG_FILE: str = f"{TEMP_DIR}/workflow_config.json"
    ENVIRONMENT_VAR_FILE: str = f"{TEMP_DIR}/environment.json"

    ######################################
    ###      CI Cache settings         ###
    ######################################
    CACHE_VERSION: int = 1
    CACHE_DIGEST_LEN: int = 20
    CACHE_S3_PATH: str = ""
    CACHE_LOCAL_PATH: str = f"{TEMP_DIR}/ci_cache"

    ######################################
    ###      HTML Report settings      ###
    ######################################
    HTML_S3_PATH: str = ""
    TEXT_CONTENT_EXTENSIONS: Iterable[str] = frozenset([".txt", ".log"])
    S3_BUCKET_TO_HTTP_ENDPOINT: Optional[Dict[str, str]] = None


_USER_DEFINED_SETTINGS = [
    "S3_ARTIFACT_PATH",
    "MAIN_BRANCH_NAME",
    "WORKFLOWS_DIRECTORY",
    "DEFAULT_RUNNER_SCALING_TYPE",
    "MAX_WAIT_TIME_BEFORE_SCALE_DOWN_SEC",
    "TEMP_DIR",
    "OUTPUT_DIR",
    "INPUT_DIR",
    "CI_CONFIG_RUNS_ON",
    "CI_CONFIG_JOB_NAME",
    "PYTHON_INTERPRETER",
    "PYTHON_VERSION",
    "WORKFLOW_CONFIG_FILE",
    "CACHE_S3_PATH",
    "HTML_S3_PATH",
    "MAX_RETRIES_S3",
    "MAX_RETRIES_GH",
    "S3_BUCKET_TO_HTTP_ENDPOINT",
    "VALIDATE_FILE_PATHS_IN_RUN_COMMAND",
    "TEXT_CONTENT_EXTENSIONS",
]


class GHRunners:
    ubuntu = "ubuntu-latest"


if __name__ == "__main__":
    print(dataclasses.asdict(_Settings()))
