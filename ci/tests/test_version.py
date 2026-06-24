import re
from pathlib import Path

from praktika.version import current_praktika_version, version_key


def test_current_praktika_version_reads_project_version():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    match = re.search(
        r'(?m)^\[project\](?:\n(?!\[).*)*\nversion\s*=\s*["\']([^"\']+)["\']',
        pyproject.read_text(encoding="utf-8"),
    )

    assert match
    assert current_praktika_version() == match.group(1)


def test_version_key_compares_numeric_components():
    assert version_key("0.1.10") > version_key("0.1.2")
