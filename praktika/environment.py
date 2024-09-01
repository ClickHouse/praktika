import dataclasses
import json
import os
from pathlib import Path
from typing import Type, Dict, Any

from praktika import Workflow
from praktika._settings import _Settings
from praktika.utils import MetaClasses, T


@dataclasses.dataclass
class Environment(MetaClasses.Serializable):
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
    EVENT_FILE_PATH: str
    CHANGE_URL: str
    RUN_ID: str
    RUN_URL: str
    name = "environment"

    @classmethod
    def file_name_static(cls, _name=""):
        return f"{_Settings.TEMP_DIR}/{cls.name}.json"

    @classmethod
    def from_dict(cls: Type[T], obj: Dict[str, Any]) -> T:
        _JOB_OUTPUT_STREAM = os.getenv("GITHUB_OUTPUT", "")
        obj["_JOB_OUTPUT_STREAM"] = _JOB_OUTPUT_STREAM
        return cls(**obj)

    @classmethod
    def get(cls):
        if Path(cls.file_name_static()).is_file():
            return cls.from_fs("environment")
        else:
            return cls.from_env()

    def set_job_name(self, job_name):
        self.JOB_NAME = job_name
        self.dump()
        return self

    @classmethod
    def from_env(cls):
        WORKFLOW_NAME = os.getenv("GITHUB_WORKFLOW", "")
        JOB_NAME = os.getenv("JOB_NAME", "")
        REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
        BRANCH = os.getenv("GITHUB_HEAD_REF", "")
        SECRET_APP_PEM_KEY = os.getenv("GH_APP_PEM_KEY", "")
        SECRET_APP_ID = os.getenv("GH_APP_ID", "")

        EVENT_FILE_PATH = os.getenv("GITHUB_EVENT_PATH", "")
        _JOB_OUTPUT_STREAM = os.getenv("GITHUB_OUTPUT", "")
        RUN_ID = os.getenv("GITHUB_RUN_ID", "0")
        RUN_URL = f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}"

        if EVENT_FILE_PATH:
            with open(EVENT_FILE_PATH, "r", encoding="utf-8") as f:
                github_event = json.load(f)
            if "pull_request" in github_event:
                EVENT_TYPE = Workflow.Event.PULL_REQUEST
                PR_NUMBER = github_event["pull_request"]["number"]
                SHA = github_event["after"]
                CHANGE_URL = github_event["pull_request"]["html_url"]
            elif "commits" in github_event:
                EVENT_TYPE = Workflow.Event.PUSH
                SHA = github_event["after"]
                CHANGE_URL = github_event["head_commit"]["url"]  # commit url
                PR_NUMBER = 0
            else:
                assert False, "TODO: not supported"
        else:
            print("WARNING: Local execution - dummy Environment will be generated")
            SHA = "TEST"
            PR_NUMBER = -1
            EVENT_TYPE = Workflow.Event.PUSH
            CHANGE_URL = ""

        return Environment(
            WORKFLOW_NAME=WORKFLOW_NAME,
            JOB_NAME=JOB_NAME,
            REPOSITORY=REPOSITORY,
            BRANCH=BRANCH,
            SECRET_APP_PEM_KEY=SECRET_APP_PEM_KEY,
            SECRET_APP_ID=SECRET_APP_ID,
            EVENT_FILE_PATH=EVENT_FILE_PATH,
            _JOB_OUTPUT_STREAM=_JOB_OUTPUT_STREAM,
            SHA=SHA,
            EVENT_TYPE=EVENT_TYPE,
            PR_NUMBER=PR_NUMBER,
            RUN_ID=RUN_ID,
            CHANGE_URL=CHANGE_URL,
            RUN_URL=RUN_URL,
        )
