import dataclasses
import json

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from praktika.cache import Cache
from praktika.settings import Settings
from praktika.utils import MetaClasses


@dataclass
class WorkflowRuntime(MetaClasses.Serializable):
    digests: Dict[str, str]
    cache_success: List[str]
    cache_artifacts: Dict[str, Cache.CacheRecord]
    sha: str

    @classmethod
    def from_dict(cls, obj):
        cache_artifacts = obj["cache_artifacts"]
        cache_artifacts_deserialized = []
        for cache_artifact in cache_artifacts:
            cache_artifacts_deserialized.append(
                Cache.CacheRecord.from_dict(cache_artifact)
            )
        obj["cache_artifacts"] = cache_artifacts_deserialized
        return WorkflowRuntime(**obj)


@dataclass
class _RuntimeVars:
    RUN_EXIT_CODE: Optional[int]
    _PATH = Settings.TEMP_DIR + "/runtime.json"

    def dump(self):
        with open(self._PATH, "w", encoding="utf8") as f:
            json.dump(asdict(self), fp=f)

    @classmethod
    def from_fs(cls):
        with open(cls._PATH, "r", encoding="utf8") as f:
            return _RuntimeVars(**json.load(fp=f))

    @classmethod
    def run_failed(cls):
        return cls.from_fs().RUN_EXIT_CODE != 0
