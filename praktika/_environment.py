import dataclasses
import json
import os

from praktika import Workflow
from praktika._settings import _Settings


@dataclasses.dataclass
class _Environment:
    WORKFLOW_NAME: str
    JOB_NAME: str
    REPOSITORY: str
    BRANCH: str
    SHA: str
    PR_NUMBER: int
    EVENT_TYPE: str
    SECRET_APP_PEM_KEY: str
    SECRET_APP_ID: str
    _JOB_OUTPUT_STREAM: str
    _EVENT_FILE_PATH: str

    def dump(self):
        with open(_Settings.ENVIRONMENT_VAR_FILE, "w", encoding="utf8") as f:
            json.dump(dataclasses.asdict(self), fp=f)

    @classmethod
    def from_fs(cls):
        with open(_Settings.ENVIRONMENT_VAR_FILE, "r", encoding="utf8") as f:
            return _Environment(**json.load(f))

    @classmethod
    def from_env(cls):
        WORKFLOW_NAME = os.getenv("GITHUB_WORKFLOW", "")
        JOB_NAME = os.getenv("JOB_NAME", "")
        REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
        BRANCH = os.getenv("GITHUB_REF_NAME", "")
        SECRET_APP_PEM_KEY = os.getenv("GH_APP_PEM_KEY", "")
        SECRET_APP_ID = os.getenv("GH_APP_ID", "")

        _EVENT_FILE_PATH = os.getenv("GITHUB_EVENT_PATH", "")
        _JOB_OUTPUT_STREAM = os.getenv("GITHUB_OUTPUT", "")

        if _EVENT_FILE_PATH:
            with open(_EVENT_FILE_PATH, "r", encoding="utf-8") as f:
                github_event = json.load(f)
            if "after" in github_event:
                SHA = github_event["after"]
            else:
                assert False, "TODO: not supported"
            if "pull_request" in github_event:
                EVENT_TYPE = Workflow.Event.PULL_REQUEST
                PR_NUMBER = github_event["pull_request"]["number"]
            elif "commits" in github_event:
                EVENT_TYPE = Workflow.Event.PUSH
                PR_NUMBER = 0
            else:
                assert False, "TODO: not supported"
        else:
            print("WARNING: Local execution - dummy Environment will be generated")
            SHA = "TEST"
            PR_NUMBER = -1
            EVENT_TYPE = Workflow.Event.PUSH

        return _Environment(
            WORKFLOW_NAME=WORKFLOW_NAME,
            JOB_NAME=JOB_NAME,
            REPOSITORY=REPOSITORY,
            BRANCH=BRANCH,
            SECRET_APP_PEM_KEY=SECRET_APP_PEM_KEY,
            SECRET_APP_ID=SECRET_APP_ID,
            _EVENT_FILE_PATH=_EVENT_FILE_PATH,
            _JOB_OUTPUT_STREAM=_JOB_OUTPUT_STREAM,
            SHA=SHA,
            EVENT_TYPE=EVENT_TYPE,
            PR_NUMBER=PR_NUMBER,
        )
