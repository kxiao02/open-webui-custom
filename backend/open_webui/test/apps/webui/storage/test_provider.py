import io

import pytest

import open_webui.test_support  # noqa: F401
from open_webui.storage import provider


def mock_upload_dir(monkeypatch, tmp_path):
    directory = tmp_path / "uploads"
    directory.mkdir()
    monkeypatch.setattr(provider, "UPLOAD_DIR", str(directory))
    return directory


def test_imports():
    provider.StorageProvider
    provider.LocalStorageProvider
    provider.S3StorageProvider
    provider.GCSStorageProvider
    provider.AzureStorageProvider
    provider.Storage


def test_get_storage_provider_local():
    storage = provider.get_storage_provider("local")
    assert isinstance(storage, provider.LocalStorageProvider)


def test_get_storage_provider_invalid():
    with pytest.raises(RuntimeError):
        provider.get_storage_provider("invalid")


def test_optional_storage_provider_dependency_guards(monkeypatch):
    monkeypatch.setattr(provider, "boto3", None)
    monkeypatch.setattr(provider, "Config", None)
    with pytest.raises(RuntimeError):
        provider.S3StorageProvider()

    monkeypatch.setattr(provider, "storage", None)
    with pytest.raises(RuntimeError):
        provider.GCSStorageProvider()

    monkeypatch.setattr(provider, "BlobServiceClient", None)
    with pytest.raises(RuntimeError):
        provider.AzureStorageProvider()


def test_local_storage_provider_round_trip(monkeypatch, tmp_path):
    upload_dir = mock_upload_dir(monkeypatch, tmp_path)
    storage = provider.LocalStorageProvider()
    file_content = b"test content"
    filename = "test.txt"

    contents, file_path = storage.upload_file(
        io.BytesIO(file_content), filename, {"source": "test"}
    )

    assert contents == file_content
    assert file_path == str(upload_dir / filename)
    assert (upload_dir / filename).read_bytes() == file_content
    assert storage.get_file(file_path) == file_path

    storage.delete_file(file_path)
    assert not (upload_dir / filename).exists()


def test_local_storage_provider_delete_all(monkeypatch, tmp_path):
    upload_dir = mock_upload_dir(monkeypatch, tmp_path)
    storage = provider.LocalStorageProvider()

    storage.upload_file(io.BytesIO(b"a"), "a.txt", {})
    storage.upload_file(io.BytesIO(b"b"), "b.txt", {})

    assert (upload_dir / "a.txt").exists()
    assert (upload_dir / "b.txt").exists()

    storage.delete_all_files()

    assert not (upload_dir / "a.txt").exists()
    assert not (upload_dir / "b.txt").exists()


def test_local_storage_provider_rejects_empty_content(monkeypatch, tmp_path):
    mock_upload_dir(monkeypatch, tmp_path)
    storage = provider.LocalStorageProvider()

    with pytest.raises(ValueError):
        storage.upload_file(io.BytesIO(), "empty.txt", {})


def test_s3_tag_sanitization():
    assert (
        provider.S3StorageProvider.sanitize_tag_value("a/b+c@x:y z!*")
        == "a/b+c@x:y z"
    )
