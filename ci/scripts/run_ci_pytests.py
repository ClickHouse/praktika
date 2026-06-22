#!/usr/bin/env python3
import shutil
import tarfile
from pathlib import Path

from praktika.result import Result
from praktika.utils import Shell


COVERAGE_HTML_DIR = Path("./ci/tmp/coverage/html")
COVERAGE_HTML_ARCHIVE = Path("./ci/tmp/coverage-html.tar.gz")


def main():
    result = Result.from_pytest_run(
        "./ci/tests/test*.py",
        name="Praktika Pytests",
        pytest_command="coverage run -m pytest",
        pytest_logfile="./ci/tmp/pytest.log",
        logfile="./ci/tmp/pytest.stdout.log",
    )
    if COVERAGE_HTML_DIR.exists():
        shutil.rmtree(COVERAGE_HTML_DIR)
    if COVERAGE_HTML_ARCHIVE.exists():
        COVERAGE_HTML_ARCHIVE.unlink()
    COVERAGE_HTML_DIR.mkdir(parents=True, exist_ok=True)
    if not Shell.check(f"coverage html -d {COVERAGE_HTML_DIR}", verbose=True):
        result.set_error()
        result.info = (result.info + "\n\n" if result.info else "") + (
            "Failed to generate coverage HTML report"
        )
    else:
        with tarfile.open(COVERAGE_HTML_ARCHIVE, "w:gz") as tar:
            for item in COVERAGE_HTML_DIR.iterdir():
                tar.add(item, arcname=item.name)
    result.complete_job()


if __name__ == "__main__":
    main()
