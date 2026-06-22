import tarfile
from types import SimpleNamespace

from ci.scripts import publish_coverage_pages


def test_extract_coverage_archive_requires_index_html(tmp_path, monkeypatch):
    archive = tmp_path / "coverage-html.tar.gz"
    source = tmp_path / "source"
    source.mkdir()
    (source / "status.json").write_text("{}", encoding="utf-8")

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "status.json", arcname="status.json")

    monkeypatch.setattr(publish_coverage_pages, "COVERAGE_HTML_ARCHIVE", archive)
    monkeypatch.setattr(
        publish_coverage_pages,
        "COVERAGE_HTML_DIR",
        tmp_path / "extracted",
    )

    try:
        publish_coverage_pages._extract_coverage_archive()
        assert False, "expected missing index.html failure"
    except RuntimeError as e:
        assert "index.html" in str(e)


def test_extract_coverage_archive_accepts_index_html(tmp_path, monkeypatch):
    archive = tmp_path / "coverage-html.tar.gz"
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("coverage", encoding="utf-8")

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "index.html", arcname="index.html")

    extracted = tmp_path / "extracted"
    monkeypatch.setattr(publish_coverage_pages, "COVERAGE_HTML_ARCHIVE", archive)
    monkeypatch.setattr(publish_coverage_pages, "COVERAGE_HTML_DIR", extracted)

    publish_coverage_pages._extract_coverage_archive()

    assert (extracted / "index.html").read_text(encoding="utf-8") == "coverage"


def test_publish_coverage_pages_updates_latest_indexes(tmp_path, monkeypatch):
    archive = tmp_path / "coverage-html.tar.gz"
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("coverage", encoding="utf-8")

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "index.html", arcname="index.html")

    extracted = tmp_path / "extracted"
    latest_index = tmp_path / "latest"
    calls = []
    labels = []
    completed = []

    class _Result:
        Status = SimpleNamespace(OK="OK")

        @classmethod
        def create_from(cls, **kwargs):
            result = cls()
            result.kwargs = kwargs
            return result

        def set_label(self, name, link=None):
            labels.append((name, link))

        def complete_job(self):
            completed.append(self.kwargs)

    def _fake_publish(source_dir, **kwargs):
        calls.append((source_dir, kwargs))
        destination = kwargs.get("destination_dir", "")
        if destination == "coverage/pr-126":
            return "https://clickhouse.github.io/praktika/coverage/pr-126/"
        if destination == "coverage":
            return "https://clickhouse.github.io/praktika/coverage/"
        if destination == "":
            return "https://clickhouse.github.io/praktika/"
        raise AssertionError(f"unexpected destination {destination}")

    monkeypatch.setattr(publish_coverage_pages, "COVERAGE_HTML_ARCHIVE", archive)
    monkeypatch.setattr(publish_coverage_pages, "COVERAGE_HTML_DIR", extracted)
    monkeypatch.setattr(
        publish_coverage_pages, "LATEST_COVERAGE_INDEX_DIR", latest_index
    )
    monkeypatch.setattr(
        publish_coverage_pages,
        "Info",
        lambda: SimpleNamespace(pr_number=126, sha="abcdef1234567890"),
    )
    monkeypatch.setattr(
        publish_coverage_pages.GH, "publish_gh_pages", staticmethod(_fake_publish)
    )
    monkeypatch.setattr(publish_coverage_pages, "Result", _Result)

    publish_coverage_pages.main()

    assert [call[1].get("destination_dir", "") for call in calls] == [
        "coverage/pr-126",
        "coverage",
        "",
    ]
    assert calls[1][1]["clean_destination"] is False
    assert calls[2][1]["clean_destination"] is False
    assert "coverage/pr-126/" in (latest_index / "index.html").read_text(
        encoding="utf-8"
    )
    assert completed[0]["links"] == [
        "https://clickhouse.github.io/praktika/coverage/",
        "https://clickhouse.github.io/praktika/coverage/pr-126/",
    ]
    assert labels == [("coverage", "https://clickhouse.github.io/praktika/coverage/")]
