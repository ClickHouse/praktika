from praktika.version import current_praktika_version, version_key


def test_current_praktika_version_reads_project_version():
    assert current_praktika_version() == "0.1.2"


def test_version_key_compares_numeric_components():
    assert version_key("0.1.10") > version_key("0.1.2")
