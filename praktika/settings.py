import dataclasses
import json
import os

from praktika import Workflow
from praktika.defaultsettings import DefaultSettings
from praktika.mangle import _get_user_settings

Settings = DefaultSettings()

user_settings = _get_user_settings()
for setting, value in user_settings.items():
    Settings.__setattr__(setting, value)


@dataclasses.dataclass
class EventInfo:
    REF_SHA: str = "deadbeef"
    EVENT_TYPE: str = ""
    PR_NUMBER: int = -1


class Environment:
    WORKFLOW_NAME = os.getenv("GITHUB_WORKFLOW", "")
    JOB_NAME = os.getenv("JOB_NAME", "")
    REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
    EVENT_FILE_PATH = os.getenv("GITHUB_EVENT_PATH", "")
    BRANCH = os.getenv("GITHUB_REF_NAME", "")
    JOB_OUTPUT_STREAM = os.getenv("GITHUB_OUTPUT", "")
    EventInfo = EventInfo()


if Environment.EVENT_FILE_PATH:
    with open(Environment.EVENT_FILE_PATH, "r", encoding="utf-8") as f:
        github_event = json.load(f)
    if "after" in github_event:
        Environment.EventInfo.REF_SHA = github_event["after"]
    if "pull_request" in github_event:
        Environment.EventInfo.EVENT_TYPE = Workflow.Event.PULL_REQUEST
        Environment.EventInfo.PR_NUMBER = github_event["pull_request"]["number"]
    elif "commits" in github_event:
        Environment.EventInfo.EVENT_TYPE = Workflow.Event.PUSH
        Environment.EventInfo.PR_NUMBER = 0
