import pytest

from praktika._environment import _Environment


def test_get_s3_prefix_always_includes_workflow_name():
    assert _Environment.get_s3_prefix_static(69, "feature", "abc123", "PR Full") == "PRs/69/abc123/pr_full"
    assert _Environment.get_s3_prefix_static(0, "main", "abc123", "Main CI") == "REFs/main/abc123/main_ci"
    assert _Environment.get_s3_prefix_static(0, "master", "abc123", "Master Build") == "REFs/master/abc123/master_build"
    assert _Environment.get_s3_prefix_static(69, "feature", "abc123", "Lint") == "PRs/69/abc123/lint"
    assert _Environment.get_s3_prefix_static(0, "feature", "abc123", "Nightly Integration") == "REFs/feature/abc123/nightly_integration"


def test_get_s3_prefix_requires_workflow_name():
    with pytest.raises(AssertionError):
        _Environment.get_s3_prefix_static(69, "feature", "abc123", "")
