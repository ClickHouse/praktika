import re
from pathlib import Path

from setuptools import find_packages, setup


def _version_from_pyproject() -> str:
    # The controller version is kept in lockstep with praktika: read `version`
    # from the ROOT pyproject.toml (the single source of truth), not this package's
    # own bootstrap/pyproject.toml (which declares `dynamic = ["version"]`). The
    # root pyproject is the parent of this bootstrap/ directory.
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    in_project = False
    for raw_line in pyproject.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "[project]":
            in_project = True
            continue
        if in_project and line.startswith("["):
            break
        if in_project:
            match = re.match(r'version\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                return match.group(1)
    raise RuntimeError(f"Could not find project version in {pyproject}")


setup(
    name="praktika-controller",
    version=_version_from_pyproject(),
    description="Thin controller launcher for versioned Praktika workloads",
    url="https://github.com/ClickHouse/praktika",
    project_urls={
        "Homepage": "https://github.com/ClickHouse/praktika",
        "Repository": "https://github.com/ClickHouse/praktika",
        "Issues": "https://github.com/ClickHouse/praktika/issues",
    },
    packages=find_packages(where="src", include=["praktika_controller*"]),
    package_dir={"": "src"},
    install_requires=[
        "boto3>=1.18.0",
        "PyJWT>=2.4.0",
        "cryptography>=42.0.0",
        "requests>=2.25.0",
    ],
    entry_points={
        "console_scripts": [
            "praktika-controller=praktika_controller.main:main",
        ],
    },
)
