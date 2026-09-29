from types import SimpleNamespace
from unittest import mock

import pytest

from conftest import load_function

host = load_function("creative-host")


@pytest.fixture
def bucket(monkeypatch):
    b = mock.Mock()
    monkeypatch.setattr(host, "_bucket", b)
    return b


@pytest.mark.parametrize("path", ["/", "/../secret", "/a/b", "/file id"])
def test_rejects_non_flat_ids(bucket, path):
    assert host.serve_creative(SimpleNamespace(method="GET", path=path))[1] == 404
    bucket.get_blob.assert_not_called()


def test_serves_blob_with_immutable_cache(bucket):
    bucket.get_blob.return_value = mock.Mock(content_type="image/jpeg", download_as_bytes=lambda: b"jpg")
    body, status, headers = host.serve_creative(SimpleNamespace(method="GET", path="/file_00ab-1"))
    assert (body, status, headers["Content-Type"]) == (b"jpg", 200, "image/jpeg")
    assert "immutable" in headers["Cache-Control"]


def test_missing_blob(bucket):
    bucket.get_blob.return_value = None
    assert host.serve_creative(SimpleNamespace(method="GET", path="/file_1"))[1] == 404


def test_get_only(bucket):
    assert host.serve_creative(SimpleNamespace(method="POST", path="/file_1"))[1] == 405
