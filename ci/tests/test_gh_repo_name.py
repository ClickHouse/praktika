from praktika.gh import GH


def test_repo_name_from_git_remote_url():
    assert (
        GH._repo_name_from_git_remote_url("git@github.com:ClickHouse/praktika.git")
        == "ClickHouse/praktika"
    )
    assert (
        GH._repo_name_from_git_remote_url(
            "https://github.com/ClickHouse/praktika.git"
        )
        == "ClickHouse/praktika"
    )
    assert (
        GH._repo_name_from_git_remote_url(
            "ssh://git@github.com/ClickHouse/praktika.git"
        )
        == "ClickHouse/praktika"
    )
