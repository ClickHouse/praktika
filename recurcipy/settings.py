import os
from recurcipy.defaultsettings import DefaultSettings
from recurcipy.mangle import _get_user_settings

Settings = DefaultSettings()

user_settings = _get_user_settings()
for setting, value in user_settings.items():
    Settings.__setattr__(setting, value)


class Environment:
    WORKFLOW_NAME = os.getenv("GITHUB_WORKFLOW", "NA")
    JOB_NAME = os.getenv("JOB_NAME", "NA")
    REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
