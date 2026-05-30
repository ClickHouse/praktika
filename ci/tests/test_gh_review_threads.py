from praktika.gh import GH


def test_list_pr_review_threads_includes_resolved_by_and_paginates_comments(monkeypatch):
    responses = [
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "thread-1",
                                    "isResolved": True,
                                    "isOutdated": False,
                                    "resolvedBy": {"login": "clickhouse-gh[bot]"},
                                    "path": "a.py",
                                    "line": 10,
                                    "comments": {
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "cursor-1",
                                        },
                                        "nodes": [
                                            {
                                                "databaseId": 1,
                                                "createdAt": "2026-05-21T00:00:00Z",
                                                "author": {"login": "reviewer"},
                                                "body": "first",
                                                "path": "a.py",
                                                "line": 10,
                                                "originalLine": 10,
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    }
                }
            }
        },
        {
            "data": {
                "node": {
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "databaseId": 2,
                                "createdAt": "2026-05-21T00:01:00Z",
                                "author": {"login": "author"},
                                "body": "second",
                                "path": "a.py",
                                "line": 11,
                                "originalLine": 11,
                            }
                        ],
                    }
                }
            }
        },
    ]

    calls = []

    def _fake_graphql(query, variables, verbose=False):
        calls.append((query, variables))
        return responses[len(calls) - 1]

    monkeypatch.setattr(GH, "_gh_graphql_json", classmethod(lambda cls, query, variables, verbose=False: _fake_graphql(query, variables, verbose)))

    threads = GH.list_pr_review_threads(pr=123, repo="ClickHouse/praktika")

    assert len(threads) == 1
    assert threads[0]["resolvedBy"]["login"] == "clickhouse-gh[bot]"
    assert [c["databaseId"] for c in threads[0]["comments"]["nodes"]] == [1, 2]
