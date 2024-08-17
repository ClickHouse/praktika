from recurcipy.utils import MetaClasses


class Settings:
    ######################################
    ###  Pipeline generation settings  ###
    ######################################
    MAIN_BRANCH_NAME = "main"
    WORKFLOW_PATH_PREFIX = "./.github/workflows"
    CONFIG_DIRECTORY = "./ci/configs"
    EXAMPLES_DIRECTORY = "./recurcipy/examples"

    ######################################
    ### Execution environment settings ###
    ######################################
    GH_ACTIONS_DIRECTORY = "/home/ubuntu/gh_actions"

    class ScalingType(metaclass=MetaClasses.WithIter):
        DISABLED = "disabled"
        AUTOMATIC_SCALE_DOWN = "scale_down"
        AUTOMATIC_SCALE_UP_DOWN = "scale"

    DEFAULT_RUNNER_SCALING_TYPE = ScalingType.AUTOMATIC_SCALE_UP_DOWN
    MAX_WAIT_TIME_BEFORE_SCALE_DOWN_SEC = 30
