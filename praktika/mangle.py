import importlib.util
from pathlib import Path
from typing import List, Dict, Any

from praktika.utils import ContextManager
from praktika import Workflow
from praktika._settings import _Settings, _USER_DEFINED_SETTINGS


def _get_workflows(name=None, file=None) -> List[Workflow.Config]:
    """
    Gets user's workflow configs
    """
    res = []  # type: List[Workflow.Config]

    with ContextManager.cd():
        directory = Path(_Settings.WORKFLOWS_DIRECTORY)
        for py_file in directory.glob("*.py"):
            if file and file not in str(py_file):
                continue
            module_name = py_file.name.removeprefix(".py")
            spec = importlib.util.spec_from_file_location(
                module_name, f"{_Settings.WORKFLOWS_DIRECTORY}/{module_name}"
            )
            assert spec
            foo = importlib.util.module_from_spec(spec)
            assert spec.loader
            spec.loader.exec_module(foo)
            try:
                for workflow in foo.WORKFLOWS:
                    if name and name == workflow.name:
                        print(f"Read workflow [{name}] config from [{module_name}]")
                        return [workflow]
                    else:
                        res += foo.WORKFLOWS
                        print(f"Read workflow configs from [{module_name}]")
            except Exception as e:
                print(
                    f"WARNING: Failed to add WORKFLOWS config from [{module_name}], exception [{e}]"
                )
    assert res
    return res


def _get_user_settings() -> Dict[str, Any]:
    """
    Gets user's settings
    """
    res = {}  # type: Dict[str, Any]

    with ContextManager.cd():
        directory = Path(_Settings.SETTINGS_DIRECTORY)
        for py_file in directory.glob("*.py"):
            module_name = py_file.name.removeprefix(".py")
            spec = importlib.util.spec_from_file_location(
                module_name, f"{_Settings.SETTINGS_DIRECTORY}/{module_name}"
            )
            assert spec
            foo = importlib.util.module_from_spec(spec)
            assert spec.loader
            spec.loader.exec_module(foo)
            for setting in _USER_DEFINED_SETTINGS:
                try:
                    value = getattr(foo, setting)
                    res[setting] = value
                    print(f"Apply user defined setting [{setting} = {value}]")
                except Exception as e:
                    pass
    return res
