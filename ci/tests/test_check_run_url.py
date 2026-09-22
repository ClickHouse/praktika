from types import SimpleNamespace

from praktika.info import Info


def _info_with_env(**env):
    info = Info.__new__(Info)
    info.env = SimpleNamespace(**env)
    info.workflow = None
    return info


def test_get_job_url_native_path_points_at_job_check():
    # Native path: RUN_URL is just the PR/change URL, so the job link must be
    # built from this job's own check-run id (not <run>/job/<id>).
    info = _info_with_env(
        WORKFLOW_JOB_DATA={"check_run_id": 106575590543},
        RUN_URL="https://github.com/ClickHouse/praktika/pull/147",
        REPOSITORY="ClickHouse/praktika",
        PR_NUMBER=147,
        SHA="abc123",
    )
    assert (
        info.get_job_url()
        == "https://github.com/ClickHouse/praktika/pull/147/checks?check_run_id=106575590543"
    )


def test_get_job_url_actions_path_keeps_legacy_job_url():
    info = _info_with_env(
        WORKFLOW_JOB_DATA={"check_run_id": 999},
        RUN_URL="https://github.com/ClickHouse/ClickHouse/actions/runs/35300151128",
        REPOSITORY="ClickHouse/ClickHouse",
        PR_NUMBER=0,
        SHA="deadbeef",
    )
    assert (
        info.get_job_url()
        == "https://github.com/ClickHouse/ClickHouse/actions/runs/35300151128/job/999"
    )


def test_get_job_url_empty_without_job_data():
    info = _info_with_env(WORKFLOW_JOB_DATA={})
    assert info.get_job_url() == ""


def test_check_run_url_for_pr_anchors_under_pull():
    assert (
        Info.get_check_run_url_static(
            repo="ClickHouse/praktika",
            check_run_id=106575590543,
            pr_number=147,
            sha="abc123",
        )
        == "https://github.com/ClickHouse/praktika/pull/147/checks?check_run_id=106575590543"
    )


def test_check_run_url_for_push_anchors_under_commit():
    assert (
        Info.get_check_run_url_static(
            repo="ClickHouse/praktika",
            check_run_id=42,
            pr_number=0,
            sha="deadbeef",
        )
        == "https://github.com/ClickHouse/praktika/commit/deadbeef/checks?check_run_id=42"
    )


def test_check_run_url_falls_back_to_bare_runs_path_without_anchor():
    assert (
        Info.get_check_run_url_static(repo="ClickHouse/praktika", check_run_id=42)
        == "https://github.com/ClickHouse/praktika/runs/42"
    )


def test_check_run_url_empty_when_id_or_repo_missing():
    assert Info.get_check_run_url_static(repo="ClickHouse/praktika", check_run_id=0) == ""
    assert Info.get_check_run_url_static(repo="", check_run_id=42) == ""
