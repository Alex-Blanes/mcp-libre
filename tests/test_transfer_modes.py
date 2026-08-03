"""
_resolve_document / _deliver_result: the four ways in and the four ways out.

These are pure plumbing tests — no LibreOffice needed — because the plumbing is
what decides whether document bytes end up in the model's context.
"""

from pathlib import Path

import httpx
import pytest

import docstore
import libremcp
from libremcp import _deliver_result, _resolve_document


# --- input ---------------------------------------------------------------

def test_resolves_a_server_path(workspace, tmp_path):
    doc = tmp_path / "x.odt"
    doc.write_bytes(b"abc")
    with _resolve_document(path=str(doc)) as resolved:
        assert Path(resolved).read_bytes() == b"abc"


def test_resolves_base64(workspace):
    import base64

    encoded = base64.b64encode(b"abc").decode()
    with _resolve_document(document_base64=encoded) as resolved:
        assert Path(resolved).read_bytes() == b"abc"


def test_resolves_a_doc_id(workspace):
    handle = docstore.store(b"abc", "x.odt")
    with _resolve_document(doc_id=handle.doc_id) as resolved:
        assert Path(resolved).read_bytes() == b"abc"


def test_doc_id_takes_precedence_over_the_other_sources(workspace, tmp_path):
    import base64

    handle = docstore.store(b"from-handle", "x.odt")
    doc = tmp_path / "x.odt"
    doc.write_bytes(b"from-path")
    with _resolve_document(
        path=str(doc), document_base64=base64.b64encode(b"from-b64").decode(), doc_id=handle.doc_id
    ) as resolved:
        assert Path(resolved).read_bytes() == b"from-handle"


def test_reading_a_handle_hands_back_the_stored_file(workspace):
    handle = docstore.store(b"abc", "x.odt")
    with _resolve_document(doc_id=handle.doc_id) as resolved:
        assert Path(resolved) == handle.path


def test_writing_to_a_handle_gets_a_scratch_copy(workspace):
    """A mutating tool must not corrupt the caller's stored original."""
    handle = docstore.store(b"original", "x.odt")
    with _resolve_document(doc_id=handle.doc_id, for_writing=True) as resolved:
        assert Path(resolved) != handle.path
        Path(resolved).write_bytes(b"modified")
    assert handle.path.read_bytes() == b"original"


def test_no_source_explains_the_options(workspace):
    with pytest.raises(ValueError, match="doc_id"):
        with _resolve_document():
            pass


def test_missing_path_points_at_the_better_options(workspace):
    with pytest.raises(FileNotFoundError, match="doc_id"):
        with _resolve_document(path="/nowhere/x.odt"):
            pass


def test_windows_path_on_a_posix_server_is_explained(workspace, monkeypatch):
    monkeypatch.setattr(libremcp, "_SERVER_OS", "Linux")
    with pytest.raises(FileNotFoundError, match="Windows-style"):
        with _resolve_document(path=r"C:\Users\user\x.odt"):
            pass


def test_resolves_a_url(workspace, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "files.example")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"remote bytes"))

    def stream(method, url, **kwargs):
        kwargs.pop("follow_redirects", None)
        return httpx.Client(transport=transport).stream(method, url, **kwargs)

    monkeypatch.setattr(httpx, "stream", stream)
    with _resolve_document(document_url="https://files.example/x.odt") as resolved:
        assert Path(resolved).read_bytes() == b"remote bytes"


# --- output --------------------------------------------------------------

@pytest.fixture
def produced(tmp_path):
    path = tmp_path / "result.odt"
    path.write_bytes(b"result bytes")
    return path


def test_default_delivery_is_a_handle_not_base64(workspace, produced):
    """The whole point: no document bytes in the tool result by default."""
    delivery = _deliver_result(produced)
    assert "result_base64" not in delivery
    assert docstore.resolve(delivery["doc_id"]).read_bytes() == b"result bytes"


def test_default_delivery_includes_a_url_when_public_url_is_set(workspace, produced, monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://my-server:8765")
    delivery = _deliver_result(produced)
    assert delivery["download_url"] == f"http://my-server:8765/files/{delivery['doc_id']}"


def test_target_path_wins_and_writes_to_disk(workspace, produced, tmp_path):
    target = tmp_path / "out" / "final.odt"
    delivery = _deliver_result(produced, target_path=str(target), return_base64=True)
    assert delivery == {"path": str(target.absolute())}
    assert target.read_bytes() == b"result bytes"


def test_target_url_wins_over_base64(workspace, produced, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "nextcloud.example")
    sent = {}

    def put(url, **kwargs):
        sent["url"] = url
        sent["content"] = kwargs.get("content")
        return httpx.Response(201, request=httpx.Request("PUT", url))

    monkeypatch.setattr(httpx, "put", put)
    delivery = _deliver_result(produced, target_url="https://nextcloud.example/x.odt", return_base64=True)
    assert delivery == {"download_url": "https://nextcloud.example/x.odt"}
    assert sent["content"] == b"result bytes"


def test_base64_is_still_available_on_request(workspace, produced):
    import base64

    delivery = _deliver_result(produced, return_base64=True)
    assert base64.b64decode(delivery["result_base64"]) == b"result bytes"
    assert "doc_id" not in delivery
