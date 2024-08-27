import dataclasses
import json

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from praktika.cache import Cache
from praktika.settings import Settings


@dataclass
class _WorkflowRuntimeConfig:
    digests: Dict[str, str]
    cache_success: List[str]
    cache_artifacts: Dict[str, Cache.CacheRecord]
    sha: str

    @classmethod
    def from_fs(cls):
        with open(Settings.WORKFLOW_CONFIG_FILE, "r", encoding="utf8") as f:
            data = json.load(f)
            # Deserialize cache_artifacts into a dictionary of CacheRecord instances
            if "cache_artifacts" in data:
                data["cache_artifacts"] = {
                    k: Cache.CacheRecord(**v)
                    for k, v in data["cache_artifacts"].items()
                }
            return cls(**data)

    def dump(self):
        with open(Settings.WORKFLOW_CONFIG_FILE, "w", encoding="utf8") as f:
            print(json.dumps(dataclasses.asdict(self)), file=f)


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
