import os


class Environment:
    TEMP_DIR = "~/lh_ci"
    OUTPUT_DIR = f"{TEMP_DIR}/output"
    INPUT_DIR = f"{TEMP_DIR}/input"
    WORKFLOW_NAME = os.getenv("GITHUB_WORKFLOW", "NA")
    JOB_NAME = os.getenv("JOB_NAME", "NA")
    REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
    LOCAL_EXECUTION = os.getenv("GITHUB_REPOSITORY", "") == ""
