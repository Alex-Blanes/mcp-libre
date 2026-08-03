"""
Server-side document store: opaque handles instead of base64 blobs.

Moving a document through the MCP channel as base64 costs the client ~1.33 bytes
of context per byte of document, on *every* call of a chain. This store lets a
document be uploaded once (over plain HTTP, see http_routes.py, or fetched from a
URL) and then referred to by a short `doc_id` for the rest of its life, so the
bytes never enter the model's context at all.

Handles live in a workspace directory and expire after a TTL. There is no
background thread: every store/resolve sweeps expired entries first, which is
enough for a store whose entries are only created by user action.
"""

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_DOC_MB = 50

_DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _workspace_root() -> Path:
    configured = os.environ.get("MCP_WORKSPACE")
    root = Path(configured) if configured else Path(tempfile.gettempdir()) / "mcp-libre-workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ttl_seconds() -> int:
    try:
        return int(os.environ.get("MCP_DOC_TTL", DEFAULT_TTL_SECONDS))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def max_doc_bytes() -> int:
    try:
        mb = float(os.environ.get("MCP_MAX_DOC_MB", DEFAULT_MAX_DOC_MB))
    except ValueError:
        mb = DEFAULT_MAX_DOC_MB
    return int(mb * 1024 * 1024)


class DocStoreError(Exception):
    """Base class for document store failures."""


class DocNotFound(DocStoreError):
    """The doc_id is unknown, malformed, or has expired."""


class DocTooLarge(DocStoreError):
    """The document exceeds MCP_MAX_DOC_MB."""


@dataclass
class DocHandle:
    doc_id: str
    filename: str
    size_bytes: int
    created_at: datetime
    path: Path


def _safe_filename(filename: Optional[str], fallback: str = "document.odt") -> str:
    """Reduce an arbitrary client-supplied name to a bare, safe filename."""
    if not filename:
        return fallback
    # Strip any directory component, in both separator conventions, so a name
    # like "../../etc/passwd" or "C:\\windows\\x" can never escape the entry dir.
    name = Path(str(filename).replace("\\", "/")).name
    name = name.strip().lstrip(".")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    return name or fallback


def _entry_dir(doc_id: str) -> Path:
    return _workspace_root() / doc_id


def _read_meta(entry: Path) -> Optional[dict]:
    try:
        return json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_expired(meta: dict, now: Optional[datetime] = None) -> bool:
    ttl = ttl_seconds()
    if ttl <= 0:  # 0 or negative disables expiry
        return False
    now = now or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(meta["created_at"])
    except (KeyError, ValueError):
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() > ttl


def sweep() -> int:
    """Delete expired entries. Returns how many were removed."""
    removed = 0
    root = _workspace_root()
    for entry in root.iterdir():
        if not entry.is_dir() or not _DOC_ID_RE.match(entry.name):
            continue
        meta = _read_meta(entry)
        if meta is None or _is_expired(meta):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def store(data: bytes, filename: Optional[str] = None) -> DocHandle:
    """Persist bytes under a fresh handle."""
    limit = max_doc_bytes()
    if len(data) > limit:
        raise DocTooLarge(f"Document is {len(data)} bytes, over the {limit}-byte limit (MCP_MAX_DOC_MB)")

    sweep()
    doc_id = uuid.uuid4().hex
    name = _safe_filename(filename)
    entry = _entry_dir(doc_id)
    entry.mkdir(parents=True, exist_ok=False)
    target = entry / name
    target.write_bytes(data)

    created_at = datetime.now(timezone.utc)
    (entry / "meta.json").write_text(
        json.dumps({"filename": name, "size_bytes": len(data), "created_at": created_at.isoformat()}),
        encoding="utf-8",
    )
    return DocHandle(doc_id=doc_id, filename=name, size_bytes=len(data), created_at=created_at, path=target)


def store_file(path: Path, filename: Optional[str] = None) -> DocHandle:
    """Persist an existing file's bytes under a fresh handle."""
    path = Path(path)
    return store(path.read_bytes(), filename or path.name)


def get(doc_id: str) -> DocHandle:
    """Look up a handle. Raises DocNotFound if unknown, malformed or expired."""
    if not isinstance(doc_id, str) or not _DOC_ID_RE.match(doc_id):
        raise DocNotFound(f"Invalid doc_id: {doc_id!r}. Expected a 32-character hex handle.")
    sweep()
    entry = _entry_dir(doc_id)
    meta = _read_meta(entry) if entry.is_dir() else None
    if meta is None:
        raise DocNotFound(
            f"Unknown or expired doc_id: {doc_id}. Handles expire after {ttl_seconds()}s; upload the document again."
        )
    target = entry / meta["filename"]
    if not target.exists():
        raise DocNotFound(f"Document content missing for doc_id: {doc_id}")
    created = datetime.fromisoformat(meta["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return DocHandle(
        doc_id=doc_id,
        filename=meta["filename"],
        size_bytes=meta.get("size_bytes", target.stat().st_size),
        created_at=created,
        path=target,
    )


def resolve(doc_id: str) -> Path:
    """Return the filesystem path backing a handle."""
    return get(doc_id).path


def delete(doc_id: str) -> bool:
    """Drop a handle early. Returns False if it wasn't there."""
    if not isinstance(doc_id, str) or not _DOC_ID_RE.match(doc_id):
        raise DocNotFound(f"Invalid doc_id: {doc_id!r}. Expected a 32-character hex handle.")
    entry = _entry_dir(doc_id)
    if not entry.is_dir():
        return False
    shutil.rmtree(entry, ignore_errors=True)
    return True


def list_handles() -> List[DocHandle]:
    """All live handles, oldest first."""
    sweep()
    handles = []
    for entry in _workspace_root().iterdir():
        if not entry.is_dir() or not _DOC_ID_RE.match(entry.name):
            continue
        try:
            handles.append(get(entry.name))
        except DocNotFound:
            continue
    return sorted(handles, key=lambda h: h.created_at)


def public_base_url() -> Optional[str]:
    """Base URL used to build download links, e.g. http://nas:8765."""
    base = os.environ.get("MCP_PUBLIC_URL", "").strip()
    return base.rstrip("/") or None


def download_url(doc_id: str, base_url: Optional[str] = None) -> Optional[str]:
    base = (base_url or public_base_url() or "").rstrip("/")
    return f"{base}/files/{doc_id}" if base else None
