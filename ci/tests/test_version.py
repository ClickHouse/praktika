import re
from pathlib import Path

import pytest

from praktika.version import compat_version, current_praktika_version, version_key


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


def test_compat_version_uses_major_minor_branch():
    assert compat_version("0.1.10") == "0.1"


def test_compat_version_rejects_too_short_versions():
    with pytest.raises(ValueError, match="unsupported compat version format"):
        compat_version("1")
