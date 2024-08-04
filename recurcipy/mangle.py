import importlib.util
from pathlib import Path
from typing import List

from recurcipy import ContextManager, Workflow
from recurcipy.settings import Settings


def _get_workflows(name=None) -> List[Workflow.Config]:
    """
    Gets CI python configuration from user's repo
    :return:
    """
    with ContextManager.cd():
        directory = Path(Settings.CONFIG_DIRECTORY)
        res = []  # type: List[Workflow.Config]
        for py_file in directory.glob('*.py'):
            module_name = py_file.name.removeprefix('.py')
            spec = importlib.util.spec_from_file_location(module_name, f"{Settings.CONFIG_DIRECTORY}/{module_name}")
            foo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(foo)
            try:
                res += foo.WORKFLOWS
                print(f"Adding WORKFLOWS config from [{module_name}]")
            except Exception as e:
                print(f"WARNING: Failed to add WORKFLOWS config from [{module_name}], exception [{e}]")

    assert res
    if name:
        for wf in res:
            if wf.name == name:
                return [wf]
        else:
            return []
    return res