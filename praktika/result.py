import dataclasses
import datetime
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from praktika.s3 import S3
from praktika.utils import Utils, MetaClasses, Shell
from praktika.settings import Settings
from praktika._environment import _Environment


@dataclasses.dataclass
class Result(MetaClasses.Serializable):
    class Status:
        SKIPPED = "skipped"
        SUCCESS = "success"
        FAILED = "failure"
        PENDING = "pending"
        RUNNING = "running"
        ERROR = "error"

    name: str
    status: str
    start_time: Optional[float] = None
    duration: Optional[float] = None
    results: List["Result"] = dataclasses.field(default_factory=list)
    files: List[str] = dataclasses.field(default_factory=list)
    links: List[str] = dataclasses.field(default_factory=list)
    info: str = ""
    aux_links: List[str] = dataclasses.field(default_factory=list)
    html_link: str = ""

    @staticmethod
    def get():
        return Result.from_fs(_Environment.get().JOB_NAME)

    def is_completed(self):
        return self.status not in (Result.Status.PENDING, Result.Status.RUNNING)

    def is_ok(self):
        return self.status in (Result.Status.SKIPPED, Result.Status.SUCCESS)

    def set_status(self, status) -> "Result":
        self.status = status
        self.dump()
        return self

    def set_success(self) -> "Result":
        return self.set_status(Result.Status.SUCCESS)

    def set_results(self, results: List["Result"]) -> "Result":
        self.results = results
        self.dump()
        return self

    def set_files(self, files) -> "Result":
        for file in files:
            assert Path(
                file
            ).is_file(), f"Not valid file [{file}] from file list [{files}]"
        if not self.files:
            self.files = []
        self.files += files
        self.dump()
        return self

    def set_info(self, info: str) -> "Result":
        if self.info:
            self.info += "\n"
        self.info += info
        self.dump()
        return self

    def set_link(self, link) -> "Result":
        self.links.append(link)
        self.dump()
        return self

    @classmethod
    def file_name_static(cls, name):
        return f"{Settings.RESULTS_DIR}/result_{Utils.normalize_string(name)}.json"

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "Result":
        sub_results = []
        for result_dict in obj["results"] or []:
            sub_res = cls.from_dict(result_dict)
            sub_results.append(sub_res)
        obj["results"] = sub_results
        return Result(**obj)

    def copy_to_s3(self, unlock=True):
        assert Settings.HTML_S3_PATH, "BUG?"
        self.dump()
        env = _Environment.get()
        pr_number = env.PR_NUMBER
        s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=pr_number, branch=env.BRANCH, sha=env.SHA)}"
        s3_path_full = f"{s3_path}/{Path(self.file_name()).name}"
        url = S3.copy_file_to_s3(s3_path=s3_path, local_path=self.file_name())
        if pr_number:
            print("Duplicate Result for PR for latest-sha html report")
            s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=pr_number, branch=env.BRANCH, sha='latest')}"
            url = S3.copy_file_to_s3(s3_path=s3_path, local_path=self.file_name())
        if unlock:
            if not self.unlock(s3_path_full):
                print(f"ERROR: File [{s3_path_full}] unlock failure")
                assert False  # TODO: investigate
        return url

    def get_link(self):
        env = _Environment.get()
        pr_number = env.PR_NUMBER
        sha = env.SHA if pr_number == 0 else "latest"
        s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=pr_number, branch=env.BRANCH, sha=sha)}"
        return S3.get_link(s3_path=s3_path, local_path=self.file_name())

    @classmethod
    def from_s3(cls, name, lock=True):
        assert Settings.HTML_S3_PATH, "BUG?"
        env = _Environment.get()
        file_path = cls.file_name_static(name)
        file_name = Path(file_path).name
        s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=env.PR_NUMBER, branch=env.BRANCH, sha=env.SHA)}/{file_name}"
        if lock:
            cls.lock(s3_path)
        if not S3.copy_file_from_s3(s3_path=s3_path, local_path=file_path):
            print(f"ERROR: failed to cp file [{s3_path}] from s3")
            raise RuntimeError(ResultInfo.S3_ERROR)
        result = Result.from_fs(name)
        return result

    @classmethod
    def unlock(cls, s3_path):
        s3_path_lock = s3_path + ".lock"
        env = _Environment.get()
        obj = S3.head_object(s3_path_lock)
        if not obj:
            print("ERROR: lock file is removed")
            assert False  # investigate
        elif not obj.has_tags({"job": Utils.to_base64(env.JOB_NAME)}):
            print("ERROR: lock file was acquired by another job")
            assert False  # investigate

        if not S3.delete(s3_path_lock):
            print(f"ERROR: File [{s3_path_lock}] delete failure")
        print("INFO: lock released")
        return True

    @classmethod
    def lock(cls, s3_path, level=0):
        assert level < 3, "Never"
        env = _Environment.get()
        s3_path_lock = s3_path + f".lock"
        file_path_lock = f"{Settings.TEMP_DIR}/{Path(s3_path_lock).name}"
        assert Shell.check(
            f"echo '''{env.JOB_NAME}''' > {file_path_lock}", verbose=True
        ), "Never"

        i = 20
        while S3.head_object(s3_path_lock):
            print("WARNING: Failed to acquire lock - wait")
            i -= 5
            if i < 0:
                raise RuntimeError("Failed to acquire lock")
            time.sleep(5)

        metadata = {"job": Utils.to_base64(env.JOB_NAME)}
        S3.put(
            s3_path=s3_path_lock,
            local_path=file_path_lock,
            metadata=metadata,
        )
        time.sleep(1)
        obj = S3.head_object(s3_path_lock)
        if not obj or not obj.has_tags(tags=metadata):
            print(f"WARNING: locked by another job [{obj}]")
            env.add_info(ResultInfo.S3_LOCK_FAILURE)
            cls.lock(s3_path, level=level + 1)
        print("INFO: lock acquired")

    def update_duration(self):
        if not self.duration and self.start_time:
            self.duration = datetime.datetime.utcnow().timestamp() - self.start_time
        else:
            if not self.duration:
                print(
                    f"NOTE: duration is set for job [{self.name}] Result - do not update by CI"
                )
            else:
                print(
                    f"NOTE: start_time is not set for job [{self.name}] Result - do not update duration"
                )
        return self

    def update_sub_result(self, result: "Result"):
        assert self.results, "BUG?"
        for i, result_ in enumerate(self.results):
            if result_.name == result.name:
                self.results[i] = result
        self._update_status()
        return self

    def _update_status(self):
        was_pending = False
        was_running = False
        if self.status == self.Status.PENDING:
            was_pending = True
        if self.status == self.Status.RUNNING:
            was_running = True

        has_pending, has_running, has_failed = False, False, False
        for result_ in self.results:
            if result_.status in (self.Status.RUNNING,):
                has_running = True
            if result_.status in (self.Status.PENDING,):
                has_pending = True
            if result_.status in (self.Status.ERROR, self.Status.FAILED):
                has_failed = True
        if has_running:
            self.status = self.Status.RUNNING
        elif has_pending:
            self.status = self.Status.PENDING
        elif has_failed:
            self.status = self.Status.FAILED
        else:
            self.status = self.Status.SUCCESS
        if (was_pending or was_running) and self.status not in (
            self.Status.PENDING,
            self.Status.RUNNING,
        ):
            print("Pipeline finished")
            self.update_duration()

    @classmethod
    def generate_pending(cls, name, results=None):
        return Result(
            name=name,
            status=Result.Status.PENDING,
            start_time=None,
            duration=None,
            results=results or [],
            files=[],
            links=[],
            info="",
        )

    @classmethod
    def generate_skipped(cls, name, results=None):
        return Result(
            name=name,
            status=Result.Status.SKIPPED,
            start_time=None,
            duration=None,
            results=results or [],
            files=[],
            links=[],
            info="from cache",
        )

    @classmethod
    def upload_file_to_s3(
        cls, local_file_path, upload_to_s3: bool, text: bool = False, s3_subprefix=""
    ) -> str:
        if upload_to_s3:
            env = _Environment.get()
            s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=env.PR_NUMBER, branch=env.BRANCH, sha=env.SHA)}"
            if s3_subprefix:
                s3_subprefix.removeprefix("/").removesuffix("/")
                s3_path += f"/{s3_subprefix}"
            html_link = S3.copy_file_to_s3(
                s3_path=s3_path, local_path=local_file_path, text=text
            )
            return html_link
        return f"file://{Path(local_file_path).absolute()}"

    def upload_files(self):
        if self.results:
            for result_ in self.results:
                result_.upload_files()
        for file in self.files:
            if not Path(file).is_file():
                print(f"ERROR: Invalid file [{file}] in [{self.name}] - skip upload")
                self.info += f"\nWARNING: Result file [{file}] was not found"
                file_link = self.upload_file_to_s3(file, upload_to_s3=False)
            else:
                is_text = False
                for text_file_suffix in Settings.TEXT_CONTENT_EXTENSIONS:
                    if file.endswith(text_file_suffix):
                        print(
                            f"File [{file}] matches Settings.TEXT_CONTENT_EXTENSIONS [{Settings.TEXT_CONTENT_EXTENSIONS}] - add text attribute for s3 object"
                        )
                        is_text = True
                        break
                file_link = self.upload_file_to_s3(
                    file,
                    upload_to_s3=True,
                    text=is_text,
                    s3_subprefix=Utils.normalize_string(self.name),
                )
            self.links.append(file_link)
        if self.files:
            print(
                f"Job files [{self.files}] uploaded to s3 [{self.links[-len(self.files):]}] - clean files list"
            )
            self.files = []


class ResultInfo:
    SETUP_ENV_JOB_FAILED = (
        "Failed to set up job env, it's praktika bug or misconfiguration"
    )
    PRE_JOB_FAILED = (
        "Failed to do a job pre-run step, it's praktika bug or misconfiguration"
    )
    KILLED = "Job killed or terminated, no Result provided"
    NOT_FOUND_IMPOSSIBLE = (
        "No Result file (bug, or job misbehaviour, must not ever happen)"
    )
    SKIPPED_DUE_TO_PREVIOUS_FAILURE = "Skipped due to previous failure"
    TIMEOUT = "Timeout"

    GH_STATUS_ERROR = "Failed to set GH commit status"

    NOT_FINALIZED = "Job not properly completed, praktika BUG"

    S3_ERROR = "S3 call failure"
    S3_LOCK_FAILURE = "S3 lock failure"
