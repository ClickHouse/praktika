from praktika._environment import _Environment


def test_get_s3_prefix_skips_pr_main_master_workflow_names():
    assert _Environment.get_s3_prefix_static(69, "feature", "abc123", "PR Full") == "PRs/69/abc123"
    assert _Environment.get_s3_prefix_static(0, "main", "abc123", "Main CI") == "REFs/main/abc123"
    assert _Environment.get_s3_prefix_static(0, "master", "abc123", "Master Build") == "REFs/master/abc123"


def test_get_s3_prefix_includes_other_workflow_names():
    assert _Environment.get_s3_prefix_static(69, "feature", "abc123", "Lint") == "PRs/69/abc123/lint"
    assert _Environment.get_s3_prefix_static(0, "feature", "abc123", "Nightly Integration") == "REFs/feature/abc123/nightly_integration"
