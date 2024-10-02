from pathlib import Path

from praktika.environment import Environment
from praktika.result import Result
from praktika.settings import Settings

if __name__ == "__main__":

    assert Path(
        f"{Settings.INPUT_DIR}/artifact.txt"
    ).is_file(), "required artifact not found"

    print(f"Job Parameter: {Environment.PARAMETER}")

    Result.get().set_success()
