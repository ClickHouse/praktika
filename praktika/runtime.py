import json

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional

from praktika.cache import Cache
from praktika.settings import Settings
from praktika.utils import MetaClasses, Utils


@dataclass
class WorkflowRuntime(MetaClasses.Serializable):
    name: str
    digest_jobs: Dict[str, str]
    digest_dockers: Dict[str, str]
    cache_success: List[str]
    cache_artifacts: Dict[str, Cache.CacheRecord]
    sha: str

    @classmethod
    def from_dict(cls, obj):
        cache_artifacts = obj["cache_artifacts"]
        cache_artifacts_deserialized = {}
        for artifact_name, cache_artifact in cache_artifacts.items():
            cache_artifacts_deserialized[artifact_name] = Cache.CacheRecord.from_dict(
                cache_artifact
            )
        obj["cache_artifacts"] = cache_artifacts_deserialized
        return WorkflowRuntime(**obj)

    @classmethod
    def file_name_static(cls, name):
        return f"{Settings.RESULTS_DIR}/workflow_config_{Utils.normalize_string(name)}.json"


@dataclass
class _RuntimeVars:
    exit_code: Optional[int]
    log_files: List[str] = field(default_factory=list)
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
        return cls.from_fs().exit_code != 0
