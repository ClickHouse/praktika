#!/usr/bin/env python3
from praktika.result import Result


def main():
    Result.from_pytest_run(
        "./ci/tests/test*.py",
        name="Praktika Pytests",
        pytest_logfile="./ci/tmp/pytest.log",
        logfile="./ci/tmp/pytest.stdout.log",
    ).complete_job()


if __name__ == "__main__":
    main()
