import json
from types import SimpleNamespace

import pytest

from praktika import ai_review
from praktika.gh import GH
from praktika.orchestrator.ai.provider import Turn, Usage


def _thread(node_id, first_author, resolved=False, db_id=11):
    return {
        "id": node_id,
        "isResolved": resolved,
        "resolvedBy": None,
        "path": "a.py",
        "line": 10,
        "comments": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "databaseId": db_id,
                    "createdAt": "2026-05-21T00:00:00Z",
                    "author": {"login": first_author},
                    "body": "first",
                }
            ],
        },
    }


# --------------------------------------------------------------- parse helpers


def test_parse_review_tolerates_fences_and_prose():
    payload = {"summary_md": "hi", "inline_findings": [], "thread_actions": []}
    fenced = "here you go:\n```json\n" + json.dumps(payload) + "\n```\n"
    assert ai_review._parse_review(fenced) == payload
    assert ai_review._parse_review("not json at all") == {}


def test_norm_login_strips_bot_suffix():
    assert ai_review._norm_login("Review-Bot[bot]") == "review-bot"
    assert ai_review._norm_login("review-bot") == "review-bot"


# ------------------------------------------------------- thread-action safety


def test_thread_actions_only_touch_bot_owned_threads(monkeypatch):
    threads = [
        _thread("t-bot", "review-bot"),
        _thread("t-human", "some-human"),
    ]
    resolved = []
    monkeypatch.setattr(
        GH, "resolve_pr_review_thread",
        classmethod(lambda cls, tid, verbose=False: resolved.append(tid)),
    )
    actions = [
        {"thread_id": "t-bot", "action": "resolve"},
        {"thread_id": "t-human", "action": "resolve"},
        {"thread_id": "t-missing", "action": "resolve"},
    ]
    ai_review._apply_thread_actions(actions, threads, "review-bot", dry_run=False)
    # only the bot-authored thread is resolved; human + unknown are refused
    assert resolved == ["t-bot"]


def test_thread_actions_skipped_when_bot_login_unknown(monkeypatch):
    threads = [_thread("t-bot", "review-bot")]
    called = []
    monkeypatch.setattr(
        GH, "resolve_pr_review_thread",
        classmethod(lambda cls, tid, verbose=False: called.append(tid)),
    )
    ai_review._apply_thread_actions(
        [{"thread_id": "t-bot", "action": "resolve"}], threads, "", dry_run=False
    )
    assert called == []


def test_thread_reply_uses_first_comment_db_id(monkeypatch):
    threads = [_thread("t-bot", "review-bot", db_id=4242)]
    replies = []

    def _fake_line_comment(cls, body_file, in_reply_to=None, **kw):
        with open(body_file, encoding="utf-8") as f:
            replies.append((in_reply_to, f.read()))

    monkeypatch.setattr(GH, "post_pr_line_comment", classmethod(_fake_line_comment))
    ai_review._apply_thread_actions(
        [{"thread_id": "t-bot", "action": "reply", "body": "answer"}],
        threads, "review-bot", dry_run=False,
    )
    assert replies == [(4242, "answer")]


def test_dry_run_makes_no_gh_calls(monkeypatch):
    threads = [_thread("t-bot", "review-bot")]

    def _boom(*a, **k):
        raise AssertionError("no GH write expected in dry-run")

    monkeypatch.setattr(GH, "resolve_pr_review_thread", classmethod(lambda cls, *a, **k: _boom()))
    ai_review._apply_thread_actions(
        [{"thread_id": "t-bot", "action": "resolve"}], threads, "review-bot", dry_run=True
    )


# ------------------------------------------------------------ inline findings


def test_inline_findings_build_comments_with_body_files(monkeypatch):
    captured = {}

    def _fake_review(cls, commit_id, comments, body="", **kw):
        # read each body_file while it still exists (before finally unlinks)
        captured["commit_id"] = commit_id
        captured["comments"] = [
            {**c, "body": open(c["body_file"], encoding="utf-8").read()}
            for c in comments
        ]
        return True

    monkeypatch.setattr(GH, "post_pr_review", classmethod(_fake_review))
    findings = [
        {"path": "a.py", "line": 5, "body": "bug here"},
        {"path": "b.py", "line": 8, "start_line": 6, "side": "LEFT", "body": "range"},
    ]
    ai_review._post_inline_findings(findings, "sha123", dry_run=False)

    assert captured["commit_id"] == "sha123"
    c0, c1 = captured["comments"]
    assert c0["path"] == "a.py" and c0["line"] == 5 and c0["side"] == "RIGHT"
    assert c0["body"] == "bug here"
    assert c1["start_line"] == 6 and c1["side"] == "LEFT" and c1["body"] == "range"


def test_inline_findings_empty_is_noop(monkeypatch):
    monkeypatch.setattr(
        GH, "post_pr_review",
        classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("no post"))),
    )
    ai_review._post_inline_findings([], "sha", dry_run=False)


def _thread_at(node_id, author, path, line):
    t = _thread(node_id, author)
    t["path"] = path
    t["line"] = line
    return t


def test_inline_findings_skip_locations_with_existing_bot_thread(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        GH, "post_pr_review",
        classmethod(lambda cls, commit_id, comments, **k: captured.update(paths=[c["path"] for c in comments]) or True),
    )
    threads = [_thread_at("t1", "review-bot", "a.py", 10)]  # bot already commented a.py:10
    findings = [
        {"path": "a.py", "line": 10, "body": "dup"},   # should be skipped
        {"path": "b.py", "line": 5, "body": "new"},    # should be posted
    ]
    ai_review._post_inline_findings(
        findings, "sha", dry_run=False, threads=threads, bot_login="review-bot"
    )
    assert captured["paths"] == ["b.py"]


def test_inline_findings_no_dedup_when_bot_login_unknown(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        GH, "post_pr_review",
        classmethod(lambda cls, commit_id, comments, **k: captured.update(n=len(comments)) or True),
    )
    threads = [_thread_at("t1", "review-bot", "a.py", 10)]
    findings = [{"path": "a.py", "line": 10, "body": "x"}]
    # Unknown bot login -> cannot identify own threads -> no dedup, finding posts.
    ai_review._post_inline_findings(
        findings, "sha", dry_run=False, threads=threads, bot_login=""
    )
    assert captured["n"] == 1


def test_infer_bot_login_from_ci_comment(monkeypatch):
    monkeypatch.setattr(
        ai_review, "Shell",
        SimpleNamespace(get_output=lambda cmd: "" if "api user" in cmd else json.dumps([
            {"user": {"login": "human"}, "body": "just a comment"},
            {"user": {"login": "praktika-gh[bot]"},
             "body": "<!-- CI automatic comment start :report: -->\nx"},
        ])),
    )
    # _infer_bot_login_from_comments imports _Environment lazily; patch that.
    import praktika._environment as envmod
    monkeypatch.setattr(envmod._Environment, "get",
                        classmethod(lambda cls: SimpleNamespace(REPOSITORY="o/r", PR_NUMBER=1)))
    assert ai_review._authenticated_login() == "praktika-gh[bot]"


# ------------------------------------------------------------- gh payload


def test_post_pr_review_skips_when_empty(monkeypatch):
    called = []
    monkeypatch.setattr(
        GH, "do_command_with_retries",
        classmethod(lambda cls, cmd, verbose=False: called.append(cmd) or True),
    )
    assert GH.post_pr_review(commit_id="s", comments=[], body="", pr=1, repo="o/r") is True
    assert called == []


# -------------------------------------------------------------- end to end


def _stub_info(pr_number=42):
    return SimpleNamespace(
        pr_number=pr_number, pr_title="t", pr_body="b",
        pr_url="http://x", sha="deadbeef",
    )


def _wire_common(monkeypatch, review_json, threads):
    monkeypatch.setattr(ai_review, "Info", lambda: _stub_info())
    monkeypatch.setattr(ai_review, "_authenticated_login", lambda explicit="": "review-bot")
    monkeypatch.setattr(GH, "get_pr_diff", classmethod(lambda cls, *a, **k: "DIFF"))
    monkeypatch.setattr(
        GH, "list_pr_review_threads", classmethod(lambda cls, *a, **k: threads)
    )

    class _Stub:
        name = "stub"

        def resolved_model(self):
            return "stub-model"

        def complete(self, system, user_content, tools=None, tool_executor=None,
                     max_tokens=4000, response_schema=None):
            return Turn(reasoning=json.dumps(review_json), usage=Usage(provider="stub"))

    monkeypatch.setattr(ai_review, "resolve_provider", lambda spec, model="": _Stub())


def test_review_end_to_end_applies_all_actions(monkeypatch):
    threads = [_thread("t-bot", "review-bot")]
    review_json = {
        "summary_md": "## Review\nlooks ok",
        "inline_findings": [{"path": "a.py", "line": 3, "body": "nit"}],
        "thread_actions": [{"thread_id": "t-bot", "action": "resolve"}],
    }
    _wire_common(monkeypatch, review_json, threads)

    posted = {}
    monkeypatch.setattr(
        GH, "post_updateable_comment",
        classmethod(lambda cls, comment_tags_and_bodies, **k: posted.update(comment_tags_and_bodies) or True),
    )
    monkeypatch.setattr(GH, "post_pr_review", classmethod(lambda cls, commit_id, comments, **k: posted.update({"inline": len(comments)}) or True))
    monkeypatch.setattr(GH, "resolve_pr_review_thread", classmethod(lambda cls, tid, **k: posted.update({"resolved": tid}) or True))

    args = SimpleNamespace(provider="stub", model="", prompt="", bot_login="", dry_run=False)
    result = ai_review.review(args)

    # The job prepends a fixed "Code Review" heading + rule to the model summary.
    assert posted["review"] == ai_review._REVIEW_HEADER + "## Review\nlooks ok"
    assert posted["review"].startswith("---\n\n### Code Review\n\n")
    assert posted["inline"] == 1
    assert posted["resolved"] == "t-bot"
    assert result.is_ok()


@pytest.mark.parametrize("pr_number", [0, -1])
def test_review_skips_when_not_a_pr(monkeypatch, pr_number):
    monkeypatch.setattr(ai_review, "Info", lambda: _stub_info(pr_number=pr_number))
    args = SimpleNamespace(provider="mock", model="", prompt="", bot_login="", dry_run=False)
    result = ai_review.review(args)
    assert result.status == result.Status.SKIPPED
