from pathlib import Path

from praktika._environment import _Environment
from praktika._settings import _Settings

if Path(_Settings.ENVIRONMENT_VAR_FILE).is_file():
    Environment = _Environment.from_fs()
else:
    print("Failed to load env from fs - use current env")
    Environment = _Environment.from_env()
