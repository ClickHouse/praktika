import dataclasses
import json
import os
from pathlib import Path
from typing import Type, Dict, Any, Optional
import urllib.parse

from praktika import Workflow
from praktika._settings import _Settings
from praktika.s3 import S3
from praktika.settings import Settings
from praktika.utils import MetaClasses, T, Shell, Utils


@dataclasses.dataclass
class Environment(MetaClasses.Serializable):
    WORKFLOW_NAME: str
    JOB_NAME: str
    REPOSITORY: str
    BRANCH: str
    SHA: str
    PR_NUMBER: int
    EVENT_TYPE: str
    JOB_OUTPUT_STREAM: str
    EVENT_FILE_PATH: str
    CHANGE_URL: str
    COMMIT_URL: str
    BASE_BRANCH: str
    RUN_ID: str
    RUN_URL: str
    REPORT_URL: str
    INSTANCE_TYPE: str
    INSTANCE_ID: str
    INSTANCE_LIFE_CYCLE: str
    PRAKTIKA_SETUP_STEP_EXIT_CODE: Optional[int] = None
    PRAKTIKA_PRERUN_STEP_EXIT_CODE: Optional[int] = None
    PRAKTIKA_RUN_STEP_EXIT_CODE: Optional[int] = None
    name = "environment"

    def setup_ok(self):
        return self.PRAKTIKA_SETUP_STEP_EXIT_CODE == 0

    def prerun_ok(self):
        return self.PRAKTIKA_PRERUN_STEP_EXIT_CODE == 0

    @classmethod
    def file_name_static(cls, _name=""):
        return f"{_Settings.TEMP_DIR}/{cls.name}.json"

    @classmethod
    def from_dict(cls: Type[T], obj: Dict[str, Any]) -> T:
        JOB_OUTPUT_STREAM = os.getenv("GITHUB_OUTPUT", "")
        obj["JOB_OUTPUT_STREAM"] = JOB_OUTPUT_STREAM
        return cls(**obj)

    @classmethod
    def get(cls):
        if Path(cls.file_name_static()).is_file():
            return cls.from_fs("environment")
        else:
            print("WARNING: Environment: get from env")
            env = cls.from_env()
            env.dump()
            return env

    def set_job_name(self, job_name):
        self.JOB_NAME = job_name
        self.dump()
        return self

    @classmethod
    def from_env(cls) -> "Environment":
        WORKFLOW_NAME = os.getenv("GITHUB_WORKFLOW", "")
        JOB_NAME = os.getenv("JOB_NAME", "")
        REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
        BRANCH = os.getenv("GITHUB_HEAD_REF", "")

        EVENT_FILE_PATH = os.getenv("GITHUB_EVENT_PATH", "")
        JOB_OUTPUT_STREAM = os.getenv("GITHUB_OUTPUT", "")
        RUN_ID = os.getenv("GITHUB_RUN_ID", "0")
        RUN_URL = f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}"
        BASE_BRANCH = os.getenv("GITHUB_BASE_REF", "")

        if os.getenv("PRAKTIKA_SETUP_STEP_EXIT_CODE", None):
            PRAKTIKA_SETUP_STEP_EXIT_CODE = int(
                os.getenv("PRAKTIKA_SETUP_STEP_EXIT_CODE")
            )
        else:
            PRAKTIKA_SETUP_STEP_EXIT_CODE = None

        if os.getenv("PRAKTIKA_PRERUN_STEP_EXIT_CODE", None):
            PRAKTIKA_PRERUN_STEP_EXIT_CODE = int(
                os.getenv("PRAKTIKA_PRERUN_STEP_EXIT_CODE")
            )
        else:
            PRAKTIKA_PRERUN_STEP_EXIT_CODE = None

        if os.getenv("PRAKTIKA_RUN_STEP_EXIT_CODE", None):
            PRAKTIKA_RUN_STEP_EXIT_CODE = int(os.getenv("PRAKTIKA_RUN_STEP_EXIT_CODE"))
        else:
            PRAKTIKA_RUN_STEP_EXIT_CODE = None

        if EVENT_FILE_PATH:
            with open(EVENT_FILE_PATH, "r", encoding="utf-8") as f:
                github_event = json.load(f)
            if "pull_request" in github_event:
                EVENT_TYPE = Workflow.Event.PULL_REQUEST
                PR_NUMBER = github_event["pull_request"]["number"]
                SHA = github_event["pull_request"]["head"]["sha"]
                CHANGE_URL = github_event["pull_request"]["html_url"]
                COMMIT_URL = CHANGE_URL + f"/commits/{SHA}"
            elif "commits" in github_event:
                EVENT_TYPE = Workflow.Event.PUSH
                SHA = github_event["after"]
                CHANGE_URL = github_event["head_commit"]["url"]  # commit url
                PR_NUMBER = 0
                COMMIT_URL = CHANGE_URL
            else:
                assert False, "TODO: not supported"
        else:
            print("WARNING: Local execution - dummy Environment will be generated")
            SHA = "TEST"
            PR_NUMBER = -1
            EVENT_TYPE = Workflow.Event.PUSH
            CHANGE_URL = ""
            COMMIT_URL = ""

        path = Settings.HTML_S3_PATH
        for bucket, endpoint in Settings.S3_BUCKET_TO_HTTP_ENDPOINT.items():
            if bucket in path:
                path = path.replace(bucket, endpoint)
                break
        result_path = urllib.parse.quote(
            f"https://{path}/{S3.get_prefix(PR_NUMBER, BRANCH, SHA)}/result_{Path(Utils.normalize_string(WORKFLOW_NAME))}.json",
            safe="",
        )
        REPORT_URL = (
            f"https://{path}/{Path(Settings.HTML_PAGE_FILE).name}?results={result_path}"
        )

        INSTANCE_TYPE = (
            os.getenv("INSTANCE_TYPE", None)
            or Shell.get_output("ec2metadata --instance-type")
            or ""
        )
        INSTANCE_ID = (
            os.getenv("INSTANCE_ID", None)
            or Shell.get_output("ec2metadata --instance-id")
            or ""
        )
        INSTANCE_LIFE_CYCLE = (
            os.getenv("INSTANCE_LIFE_CYCLE", None)
            or Shell.get_output(
                "curl -s --fail http://169.254.169.254/latest/meta-data/instance-life-cycle"
            )
            or ""
        )

        return Environment(
            WORKFLOW_NAME=WORKFLOW_NAME,
            JOB_NAME=JOB_NAME,
            REPOSITORY=REPOSITORY,
            BRANCH=BRANCH,
            EVENT_FILE_PATH=EVENT_FILE_PATH,
            JOB_OUTPUT_STREAM=JOB_OUTPUT_STREAM,
            SHA=SHA,
            EVENT_TYPE=EVENT_TYPE,
            PR_NUMBER=PR_NUMBER,
            RUN_ID=RUN_ID,
            CHANGE_URL=CHANGE_URL,
            COMMIT_URL=COMMIT_URL,
            RUN_URL=RUN_URL,
            BASE_BRANCH=BASE_BRANCH,
            REPORT_URL=REPORT_URL,
            PRAKTIKA_SETUP_STEP_EXIT_CODE=PRAKTIKA_SETUP_STEP_EXIT_CODE,
            PRAKTIKA_PRERUN_STEP_EXIT_CODE=PRAKTIKA_PRERUN_STEP_EXIT_CODE,
            PRAKTIKA_RUN_STEP_EXIT_CODE=PRAKTIKA_RUN_STEP_EXIT_CODE,
            INSTANCE_TYPE=INSTANCE_TYPE,
            INSTANCE_ID=INSTANCE_ID,
            INSTANCE_LIFE_CYCLE=INSTANCE_LIFE_CYCLE,
        )
