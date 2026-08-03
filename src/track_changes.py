"""
Tools for inserting text into ODT documents as tracked changes (LibreOffice's
"Track changes"), instead of silently rewriting the document content.

The document is addressed however the caller finds convenient — a server-side
handle, a URL, a path, or (last resort) a base64 blob; resolution happens in
libremcp.py. This module works on a plain filesystem path and edits it in place.

The `uno` module required to talk to LibreOffice is only available inside
LibreOffice's own bundled Python interpreter, not in this server's venv. So this
module shells out to that interpreter to run scripts/uno/insert_tracked_text.py,
passing parameters via a temp JSON file and reading the result back the same way.
"""
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

LO_PYTHON = os.environ.get("LIBREOFFICE_PYTHON", r"C:\Program Files\LibreOffice\program\python.exe")
_UNO_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "uno" / "insert_tracked_text.py"


class TrackedInsertResult(BaseModel):
    """Result of inserting a tracked-change text into an ODT document"""

    success: bool = Field(description="Whether the insertion succeeded")
    document_base64: Optional[str] = Field(
        default=None, description="Base64-encoded modified .odt file (only when base64 output was requested)"
    )
    result_base64: Optional[str] = Field(
        default=None, description="Base64-encoded modified .odt file (same as document_base64; preferred name)"
    )
    doc_id: Optional[str] = Field(
        default=None, description="Server-side handle for the modified document; pass it as 'doc_id' to other tools"
    )
    download_url: Optional[str] = Field(default=None, description="HTTP URL to download the modified document")
    error: Optional[str] = Field(default=None, description="Error message if it failed")


def insert_tracked_text_in_file(
    doc_path: Path,
    anchor_text: str,
    new_text: str,
    author: Optional[str] = None,
    insert_mode: str = "after",
) -> Optional[str]:
    """Insert text into an .odt file as a tracked change, editing it in place.

    Returns None on success, or an error message.

    Args:
        doc_path: The .odt file to edit. Modified in place.
        anchor_text: Existing text in the document used to locate the insertion point.
        new_text: Text to insert; recorded as a tracked insertion, not merged silently.
        author: "First Last" to attribute the change to (sets LibreOffice's
            user profile before inserting). If omitted, uses whatever author is
            currently configured in LibreOffice.
        insert_mode: "after" (default) inserts right after the anchor text on a new
            line; "before" inserts right before it on its own line.
    """
    if insert_mode not in ("after", "before"):
        return "insert_mode must be 'after' or 'before'"

    doc_path = Path(doc_path)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        params_path = tmp_dir_path / "params.json"
        result_path = tmp_dir_path / "result.json"
        params_path.write_text(
            json.dumps(
                {
                    "path": str(doc_path),
                    "anchor_text": anchor_text,
                    "new_text": new_text,
                    "author": author,
                    "insert_mode": insert_mode,
                }
            ),
            encoding="utf-8",
        )

        proc = subprocess.run(
            [LO_PYTHON, str(_UNO_SCRIPT), str(params_path), str(result_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if not result_path.exists():
            return f"UNO script produced no output. stdout={proc.stdout!r} stderr={proc.stderr!r}"

        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not result["success"]:
            return result.get("error") or "Tracked insertion failed for an unreported reason"

    return None


def insert_tracked_text_impl(
    document_base64: str,
    anchor_text: str,
    new_text: str,
    author: Optional[str] = None,
    insert_mode: str = "after",
) -> TrackedInsertResult:
    """Base64 in, base64 out. Kept for callers that only have the bytes."""
    try:
        document_bytes = base64.b64decode(document_base64, validate=True)
    except Exception as e:
        return TrackedInsertResult(success=False, error=f"Invalid document_base64: {e}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        doc_path = Path(tmp_dir) / "document.odt"
        doc_path.write_bytes(document_bytes)

        error = insert_tracked_text_in_file(doc_path, anchor_text, new_text, author, insert_mode)
        if error:
            return TrackedInsertResult(success=False, error=error)

        modified_b64 = base64.b64encode(doc_path.read_bytes()).decode("ascii")

    return TrackedInsertResult(success=True, document_base64=modified_b64, result_base64=modified_b64)
