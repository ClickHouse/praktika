import dataclasses
import json
from pathlib import Path
from typing import Optional, List

from praktika.utils import Utils
from praktika.settings import Settings


@dataclasses.dataclass
class Result:
    class Status:
        SUCCESS = "success"
        FAILED = "failed"
        PENDING = "pending"
        ERROR = "error"

    name: str
    status: str
    start_time: str
    duration: Optional[int]
    results: Optional[List["Result"]]
    files: Optional[List[str]]
    urls: Optional[List[str]]
    info: str = ""

    def dump(self):
        path = Path(Settings.RESULTS_DIR) / f"{Utils.normalize_string(self.name)}.json"
        with open(path, "w", encoding="utf8") as f:
            json.dump(dataclasses.asdict(self), f)
        return self

    @staticmethod
    def from_fs(self, name: str) -> "Result":
        path = Path(Settings.RESULTS_DIR) / f"{Utils.normalize_string(name)}.json"
        with open(path, "r", encoding="utf8") as f:
            return Result(**json.load(f))

    @classmethod
    def generate_pending(cls, name, results=None):
        return Result(
            name=name,
            status=Result.Status.PENDING,
            start_time="",
            duration=None,
            results=results or [],
            files=[],
            urls=[],
            info="",
        )
