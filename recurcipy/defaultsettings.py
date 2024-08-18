import dataclasses
from typing import Optional, List

from recurcipy.utils import MetaClasses


@dataclasses.dataclass
class DefaultSettings:
    ######################################
    ###  Pipeline generation settings  ###
    ######################################
    MAIN_BRANCH_NAME: str = "main"
    WORKFLOW_PATH_PREFIX: str = "./.github/workflows"
    WORKFLOWS_DIRECTORY: str = "./ci/workflows"
    SETTINGS_DIRECTORY: str = "./ci/settings"

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
    PYTHON_INTERPRETER: str = "python3"
    PYTHON_VERSION: str = "3.9"
    WORKFLOW_RUN_CONFIG_FILE: str = "/tmp/workflow_config.json"

    ######################################
    ###      CI Cache settings         ###
    ######################################
    CACHE_VERSION: int = 1
    CACHE_DIGEST_LEN: int = 20
    CACHE_CONFIG_RUNS_ON: Optional[List[str]] = None
    CACHE_CONFIG_JOB_NAME = "WorkflowConfig"
    CACHE_S3_PATH: str = ""
    CACHE_LOCAL_PATH: str = f"{TEMP_DIR}/ci_cache"


_USER_DEFINED_SETTINGS = [
    "S3_ARTIFACT_PATH",
    "MAIN_BRANCH_NAME",
    "WORKFLOWS_DIRECTORY",
    "DEFAULT_RUNNER_SCALING_TYPE",
    "MAX_WAIT_TIME_BEFORE_SCALE_DOWN_SEC",
    "TEMP_DIR",
    "OUTPUT_DIR",
    "INPUT_DIR",
    "CACHE_CONFIG_RUNS_ON",
    "CACHE_CONFIG_JOB_NAME",
    "PYTHON_INTERPRETER",
    "PYTHON_VERSION",
    "WORKFLOW_RUN_CONFIG_FILE",
    "CACHE_S3_PATH",
]


class GHRunners(metaclass=MetaClasses.WithIter):
    ubuntu = "ubuntu-latest"


if __name__ == "__main__":
    print(dataclasses.asdict(DefaultSettings()))
