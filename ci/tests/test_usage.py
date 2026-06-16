from praktika.settings import Settings
from praktika.usage import StorageUsage


def test_storage_usage_add_uploaded_creates_temp_dir(tmp_path, monkeypatch):
    temp_dir = tmp_path / "missing" / "ci" / "tmp"
    artifact = tmp_path / "json.html.gz"
    artifact.write_bytes(b"abc")

    monkeypatch.setattr(Settings, "TEMP_DIR", str(temp_dir))

    StorageUsage.add_uploaded(artifact)

    usage_file = temp_dir / "storage_usage.json"
    assert usage_file.is_file()

    usage = StorageUsage.from_fs()
    assert usage.uploaded == 3
    assert usage.uploaded_details == {"json.html.gz": 3}
