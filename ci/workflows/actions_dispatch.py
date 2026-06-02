from ci.settings.settings import RunnerLabels
from praktika import Job, Workflow

_INSTALL_DEPS = (
    "python3 -m pip install -r ./ci/requirements.txt --break-system-packages "
    "|| python3 -m pip install -r ./ci/requirements.txt"
)


workflow = Workflow.Config(
    engine="GHActions",
    name="GHActions Dispatch Workflow",
    event=Workflow.Event.DISPATCH,
    jobs=[
        Job.Config(
            name="Hello User Name",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/tests/example_5/some_code.py",
            pre_hooks=[_INSTALL_DEPS],
        ),
    ],
    inputs=[
        Workflow.Config.InputConfig(
            name="user_name",
            is_required=True,
            default_value="",
            description="User Name",
        ),
        Workflow.Config.InputConfig(
            name="user_age",
            is_required=False,
            default_value="0",
            description="User Age",
        ),
    ],
    enable_exit_code_result=True,
)

WORKFLOWS = [workflow]
