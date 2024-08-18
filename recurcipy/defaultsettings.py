import dataclasses

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
    TEMP_DIR: str = "~/tmp_ci"
    OUTPUT_DIR: str = f"{TEMP_DIR}/output"
    INPUT_DIR: str = f"{TEMP_DIR}/input"


_USER_DEFINED_SETTINGS = [
    "S3_ARTIFACT_PATH",
    "MAIN_BRANCH_NAME",
    "WORKFLOWS_DIRECTORY",
    "DEFAULT_RUNNER_SCALING_TYPE",
    "MAX_WAIT_TIME_BEFORE_SCALE_DOWN_SEC",
    "TEMP_DIR",
    "OUTPUT_DIR",
    "INPUT_DIR",
]


class GHRunners(metaclass=MetaClasses.WithIter):
    ubuntu = "ubuntu-latest"


if __name__ == "__main__":
    print(dataclasses.asdict(DefaultSettings()))
