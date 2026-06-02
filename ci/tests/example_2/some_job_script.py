from praktika.result import Result
from praktika.settings import Settings
from praktika.utils import Shell, Utils

if __name__ == "__main__":
    # 1. do some work

    # dummy job artifact
    artifact_path = f"{Settings.OUTPUT_DIR}/some_job_artifact.txt"
    Shell.check(f"echo 'Hello World!' > {artifact_path}")

    # 2. dump results
    Result.get().set_success().set_info("all good").set_files(
        files=[artifact_path]
    ).set_results(
        results=[
            Result(
                name="Test 1",
                status=Result.Status.OK,
                start_time=Utils.timestamp(),
                duration=1.0,
                info="all good",
            ),
            Result(
                name="Test 2",
                status=Result.Status.SKIPPED,
                start_time=Utils.timestamp(),
                duration=2.0,
            ),
        ]
    )  # set success, set info which can be expanded and seen in html report, add some sub results a.k.a. testcases results
