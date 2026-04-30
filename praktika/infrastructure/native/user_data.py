from pathlib import Path

# TODO: refactor - move template rendering closer to the infrastructure
# definitions and decouple from cloud.py deploy logic
_HERE = Path(__file__).parent


def ci_engine_user_data():
    import base64
    import gzip

    run_py = (_HERE / "../../orchestrator/workflow_agent.py").read_text()
    template = (_HERE / "user_data_orchestrator.sh").read_text()
    placeholder = "__RUN_PY_CONTENTS__"
    if placeholder not in template:
        raise RuntimeError(
            f"user_data_orchestrator.sh is missing {placeholder} placeholder"
        )
    encoded = base64.b64encode(gzip.compress(run_py.encode("utf-8"), mtime=0)).decode("ascii")
    return template.replace(placeholder, encoded)


def runner_user_data(queue_name):
    import base64
    import gzip

    run_job_py = (_HERE / "../../orchestrator/job_agent.py").read_text()
    template = (_HERE / "user_data_runner.sh").read_text()
    for ph in ("__RUN_JOB_PY_CONTENTS__", "__RUNNER_QUEUE_NAME__"):
        if ph not in template:
            raise RuntimeError(f"user_data_runner.sh is missing {ph}")
    encoded = base64.b64encode(
        gzip.compress(run_job_py.encode("utf-8"), mtime=0)
    ).decode("ascii")
    return template.replace("__RUN_JOB_PY_CONTENTS__", encoded).replace(
        "__RUNNER_QUEUE_NAME__", queue_name
    )
