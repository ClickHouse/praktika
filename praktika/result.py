import dataclasses
import datetime
import json
from pathlib import Path
from typing import Optional, List, Dict, Any, Union

from praktika.s3 import S3
from praktika.utils import Utils
from praktika.settings import Settings
from praktika.environment import Environment


@dataclasses.dataclass
class Result:
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
    results: Optional[List["Result"]] = None
    files: Optional[List[str]] = None
    urls: Optional[List[str]] = None
    info: str = ""
    html_link: str = ""

    @classmethod
    def set_status(cls, status) -> "Result":
        result = cls.from_fs(Environment.JOB_NAME)
        assert result
        result.status = status
        result.dump()
        return result

    @classmethod
    def set_success(cls) -> "Result":
        return cls.set_status(cls.Status.SUCCESS)

    @classmethod
    def set_results(cls, results: List["Result"]) -> "Result":
        result = cls.from_fs(Environment.JOB_NAME)
        assert result
        result.results = results
        result.dump()
        return result

    @classmethod
    def set_files(cls, files) -> "Result":
        for file in files:
            assert Path(
                file
            ).is_file(), f"Not valid file [{file}] from file list [{files}]"
        result = cls.from_fs(Environment.JOB_NAME)
        assert result
        if not result.files:
            result.files = []
        result.files += files
        result.dump()
        return result

    @classmethod
    def set_info(cls, info: str) -> "Result":
        result = cls.from_fs(Environment.JOB_NAME)
        assert result
        result.info = info
        result.dump()
        return result

    def dump(self):
        path = self._get_file_name(self.name)
        with open(path, "w", encoding="utf8") as f:
            json.dump(dataclasses.asdict(self), f)
        return self

    @staticmethod
    def _get_file_name(name):
        assert name
        return Path(Settings.RESULTS_DIR) / f"{Utils.normalize_string(name)}.json"

    @classmethod
    def from_fs(cls, name) -> Optional["Result"]:
        path = cls._get_file_name(name)
        try:
            with open(path, "r", encoding="utf8") as f:
                dict_obj = json.load(f)
                return cls.from_dict(dict_obj)
        except Exception as ex:
            print(f"ERROR: failed to load Results from [{path}], exception [{ex}]")
            return None

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "Result":
        sub_results = []
        for result_dict in obj["results"] or []:
            sub_res = cls.from_dict(result_dict)
            sub_results.append(sub_res)
        obj["results"] = sub_results
        return Result(**obj)

    def copy_to_s3(self):
        assert Settings.HTML_S3_PATH, "BUG?"
        self.dump()
        s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=Environment.PR_NUMBER, branch=Environment.BRANCH, sha=Environment.SHA)}"
        html_link = S3.copy_file_to_s3(
            s3_path=s3_path, local_path=self._get_file_name(self.name)
        )
        return html_link

    @classmethod
    def from_s3(cls, name):
        assert Settings.HTML_S3_PATH, "BUG?"
        file_path = cls._get_file_name(name)
        file_name = Path(file_path).name
        s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=Environment.PR_NUMBER, branch=Environment.BRANCH, sha=Environment.SHA)}/{file_name}"
        S3.copy_file_from_s3(s3_path=s3_path, local_path=file_path)
        result = Result.from_fs(name)
        assert result
        return result

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
            urls=[],
            info="",
        )


@dataclasses.dataclass
class _PreResult:
    name: str
    start_time: float

    @classmethod
    def get_path(cls, name):
        path = Path(Settings.RESULTS_DIR) / f"{Utils.normalize_string(name)}_pre.json"
        return path

    def dump(self):
        with open(self.get_path(self.name), "w", encoding="utf8") as f:
            json.dump(dataclasses.asdict(self), f)
        assert Path(self.get_path(self.name)).is_file()
        return self

    @classmethod
    def from_fs(cls, name: str) -> Optional["_PreResult"]:
        assert Path(cls.get_path(name)).is_file()
        try:
            with open(cls.get_path(name), "r", encoding="utf8") as f:
                return _PreResult(**json.load(f))
        except Exception as ex:
            print(
                f"ERROR: failed to load Results from [{cls.get_path(name)}], exception [{ex}]"
            )
            return None


class ResultInfo:
    NOT_FOUND = "No results found (job killed or terminated, no @Result provided)"
    NOT_FOUND_IMPOSSIBLE = (
        "No job @Result or pre-run @Result (bug, or job misbehaviour, must not be here)"
    )
    SKIPPED_DUE_TO_PREVIOUS_FAILURE = "Skipped due to previous dependency job failure"
