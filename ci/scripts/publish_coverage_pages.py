#!/usr/bin/env python3
import shutil
import tarfile
from pathlib import Path

from praktika.gh import GH
from praktika.info import Info
from praktika.result import Result
from praktika.settings import Settings

COVERAGE_HTML_ARCHIVE = Path(Settings.INPUT_DIR) / "coverage-html.tar.gz"
COVERAGE_HTML_DIR = Path(Settings.TEMP_DIR) / "coverage/html_publish"
LATEST_COVERAGE_INDEX_DIR = Path(Settings.TEMP_DIR) / "coverage/latest_index"


def _extract_coverage_archive():
    if not COVERAGE_HTML_ARCHIVE.is_file():
        raise FileNotFoundError(f"Coverage archive not found: {COVERAGE_HTML_ARCHIVE}")

    if COVERAGE_HTML_DIR.exists():
        shutil.rmtree(COVERAGE_HTML_DIR)
    COVERAGE_HTML_DIR.mkdir(parents=True, exist_ok=True)

    with tarfile.open(COVERAGE_HTML_ARCHIVE, "r:gz") as tar:
        target_root = COVERAGE_HTML_DIR.resolve()
        for member in tar.getmembers():
            target = (COVERAGE_HTML_DIR / member.name).resolve()
            if target_root not in (target, *target.parents):
                raise RuntimeError(f"Invalid coverage archive path: {member.name}")
        try:
            tar.extractall(COVERAGE_HTML_DIR, filter="data")
        except TypeError:
            tar.extractall(COVERAGE_HTML_DIR)

    if not (COVERAGE_HTML_DIR / "index.html").is_file():
        raise RuntimeError(
            f"Coverage archive did not extract an index.html into {COVERAGE_HTML_DIR}"
        )


def _write_redirect_index(target_url: str):
    if LATEST_COVERAGE_INDEX_DIR.exists():
        shutil.rmtree(LATEST_COVERAGE_INDEX_DIR)
    LATEST_COVERAGE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (LATEST_COVERAGE_INDEX_DIR / "index.html").write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<html lang="en">',
                "<head>",
                '<meta charset="utf-8">',
                f'<meta http-equiv="refresh" content="0; url={target_url}">',
                f'<link rel="canonical" href="{target_url}">',
                "<title>Latest coverage report</title>",
                "</head>",
                "<body>",
                f'<p><a href="{target_url}">Latest coverage report</a></p>',
                "</body>",
                "</html>",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main():
    info = Info()
    if info.pr_number:
        destination_dir = f"coverage/pr-{info.pr_number}"
    else:
        destination_dir = f"coverage/{info.sha[:12]}"

    _extract_coverage_archive()
    url = GH.publish_gh_pages(
        str(COVERAGE_HTML_DIR),
        destination_dir=destination_dir,
        commit_message=f"Publish coverage report for {info.sha[:12]}",
    )
    _write_redirect_index(url)
    latest_url = GH.publish_gh_pages(
        str(LATEST_COVERAGE_INDEX_DIR),
        destination_dir="coverage",
        commit_message=f"Update latest coverage report for {info.sha[:12]}",
        clean_destination=False,
    )
    GH.publish_gh_pages(
        str(LATEST_COVERAGE_INDEX_DIR),
        commit_message=f"Update coverage report index for {info.sha[:12]}",
        clean_destination=False,
    )
    result = Result.create_from(
        name="Publish Coverage Report",
        status=Result.Status.OK,
        info=f"Coverage report: {latest_url}\nReport snapshot: {url}",
        links=[latest_url, url],
    )
    result.set_label("coverage", link=latest_url)
    result.complete_job()


if __name__ == "__main__":
    main()
