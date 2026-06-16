import tomllib
from pathlib import Path


def test_native_runtime_assets_are_included_in_package_data():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf8"))

    package_data = config["tool"]["setuptools"]["package-data"]["praktika"]

    assert "infrastructure/native/user_data_cidb.sh" in package_data
    assert "infrastructure/native/cidb_schema.sql" in package_data
