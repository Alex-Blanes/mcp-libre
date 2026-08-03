"""Document store: handles, expiry, and the safety properties that matter."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import docstore


def test_store_and_resolve_round_trip(workspace):
    handle = docstore.store(b"hello", "report.odt")
    assert len(handle.doc_id) == 32
    assert handle.filename == "report.odt"
    assert handle.size_bytes == 5
    assert docstore.resolve(handle.doc_id).read_bytes() == b"hello"


def test_handles_are_distinct(workspace):
    a = docstore.store(b"a", "x.odt")
    b = docstore.store(b"b", "x.odt")
    assert a.doc_id != b.doc_id
    assert docstore.resolve(a.doc_id).read_bytes() == b"a"


def test_delete_removes_the_handle(workspace):
    handle = docstore.store(b"x", "x.odt")
    assert docstore.delete(handle.doc_id) is True
    with pytest.raises(docstore.DocNotFound):
        docstore.resolve(handle.doc_id)


def test_delete_of_unknown_handle_reports_false(workspace):
    assert docstore.delete("b" * 32) is False


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "not-hex-at-all", "A" * 32, "a" * 31])
def test_malformed_doc_ids_are_rejected(workspace, bad):
    with pytest.raises(docstore.DocNotFound):
        docstore.resolve(bad)


@pytest.mark.parametrize(
    "given",
    ["../../../etc/passwd", r"..\..\windows\system32\evil.odt", "/etc/shadow", "C:\\secrets\\x.odt"],
)
def test_filenames_cannot_escape_the_entry_directory(workspace, given):
    handle = docstore.store(b"x", given)
    assert "/" not in handle.filename and "\\" not in handle.filename
    # The file must live inside its own entry dir, nowhere else.
    assert handle.path.parent.name == handle.doc_id
    assert handle.path.resolve().is_relative_to(workspace.resolve())


def test_oversized_documents_are_refused(workspace, monkeypatch):
    monkeypatch.setenv("MCP_MAX_DOC_MB", "0.001")  # ~1 KB
    with pytest.raises(docstore.DocTooLarge):
        docstore.store(b"x" * 5000, "big.odt")


def test_expired_handles_are_swept(workspace, monkeypatch):
    handle = docstore.store(b"x", "x.odt")
    # Backdate the entry rather than sleeping.
    meta_path = handle.path.parent / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    meta_path.write_text(json.dumps(meta))

    monkeypatch.setenv("MCP_DOC_TTL", "60")
    with pytest.raises(docstore.DocNotFound):
        docstore.resolve(handle.doc_id)
    assert not handle.path.parent.exists()


def test_ttl_of_zero_disables_expiry(workspace, monkeypatch):
    handle = docstore.store(b"x", "x.odt")
    meta_path = handle.path.parent / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["created_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    meta_path.write_text(json.dumps(meta))

    monkeypatch.setenv("MCP_DOC_TTL", "0")
    assert docstore.resolve(handle.doc_id).read_bytes() == b"x"


def test_list_handles_is_oldest_first(workspace):
    ids = [docstore.store(b"x", f"{i}.odt").doc_id for i in range(3)]
    assert [h.doc_id for h in docstore.list_handles()] == ids


def test_download_url_needs_a_public_url(workspace, monkeypatch):
    handle = docstore.store(b"x", "x.odt")
    assert docstore.download_url(handle.doc_id) is None
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://nas:8765/")
    assert docstore.download_url(handle.doc_id) == f"http://nas:8765/files/{handle.doc_id}"
