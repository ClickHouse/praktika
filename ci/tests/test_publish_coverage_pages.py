import tarfile

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
