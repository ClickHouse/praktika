from praktika.result import Result
from praktika.utils import Utils

if __name__ == "__main__":
    sw = Utils.Stopwatch()
    artifact_path = "./ci/tmp/artifact.txt"
    with open(artifact_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    Result.get().set_results(
        results=[
            Result(
                name="Read artifact",
                status=Result.Status.OK,
                start_time=sw.start_time,
                duration=sw.duration,
                info=f"artifact.txt: {content!r}",
            ),
        ]
    ).complete_job()
