import dataclasses
import json
from dataclasses import dataclass
from typing import Dict, List

from recurcipy.cache import Cache
from recurcipy.settings import Settings


@dataclass
class _WorkflowRuntimeConfig:
    digests: Dict[str, str]
    cache_success: List[str]
    cache_artifacts: Dict[str, Cache.CacheRecord]
    sha: str

    @classmethod
    def from_fs(cls):
        with open(Settings.WORKFLOW_RUN_CONFIG_FILE, "r", encoding="utf8") as f:
            data = json.load(f)
            # Deserialize cache_artifacts into a dictionary of CacheRecord instances
            if 'cache_artifacts' in data:
                data['cache_artifacts'] = {
                    k: Cache.CacheRecord(**v) for k, v in data['cache_artifacts'].items()
                }
            return cls(**data)

    def dump(self):
        with open(Settings.WORKFLOW_RUN_CONFIG_FILE, "w", encoding="utf8") as f:
            print(json.dumps(dataclasses.asdict(self)), file=f)
