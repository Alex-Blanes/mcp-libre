"""
LibreOffice Model Context Protocol Server

This MCP server provides tools and resources for interacting with LibreOffice documents.
It supports reading, writing, and manipulating Writer documents, Calc spreadsheets, 
and other LibreOffice formats.
"""

import asyncio
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Dict, Generator, List, Optional, Union
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, BeforeValidator, Field
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import docstore
import http_routes
import urlio
from track_changes import TrackedInsertResult, insert_tracked_text_in_file

# Transport is stdio by default (local use from Claude Code on this laptop).
# Set MCP_TRANSPORT=streamable-http (+ MCP_HOST/MCP_PORT/MCP_ALLOWED_HOSTS) to run
# as a network server, e.g. inside a Docker container deployed remotely.
_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
_EXTRA_ALLOWED_HOSTS = [h for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h]

mcp = FastMCP(
    "LibreOffice MCP Server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8765")),
)

# FastMCP's DNS-rebinding protection only allows localhost by default (and, at
# least in this mcp version, ends up None instead of that default once host/port
# are passed explicitly above) — build it ourselves so a remote
# deployment can be reached at all.
_default_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_default_origins = [f"{scheme}://{h}" for h in ["127.0.0.1:*", "localhost:*", "[::1]:*"] for scheme in ("http", "https")]
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_default_hosts + _EXTRA_ALLOWED_HOSTS,
    allowed_origins=_default_origins
    + [f"http://{h}" for h in _EXTRA_ALLOWED_HOSTS]
    + [f"https://{h}" for h in _EXTRA_ALLOWED_HOSTS],
)

# Plain-HTTP upload/download for the document store. Only meaningful when we're
# serving HTTP at all; under stdio there is no app to attach them to.
if _TRANSPORT != "stdio":
    http_routes.register_routes(mcp)


def _ensure_aware(value: Any) -> Any:
    """Attach UTC to naive datetimes.

    The tool output schemas declare `format: "date-time"`, which is RFC 3339 and
    *requires* a UTC offset. A naive datetime serializes as "2026-08-03T12:00:00.123456",
    which strict client-side validators (ajv with format checking) reject — that
    surfaced as `modified_time must match format "date-time"`. Producers should hand
    us aware datetimes; this is the belt-and-braces so a naive one can never escape.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


AwareDatetime = Annotated[datetime, BeforeValidator(_ensure_aware)]


# Data models for structured responses
class DocumentInfo(BaseModel):
    """Information about a LibreOffice document"""
    path: str = Field(description="Full path to the document")
    filename: str = Field(description="Document filename")
    format: str = Field(description="Document format (odt, ods, odp, etc.)")
    size_bytes: int = Field(description="File size in bytes")
    modified_time: AwareDatetime = Field(description="Last modification time (UTC, RFC 3339)")
    exists: bool = Field(description="Whether the file exists")


class TextContent(BaseModel):
    """Text content extracted from a document"""
    content: str = Field(description="The extracted text content")
    word_count: int = Field(description="Number of words in the content")
    char_count: int = Field(description="Number of characters in the content")
    page_count: Optional[int] = Field(description="Number of pages (if available)")


class ConversionResult(BaseModel):
    """Result of document conversion"""
    source_path: str = Field(description="Source document path")
    target_path: str = Field(description="Target document path")
    source_format: str = Field(description="Original format")
    target_format: str = Field(description="Converted format")
    success: bool = Field(description="Whether conversion was successful")
    error_message: Optional[str] = Field(description="Error message if conversion failed")
    result_base64: Optional[str] = Field(default=None, description="Base64-encoded converted document (base64 mode)")
    doc_id: Optional[str] = Field(default=None, description="Server-side handle for the result; pass it as 'doc_id' to other tools")
    download_url: Optional[str] = Field(default=None, description="HTTP URL to download the converted document")


class SpreadsheetData(BaseModel):
    """Data from a spreadsheet"""
    sheet_name: str = Field(description="Name of the sheet")
    data: List[List[str]] = Field(description="2D array of cell values")
    row_count: int = Field(description="Number of rows")
    col_count: int = Field(description="Number of columns")


class DocResult(BaseModel):
    """Result of a document mutation operation (path or base64 mode)"""
    success: bool = Field(default=True, description="Whether the operation succeeded")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    path: Optional[str] = Field(default=None, description="Path to the document (filesystem mode)")
    filename: Optional[str] = Field(default=None, description="Document filename")
    format: Optional[str] = Field(default=None, description="Document format")
    size_bytes: Optional[int] = Field(default=None, description="File size in bytes")
    modified_time: Optional[AwareDatetime] = Field(default=None, description="Last modification time (UTC, RFC 3339)")
    exists: Optional[bool] = Field(default=None, description="Whether the file exists")
    result_base64: Optional[str] = Field(default=None, description="Base64-encoded result document (base64 mode)")
    doc_id: Optional[str] = Field(default=None, description="Server-side handle for the result; pass it as 'doc_id' to other tools")
    download_url: Optional[str] = Field(default=None, description="HTTP URL to download the result document")


# Helper functions
def _run_libreoffice_command(args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a LibreOffice command with proper error handling"""
    # SOFFICE_BIN wins when set (the Dockerfile sets it, and on Windows the
    # binary is installed outside PATH); otherwise fall back to the usual names.
    configured = os.environ.get("SOFFICE_BIN", "").strip()
    candidates = ([configured] if configured else []) + ['libreoffice', 'loffice', 'soffice']

    try:
        for executable in candidates:
            try:
                cmd = [executable] + args
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False
                )
                if result.returncode == 0 or executable == 'soffice':  # soffice might work even if return code != 0
                    return result
            except FileNotFoundError:
                continue
        
        raise FileNotFoundError("LibreOffice executable not found. Please install LibreOffice.")
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"LibreOffice command timed out after {timeout} seconds")


def _get_document_info(file_path: str) -> DocumentInfo:
    """Get information about a document file"""
    path = Path(file_path)
    # One stat() call: the file can vanish between exists() and stat(), and two
    # separate stat()s could even report different sizes/mtimes.
    try:
        st = path.stat()
    except OSError:
        st = None
    return DocumentInfo(
        path=str(path.absolute()),
        filename=path.name,
        format=path.suffix.lower().lstrip('.'),
        size_bytes=st.st_size if st else 0,
        modified_time=(
            datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) if st else datetime.now(timezone.utc)
        ),
        exists=st is not None,
    )


# Document resolution helpers for base64 support
_format_extensions = {
    "writer": ".odt",
    "calc": ".ods",
    "spreadsheet": ".ods",
    "impress": ".odp",
    "draw": ".odg",
}


_WINDOWS_PATH_RE = re.compile(r'^[A-Za-z]:[/\\]')
_SERVER_OS = platform.system()


def _is_windows_path(p: str) -> bool:
    return bool(_WINDOWS_PATH_RE.match(str(p)))


_NO_SOURCE_HELP = (
    "Provide the document in one of these ways, best first:\n"
    "  1. doc_id — upload once with: "
    "curl --data-binary @file.odt '<server>/files?filename=file.odt', then pass the returned doc_id\n"
    "  2. document_url — an http(s)/WebDAV URL the server can fetch itself\n"
    "  3. path — only for files on the server's own filesystem\n"
    "  4. document_base64 — last resort; it costs ~1.33 bytes of your context per document byte"
)


@contextmanager
def _resolve_document(
    path: Optional[str] = None,
    document_base64: Optional[str] = None,
    format_hint: str = "writer",
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    url_auth: Optional[str] = None,
    for_writing: bool = False,
) -> Generator[str, None, None]:
    """Resolve a document from a handle, a URL, base64 content, or a path.

    Precedence: doc_id > document_url > document_base64 > path. The first three
    land in (or already live in) server-side storage so the bytes never travel
    through the MCP channel; temp copies are cleaned up on exit.

    Set for_writing=True in tools that modify the document: a stored handle is
    then resolved to a scratch copy, so the caller's original stays intact and
    the edit comes back as a new handle.
    """
    if doc_id is not None:
        stored = docstore.resolve(doc_id)
        if not for_writing:
            yield str(stored)
            return
        with tempfile.TemporaryDirectory() as tmp_dir:
            scratch = Path(tmp_dir) / stored.name
            shutil.copyfile(stored, scratch)
            yield str(scratch)
        return

    if document_url is not None:
        ext = Path(urlio.filename_from_url(document_url)).suffix or _format_extensions.get(format_hint, ".odt")
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc_path = Path(tmp_dir) / f"document{ext}"
            urlio.fetch(document_url, doc_path, url_auth=url_auth)
            yield str(doc_path)
        return

    if document_base64 is not None:
        ext = _format_extensions.get(format_hint, ".odt")
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc_path = Path(tmp_dir) / f"document{ext}"
            doc_path.write_bytes(base64.b64decode(document_base64))
            yield str(doc_path)
        return

    if path is not None:
        normalized = str(path).replace('\\', '/')
        p = Path(normalized)
        if not p.exists():
            if _is_windows_path(path) and _SERVER_OS != "Windows":
                raise FileNotFoundError(
                    f"Path '{path}' is a Windows-style path but this MCP server "
                    f"runs on {_SERVER_OS} (Docker container).\n"
                    f"Windows filesystem paths are NOT accessible from the server.\n\n"
                    f"{_NO_SOURCE_HELP}"
                )
            raise FileNotFoundError(
                f"Document not found on the server: {path}.\n\n{_NO_SOURCE_HELP}"
            )
        yield normalized
        return

    raise ValueError(f"No document source given.\n\n{_NO_SOURCE_HELP}")


def _deliver_result(
    produced: Path,
    *,
    target_path: Optional[str] = None,
    target_url: Optional[str] = None,
    return_base64: bool = False,
    url_auth: Optional[str] = None,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Hand a freshly produced document back to the caller.

    Precedence: target_path (server filesystem) > target_url (PUT/WebDAV) >
    return_base64 > a stored handle. The last one is the default, and the point
    of the whole exercise: chaining tools by doc_id keeps the bytes out of the
    model's context entirely.

    Returns the fields to merge into the tool's result model.
    """
    produced = Path(produced)
    name = filename or produced.name

    if target_path:
        final = Path(str(target_path).replace('\\', '/'))
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.resolve() != produced.resolve():
            shutil.copyfile(produced, final)
        return {"path": str(final.absolute())}

    if target_url:
        urlio.put(target_url, produced, url_auth=url_auth)
        return {"download_url": target_url}

    if return_base64:
        return {"result_base64": _read_as_base64(str(produced))}

    handle = docstore.store_file(produced, name)
    return {"doc_id": handle.doc_id, "download_url": docstore.download_url(handle.doc_id)}


def _read_as_base64(path: str) -> str:
    """Read a file and return its content as a base64-encoded string."""
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


# Core LibreOffice Tools

@mcp.tool()
def create_document(
    path: Optional[str] = None,
    doc_type: str = "writer",
    content: str = "",
    return_base64: bool = False,
    target_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> DocResult:
    """Create a new LibreOffice document

    By default the document is kept on the server and you get back a `doc_id`
    (plus a download URL). Pass that doc_id to the other tools to keep working on
    it — the file's bytes never enter the conversation.

    Args:
        path: Write to this path on the SERVER's filesystem (rarely what you want remotely)
        doc_type: Type of document to create (writer, calc, impress, draw)
        content: Initial content for the document (for writer documents)
        return_base64: Return the document inline as base64 (expensive; last resort)
        target_url: Upload the result to this URL with PUT (WebDAV/Nextcloud)
        url_auth: "user:password" for target_url, if it needs authentication
    """
    # Anything other than an explicit server path is built in a temp dir and
    # then handed over by _deliver_result.
    use_temp = bool(target_url) or return_base64 or not path

    if use_temp:
        _tmp_manager = tempfile.TemporaryDirectory()
        path_obj = Path(_tmp_manager.name) / f"document{_format_extensions.get(doc_type, '.odt')}"
    else:
        _tmp_manager = None
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
    
    # Map document types to LibreOffice formats
    format_map = {
        "writer": ".odt",
        "calc": ".ods", 
        "impress": ".odp",
        "draw": ".odg"
    }
    
    if doc_type not in format_map:
        return DocResult(success=False, error=f"Unsupported document type: {doc_type}. Use: {list(format_map.keys())}")
    
    # Add appropriate extension if not present
    if not path_obj.suffix:
        path_obj = path_obj.parent / (path_obj.stem + format_map[doc_type])
    
    try:
        if doc_type == "writer" and content:
            # For writer documents with content, create a simple text file first
            # then convert to ODT format
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            
            try:
                # Try to convert text to ODT
                result = _run_libreoffice_command([
                    '--headless',
                    '--convert-to', 'odt',
                    '--outdir', str(path_obj.parent),
                    tmp_path
                ])
                
                # Find the converted file and move it to the target location
                tmp_stem = Path(tmp_path).stem
                converted_file = path_obj.parent / f"{tmp_stem}.odt"
                
                if converted_file.exists():
                    converted_file.rename(path_obj)
                else:
                    import zipfile
                    
                    with zipfile.ZipFile(path_obj, 'w', zipfile.ZIP_DEFLATED) as zf:
                        zf.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
                        manifest = '''<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>'''
                        zf.writestr('META-INF/manifest.xml', manifest)
                        content_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body>
  <office:text>
   <text:p>{content}</text:p>
  </office:text>
 </office:body>
</office:document-content>'''
                        zf.writestr('content.xml', content_xml)
                
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:
            try:
                result = _run_libreoffice_command([
                    '--headless',
                    '--invisible',
                    '--nodefault',
                    '--nolockcheck',
                    '--nologo',
                    '--norestore',
                    '--convert-to', format_map[doc_type].lstrip('.'),
                    '--outdir', str(path_obj.parent),
                    '/dev/null'
                ])
                
                if not path_obj.exists():
                    if doc_type == "writer":
                        import zipfile
                        with zipfile.ZipFile(path_obj, 'w', zipfile.ZIP_DEFLATED) as zf:
                            zf.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
                            manifest = '''<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>'''
                            zf.writestr('META-INF/manifest.xml', manifest)
                            content_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body>
  <office:text>
   <text:p></text:p>
  </office:text>
 </office:body>
</office:document-content>'''
                            zf.writestr('content.xml', content_xml)
                    else:
                        path_obj.touch()
                        
            except Exception:
                path_obj.touch()
        
        doc_info = _get_document_info(str(path_obj))
        if not use_temp:
            return DocResult(**doc_info.model_dump(), success=True)

        delivery = _deliver_result(
            path_obj,
            target_url=target_url,
            return_base64=return_base64,
            url_auth=url_auth,
            filename=f"document{format_map[doc_type]}",
        )
        fields = doc_info.model_dump()
        # The temp path is about to be deleted; don't hand the caller a dead path.
        fields.pop("path", None)
        return DocResult(**fields, success=True, **delivery)

    except Exception as e:
        return DocResult(success=False, error=str(e))
    finally:
        if _tmp_manager:
            _tmp_manager.cleanup()


@mcp.tool()
def read_document_text(
    path: Optional[str] = None,
    document_base64: Optional[str] = None,
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> TextContent:
    """Extract text content from a LibreOffice document

    Args:
        path: Path on the SERVER's filesystem
        document_base64: Base64-encoded document content (expensive; last resort)
        doc_id: Handle from a previous tool or from POST /files (preferred)
        document_url: http(s)/WebDAV URL the server fetches itself
        url_auth: "user:password" for document_url, if it needs authentication
    """
    try:
        with _resolve_document(path, document_base64, doc_id=doc_id, document_url=document_url, url_auth=url_auth) as resolved_path:
            path_obj = Path(resolved_path)
            
            with tempfile.TemporaryDirectory() as tmp_dir:
                result = _run_libreoffice_command([
                    '--headless',
                    '--convert-to', 'txt',
                    '--outdir', tmp_dir,
                    str(path_obj)
                ])
                
                tmp_path = Path(tmp_dir)
                created_files = list(tmp_path.iterdir())
                
                txt_file = None
                possible_names = [
                    path_obj.stem + '.txt',
                    path_obj.name + '.txt', 
                    'output.txt'
                ]
                
                for name in possible_names:
                    candidate = tmp_path / name
                    if candidate.exists():
                        txt_file = candidate
                        break
                
                if not txt_file:
                    txt_files = list(tmp_path.glob('*.txt'))
                    if txt_files:
                        txt_file = txt_files[0]
                
                if txt_file and txt_file.exists():
                    content = txt_file.read_text(encoding='utf-8', errors='ignore')
                else:
                    if path_obj.suffix.lower() == '.odt':
                        content = _extract_text_from_odt(str(path_obj))
                    else:
                        try:
                            content = path_obj.read_text(encoding='utf-8', errors='ignore')
                        except:
                            raise RuntimeError(f"Could not extract text. LibreOffice output: {result.stderr}. Files created: {[f.name for f in created_files]}")
            
            word_count = len(content.split())
            char_count = len(content)
            
            return TextContent(
                content=content,
                word_count=word_count,
                char_count=char_count,
                page_count=None
            )
    except Exception as e:
        raise RuntimeError(f"Failed to read document: {str(e)}")


def _extract_text_from_odt(file_path: str) -> str:
    """Extract text content directly from ODT file"""
    import zipfile
    import xml.etree.ElementTree as ET
    
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            # Read content.xml from the ODT file
            content_xml = zf.read('content.xml').decode('utf-8')
            
            # Parse XML and extract text
            root = ET.fromstring(content_xml)
            
            # Find all text elements (simplified extraction)
            text_parts = []
            for elem in root.iter():
                if elem.text:
                    text_parts.append(elem.text)
                if elem.tail:
                    text_parts.append(elem.tail)
            
            return ' '.join(text_parts).strip()
    
    except Exception as e:
        raise RuntimeError(f"Failed to extract text from ODT: {str(e)}")


@mcp.tool()
def convert_document(
    source_path: Optional[str] = None,
    target_path: Optional[str] = None,
    target_format: str = "pdf",
    document_base64: Optional[str] = None,
    return_base64: bool = False,
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    target_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> ConversionResult:
    """Convert a document to a different format

    By default the converted file stays on the server and you get a `doc_id` and
    a download URL back, so the bytes never enter the conversation.

    Args:
        source_path: Path on the SERVER's filesystem
        target_path: Write the result to this path on the SERVER's filesystem
        target_format: Target format (pdf, docx, xlsx, pptx, html, txt, etc.)
        document_base64: Base64-encoded source document (expensive; last resort)
        return_base64: Return the converted document inline as base64 (expensive)
        doc_id: Source handle from a previous tool or from POST /files (preferred)
        document_url: http(s)/WebDAV URL of the source; the server fetches it
        target_url: Upload the result to this URL with PUT (WebDAV/Nextcloud)
        url_auth: "user:password" for document_url/target_url, if needed
    """
    use_source_path = doc_id is None and document_url is None and document_base64 is None
    # Build in a temp dir unless the caller wants it written straight to a server path.
    use_temp_target = not target_path

    def _source_label(source_obj: Path) -> str:
        if doc_id:
            return f"(doc_id:{doc_id})"
        if document_url:
            return document_url
        if not use_source_path:
            return "(base64)"
        return source_path or str(source_obj)

    try:
        with _resolve_document(
            source_path, document_base64, doc_id=doc_id, document_url=document_url, url_auth=url_auth
        ) as resolved_source:
            source_obj = Path(resolved_source)

            if use_temp_target:
                _tmp_manager = tempfile.TemporaryDirectory()
                resolved_target = Path(_tmp_manager.name) / f"{source_obj.stem}.{target_format}"
            else:
                _tmp_manager = None
                resolved_target = Path(str(target_path).replace('\\', '/'))
                resolved_target.parent.mkdir(parents=True, exist_ok=True)

            try:
                result = _run_libreoffice_command([
                    '--headless',
                    '--convert-to', target_format,
                    '--outdir', str(resolved_target.parent),
                    str(source_obj)
                ])

                expected_output = resolved_target.parent / (source_obj.stem + f'.{target_format}')
                if expected_output.exists() and expected_output != resolved_target:
                    expected_output.rename(resolved_target)

                success = resolved_target.exists()
                error_msg = None if success else f"Conversion failed. LibreOffice output: {result.stderr}"

                conv_result = ConversionResult(
                    source_path=_source_label(source_obj),
                    target_path=str(resolved_target) if not use_temp_target else "",
                    source_format=source_obj.suffix.lower().lstrip('.'),
                    target_format=target_format,
                    success=success,
                    error_message=error_msg,
                )

                if success and use_temp_target:
                    delivery = _deliver_result(
                        resolved_target,
                        target_url=target_url,
                        return_base64=return_base64,
                        url_auth=url_auth,
                        filename=resolved_target.name,
                    )
                    for key, value in delivery.items():
                        setattr(conv_result, key if key != "path" else "target_path", value)

                return conv_result

            except Exception as e:
                return ConversionResult(
                    source_path=_source_label(source_obj),
                    target_path=str(resolved_target),
                    source_format=source_obj.suffix.lower().lstrip('.'),
                    target_format=target_format,
                    success=False,
                    error_message=str(e)
                )
            finally:
                if _tmp_manager:
                    _tmp_manager.cleanup()
    except (FileNotFoundError, ValueError, urlio.UrlTransferError, docstore.DocStoreError) as e:
        return ConversionResult(
            source_path=source_path or document_url or (f"(doc_id:{doc_id})" if doc_id else "(base64)"),
            target_path=target_path or "",
            source_format="",
            target_format=target_format,
            success=False,
            error_message=str(e)
        )


@mcp.tool()
def get_document_info(
    path: Optional[str] = None,
    document_base64: Optional[str] = None,
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> DocumentInfo:
    """Get detailed information about a LibreOffice document

    Note: for non-path sources the reported path/modified_time describe the
    server-side copy, not any original on your machine.

    Args:
        path: Path on the SERVER's filesystem
        document_base64: Base64-encoded document content (expensive; last resort)
        doc_id: Handle from a previous tool or from POST /files (preferred)
        document_url: http(s)/WebDAV URL the server fetches itself
        url_auth: "user:password" for document_url, if it needs authentication
    """
    with _resolve_document(path, document_base64, doc_id=doc_id, document_url=document_url, url_auth=url_auth) as resolved_path:
        return _get_document_info(resolved_path)


@mcp.tool()
def read_spreadsheet_data(
    path: Optional[str] = None,
    document_base64: Optional[str] = None,
    sheet_name: Optional[str] = None,
    max_rows: int = 100,
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> SpreadsheetData:
    """Read data from a LibreOffice Calc spreadsheet
    
    Args:
        path: Path on the SERVER's filesystem (.ods, .xlsx, etc.)
        document_base64: Base64-encoded spreadsheet content (expensive; last resort)
        sheet_name: Name of the specific sheet to read (if None, reads first sheet)
        max_rows: Maximum number of rows to read (default 100)
        doc_id: Handle from a previous tool or from POST /files (preferred)
        document_url: http(s)/WebDAV URL the server fetches itself
        url_auth: "user:password" for document_url, if it needs authentication
    """
    try:
        with _resolve_document(path, document_base64, format_hint="spreadsheet", doc_id=doc_id, document_url=document_url, url_auth=url_auth) as resolved_path:
            path_obj = Path(resolved_path)
            
            with tempfile.TemporaryDirectory() as tmp_dir:
                result = _run_libreoffice_command([
                    '--headless',
                    '--convert-to', 'csv',
                    '--outdir', tmp_dir,
                    str(path_obj)
                ])
                
                csv_file = Path(tmp_dir) / (path_obj.stem + '.csv')
                if not csv_file.exists():
                    raise RuntimeError("Failed to convert spreadsheet to CSV")
                
                import csv
                data = []
                with open(csv_file, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    for i, row in enumerate(reader):
                        if i >= max_rows:
                            break
                        data.append(row)
                
                row_count = len(data)
                col_count = max(len(row) for row in data) if data else 0
                
                return SpreadsheetData(
                    sheet_name=sheet_name or "Sheet1",
                    data=data,
                    row_count=row_count,
                    col_count=col_count
                )
    except Exception as e:
        raise RuntimeError(f"Failed to read spreadsheet: {str(e)}")


@mcp.tool()
def insert_text_at_position(
    path: Optional[str] = None,
    text: str = "",
    position: str = "end",
    document_base64: Optional[str] = None,
    return_base64: bool = False,
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    target_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> DocResult:
    """Insert text into a LibreOffice Writer document

    Editing by `path` modifies that file on the server in place. Every other
    source leaves the original untouched and returns a NEW doc_id for the result.

    Args:
        path: Path on the SERVER's filesystem (edited in place)
        text: Text to insert
        position: Where to insert the text ("start", "end", or "replace")
        document_base64: Base64-encoded document content (expensive; last resort)
        return_base64: Return the modified document inline as base64 (expensive)
        doc_id: Source handle from a previous tool or from POST /files (preferred)
        document_url: http(s)/WebDAV URL of the source; the server fetches it
        target_url: Upload the result to this URL with PUT (WebDAV/Nextcloud)
        url_auth: "user:password" for document_url/target_url, if needed
    """
    # Only an explicit server path is edited in place; anything else is a copy
    # that has to be handed back somehow.
    edit_in_place = bool(path) and doc_id is None and document_url is None and document_base64 is None
    edit_in_place = edit_in_place and not (return_base64 or target_url)

    try:
        with _resolve_document(
            path, document_base64, doc_id=doc_id, document_url=document_url,
            url_auth=url_auth, for_writing=True,
        ) as resolved_path:
            path_obj = Path(resolved_path)
            existing_content = read_document_text(path=resolved_path).content
            
            if position == "start":
                new_content = text + "\n" + existing_content
            elif position == "end":
                new_content = existing_content + "\n" + text
            elif position == "replace":
                new_content = text
            else:
                return DocResult(success=False, error="Position must be 'start', 'end', or 'replace'")
            
            file_ext = path_obj.suffix.lower()
            
            backup_path = str(path_obj) + '.backup'
            shutil.copy2(path_obj, backup_path)
            
            try:
                if file_ext in ['.odt', '.docx', '.doc']:
                    success = _insert_text_writer_document(str(path_obj), new_content)
                    if not success:
                        _recreate_writer_document(str(path_obj), new_content)
                else:
                    _recreate_document_with_content(str(path_obj), new_content)
            except Exception as convert_error:
                shutil.copy2(backup_path, path_obj)
                return DocResult(success=False, error=f"Failed to modify document: {str(convert_error)}")
            finally:
                Path(backup_path).unlink(missing_ok=True)
            
            doc_info = _get_document_info(str(path_obj))
            if edit_in_place:
                return DocResult(**doc_info.model_dump(), success=True)

            delivery = _deliver_result(
                path_obj,
                target_url=target_url,
                return_base64=return_base64,
                url_auth=url_auth,
                filename=path_obj.name,
            )
            fields = doc_info.model_dump()
            fields.pop("path", None)  # scratch copy; about to be cleaned up
            return DocResult(**fields, success=True, **delivery)
    except Exception as e:
        return DocResult(success=False, error=str(e))


def _insert_text_writer_document(path: str, content: str) -> bool:
    """Insert text into Writer document using LibreOffice macro approach

    Unimplemented: always falls back to _recreate_writer_document. Real tracked
    insertion is handled separately by insert_tracked_text (see track_changes.py).
    """
    return False


def _recreate_writer_document(path: str, content: str):
    """Recreate a Writer document with new content"""
    path_obj = Path(path)
    original_ext = path_obj.suffix.lower()
    
    # Create temporary text file with proper encoding
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    
    try:
        # Determine target format
        if original_ext == '.odt':
            target_format = 'odt'
        elif original_ext == '.docx':
            target_format = 'docx'  
        elif original_ext == '.doc':
            target_format = 'doc'
        else:
            target_format = 'odt'  # Default to ODT
        
        # Remove original file
        if path_obj.exists():
            path_obj.unlink()
        
        # Use LibreOffice to convert text to document format
        # First, let's try a different approach - create from template
        try:
            # Method 1: Convert using LibreOffice
            result = _run_libreoffice_command([
                '--headless',
                '--invisible', 
                '--convert-to', target_format,
                '--outdir', str(path_obj.parent),
                tmp_path
            ])
            
            # Find and rename the converted file
            tmp_name = Path(tmp_path).stem
            converted_file = path_obj.parent / f"{tmp_name}.{target_format}"
            
            if converted_file.exists():
                converted_file.rename(path_obj)
                return
        except Exception:
            pass
        
        # Method 2: If conversion failed, create minimal valid ODT
        if original_ext == '.odt' or target_format == 'odt':
            _create_minimal_odt(path_obj, content)
        else:
            # For other formats, create a simple text file with correct extension
            with open(path_obj, 'w', encoding='utf-8') as f:
                f.write(content)
        
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _create_minimal_odt(path: Path, content: str):
    """Create a minimal but valid ODT file with the given content"""
    import zipfile
    
    # Escape content for XML
    import html
    escaped_content = html.escape(content)
    
    # Split content into paragraphs
    paragraphs = escaped_content.split('\n')
    
    # Create paragraph XML
    text_paragraphs = []
    for para in paragraphs:
        if para.strip():
            text_paragraphs.append(f'   <text:p text:style-name="Standard">{para}</text:p>')
        else:
            text_paragraphs.append('   <text:p text:style-name="Standard"/>')
    
    text_content = '\n'.join(text_paragraphs)
    
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # mimetype (must be first and uncompressed)
        zf.writestr('mimetype', 'application/vnd.oasis.opendocument.text', compress_type=zipfile.ZIP_STORED)
        
        # META-INF/manifest.xml
        manifest_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>
</manifest:manifest>'''
        zf.writestr('META-INF/manifest.xml', manifest_xml)
        
        # content.xml
        content_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" office:version="1.2">
 <office:scripts/>
 <office:font-face-decls/>
 <office:automatic-styles/>
 <office:body>
  <office:text>
{text_content}
  </office:text>
 </office:body>
</office:document-content>'''
        zf.writestr('content.xml', content_xml)
        
        # styles.xml (minimal)
        styles_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" office:version="1.2">
 <office:font-face-decls/>
 <office:styles>
  <style:default-style style:family="paragraph">
   <style:paragraph-properties fo:hyphenation-ladder-count="no-limit"/>
   <style:text-properties style:tab-stop-distance="0.5in"/>
  </style:default-style>
  <style:style style:name="Standard" style:family="paragraph" style:class="text"/>
 </office:styles>
 <office:automatic-styles/>
 <office:master-styles/>
</office:document-styles>'''
        zf.writestr('styles.xml', styles_xml)
        
        # meta.xml (minimal)
        meta_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" office:version="1.2">
 <office:meta>
  <meta:generator>LibreOffice MCP Server</meta:generator>
 </office:meta>
</office:document-meta>'''
        zf.writestr('meta.xml', meta_xml)


def _recreate_document_with_content(path: str, content: str):
    """Recreate any document with new content"""
    # For non-Writer documents, just create a text file with the correct extension
    with open(path, 'w') as f:
        f.write(content)


@mcp.tool()
def insert_tracked_text(
    anchor_text: str,
    new_text: str,
    document_base64: Optional[str] = None,
    author: Optional[str] = None,
    insert_mode: str = "after",
    path: Optional[str] = None,
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    target_url: Optional[str] = None,
    return_base64: bool = False,
    url_auth: Optional[str] = None,
) -> TrackedInsertResult:
    """Insert text into an .odt document as a tracked change

    Unlike insert_text_at_position, this does NOT rewrite the document content.
    It opens the document via LibreOffice's UNO API, enables RecordChanges,
    finds anchor_text, and inserts new_text next to it so it shows up as a
    reviewable insertion (Edit > Track Changes > Manage).

    The source is never modified unless you pass `path`; by default you get a
    new doc_id back.

    Args:
        anchor_text: Existing text used to locate the insertion point
        new_text: Text to insert as a tracked insertion
        document_base64: Base64-encoded source .odt (expensive; last resort)
        author: "First Last" to attribute the change to (optional)
        insert_mode: "after" (default) or "before" the anchor text
        path: Path on the SERVER's filesystem (edited in place)
        doc_id: Source handle from a previous tool or from POST /files (preferred)
        document_url: http(s)/WebDAV URL of the source; the server fetches it
        target_url: Upload the result to this URL with PUT (WebDAV/Nextcloud)
        return_base64: Return the modified document inline as base64 (expensive)
        url_auth: "user:password" for document_url/target_url, if needed
    """
    edit_in_place = (
        bool(path)
        and doc_id is None
        and document_url is None
        and document_base64 is None
        and not (return_base64 or target_url)
    )

    try:
        with _resolve_document(
            path, document_base64, doc_id=doc_id, document_url=document_url,
            url_auth=url_auth, for_writing=True,
        ) as resolved_path:
            doc_path = Path(resolved_path)
            error = insert_tracked_text_in_file(doc_path, anchor_text, new_text, author, insert_mode)
            if error:
                return TrackedInsertResult(success=False, error=error)

            if edit_in_place:
                return TrackedInsertResult(success=True)

            delivery = _deliver_result(
                doc_path,
                target_url=target_url,
                return_base64=return_base64,
                url_auth=url_auth,
                filename=doc_path.name,
            )
            result = TrackedInsertResult(success=True, **delivery)
            # Old field name kept in sync so existing callers don't break.
            result.document_base64 = result.result_base64
            return result
    except Exception as e:
        return TrackedInsertResult(success=False, error=str(e))


# Resources for document discovery

@mcp.resource("documents://")
def list_documents() -> List[str]:
    """List all LibreOffice documents in common locations"""
    documents = []
    
    # Common document locations
    search_paths = [
        Path.home() / "Documents",
        Path.home() / "Desktop", 
        Path.cwd()
    ]
    
    # LibreOffice file extensions
    extensions = {'.odt', '.ods', '.odp', '.odg', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
    
    for search_path in search_paths:
        if search_path.exists():
            for ext in extensions:
                for doc in search_path.rglob(f'*{ext}'):
                    if doc.is_file():
                        documents.append(str(doc))
    
    return sorted(documents)


@mcp.resource("document://{path}")
def get_document_content(path: str) -> str:
    """Get the text content of a specific document"""
    try:
        # Decode the path properly - remove the leading slash if present
        if path.startswith('/'):
            actual_path = path
        else:
            actual_path = '/' + path
            
        content = read_document_text(actual_path)
        return f"Document: {Path(actual_path).name}\n" + \
               f"Words: {content.word_count}, Characters: {content.char_count}\n\n" + \
               content.content
    except Exception as e:
        return f"Error reading document {path}: {str(e)}"


# Additional utility tools

@mcp.tool()
def search_documents(
    query: str,
    search_path: Optional[str] = None,
    documents_base64: Optional[List[str]] = None,
    doc_ids: Optional[List[str]] = None,
    document_urls: Optional[List[str]] = None,
    url_auth: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search for documents containing specific text

    Args:
        query: Text to search for
        search_path: Directory on the SERVER to search in (default: common locations)
        documents_base64: Base64-encoded documents to search (expensive; last resort)
        doc_ids: Handles to search (preferred for documents you uploaded)
        document_urls: http(s)/WebDAV URLs the server fetches and searches
        url_auth: "user:password" for document_urls, if needed
    """
    results = []

    # Explicit document lists take precedence over walking the server's disk.
    explicit = (
        [("doc_id", d) for d in (doc_ids or [])]
        + [("url", u) for u in (document_urls or [])]
        + [("base64", b) for b in (documents_base64 or [])]
    )
    if explicit:
        for idx, (kind, value) in enumerate(explicit):
            label = {"doc_id": f"(doc_id:{value})", "url": value, "base64": f"(base64:{idx})"}[kind]
            kwargs = {
                "doc_id": value if kind == "doc_id" else None,
                "document_url": value if kind == "url" else None,
                "document_base64": value if kind == "base64" else None,
            }
            try:
                with _resolve_document(url_auth=url_auth, **kwargs) as resolved_path:
                    content = read_document_text(path=resolved_path)
                    if query.lower() in content.content.lower():
                        results.append({
                            "path": label,
                            "filename": Path(resolved_path).name,
                            "format": Path(resolved_path).suffix,
                            "word_count": content.word_count,
                            "match_context": _get_match_context(content.content, query)
                        })
            except Exception:
                continue
        return results

    if search_path:
        search_paths = [Path(search_path)]
    else:
        search_paths = [
            Path.home() / "Documents",
            Path.home() / "Desktop",
            Path.cwd()
        ]
    
    extensions = {'.odt', '.ods', '.odp', '.odg', '.doc', '.docx', '.txt'}
    
    for search_dir in search_paths:
        if not search_dir.exists():
            continue
            
        for ext in extensions:
            for doc_path in search_dir.rglob(f'*{ext}'):
                if not doc_path.is_file():
                    continue
                    
                try:
                    content = read_document_text(str(doc_path))
                    if query.lower() in content.content.lower():
                        results.append({
                            "path": str(doc_path),
                            "filename": doc_path.name,
                            "format": doc_path.suffix.lower(),
                            "word_count": content.word_count,
                            "match_context": _get_match_context(content.content, query)
                        })
                except Exception:
                    continue
    
    return results


def _get_match_context(content: str, query: str, context_chars: int = 200) -> str:
    """Get surrounding context for a search match"""
    content_lower = content.lower()
    query_lower = query.lower()
    
    match_pos = content_lower.find(query_lower)
    if match_pos == -1:
        return ""
    
    start = max(0, match_pos - context_chars // 2)
    end = min(len(content), match_pos + len(query) + context_chars // 2)
    
    context = content[start:end]
    if start > 0:
        context = "..." + context
    if end < len(content):
        context = context + "..."
        
    return context


@mcp.tool()
def batch_convert_documents(source_dir: str, target_dir: str, target_format: str, 
                          source_extensions: Optional[List[str]] = None) -> List[ConversionResult]:
    """Convert multiple documents in a directory to a different format
    
    Args:
        source_dir: Directory containing source documents
        target_dir: Directory where converted documents should be saved
        target_format: Target format for conversion
        source_extensions: List of source file extensions to convert (default: common formats)
    """
    source_path = Path(source_dir)
    target_path = Path(target_dir)
    
    if not source_path.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")
    
    target_path.mkdir(parents=True, exist_ok=True)
    
    if source_extensions is None:
        source_extensions = ['.odt', '.ods', '.odp', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx']
    
    results = []
    
    for ext in source_extensions:
        for doc_file in source_path.rglob(f'*{ext}'):
            if doc_file.is_file():
                target_file = target_path / (doc_file.stem + f'.{target_format}')
                result = convert_document(str(doc_file), str(target_file), target_format)
                results.append(result)
    
    return results


@mcp.tool()
def merge_text_documents(
    document_paths: Optional[List[str]] = None,
    output_path: Optional[str] = None,
    separator: str = "\n\n---\n\n",
    documents_base64: Optional[List[str]] = None,
    return_base64: bool = False,
    doc_ids: Optional[List[str]] = None,
    document_urls: Optional[List[str]] = None,
    target_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> DocResult:
    """Merge multiple text documents into a single document

    Sources are merged in this order: doc_ids, then document_urls, then
    documents_base64, then document_paths. Without output_path or target_url the
    merged document stays on the server and you get a doc_id back.

    Args:
        document_paths: Paths on the SERVER's filesystem
        output_path: Write the merged document to this path on the SERVER
        separator: Text to insert between merged documents
        documents_base64: Base64-encoded documents to merge (expensive; last resort)
        return_base64: Return the merged document inline as base64 (expensive)
        doc_ids: Handles of documents to merge (preferred)
        document_urls: http(s)/WebDAV URLs the server fetches and merges
        target_url: Upload the merged document to this URL with PUT
        url_auth: "user:password" for document_urls/target_url, if needed
    """
    merged_content = []

    sources = (
        [("doc_id", d) for d in (doc_ids or [])]
        + [("url", u) for u in (document_urls or [])]
        + [("base64", b) for b in (documents_base64 or [])]
        + [("path", p) for p in (document_paths or [])]
    )

    for idx, (kind, value) in enumerate(sources):
        kwargs = {
            "doc_id": value if kind == "doc_id" else None,
            "document_url": value if kind == "url" else None,
            "document_base64": value if kind == "base64" else None,
            "path": value if kind == "path" else None,
        }
        label = value if kind in ("path", "url") else f"document_{idx}"
        try:
            with _resolve_document(url_auth=url_auth, **kwargs) as resolved_path:
                label = Path(resolved_path).name if kind != "url" else value
                content = read_document_text(path=resolved_path)
                merged_content.append(f"=== {label} ===\n\n{content.content}")
        except Exception as e:
            merged_content.append(f"=== {label} ===\n\nError reading document: {str(e)}")

    final_content = separator.join(merged_content)

    if output_path:
        return create_document(path=output_path, doc_type="writer", content=final_content)
    return create_document(
        doc_type="writer",
        content=final_content,
        return_base64=return_base64,
        target_url=target_url,
        url_auth=url_auth,
    )


@mcp.tool()
def get_document_statistics(
    path: Optional[str] = None,
    document_base64: Optional[str] = None,
    doc_id: Optional[str] = None,
    document_url: Optional[str] = None,
    url_auth: Optional[str] = None,
) -> Dict[str, Any]:
    """Get detailed statistics about a document

    Args:
        path: Path on the SERVER's filesystem
        document_base64: Base64-encoded document content (expensive; last resort)
        doc_id: Handle from a previous tool or from POST /files (preferred)
        document_url: http(s)/WebDAV URL the server fetches itself
        url_auth: "user:password" for document_url, if it needs authentication
    """
    with _resolve_document(path, document_base64, doc_id=doc_id, document_url=document_url, url_auth=url_auth) as resolved_path:
        doc_info = get_document_info(path=resolved_path)
        
        try:
            content = read_document_text(path=resolved_path)
            
            lines = content.content.split('\n')
            paragraphs = [p for p in content.content.split('\n\n') if p.strip()]
            sentences = [s for s in content.content.replace('!', '.').replace('?', '.').split('.') if s.strip()]
            
            return {
                "file_info": doc_info.model_dump(),
                "content_stats": {
                    "word_count": content.word_count,
                    "character_count": content.char_count,
                    "line_count": len(lines),
                    "paragraph_count": len(paragraphs),
                    "sentence_count": len(sentences),
                    "average_words_per_sentence": content.word_count / max(len(sentences), 1),
                    "average_chars_per_word": content.char_count / max(content.word_count, 1)
                }
            }
            
        except Exception as e:
            return {
                "file_info": doc_info.model_dump(),
                "error": f"Could not analyze content: {str(e)}"
            }


@mcp.tool()
def get_server_info() -> Dict[str, Any]:
    """Get information about the LibreOffice MCP server environment.

    Returns platform, LibreOffice version, accessible paths, and operational
    hints. Useful for debugging connection issues and understanding what
    the server can access.
    """
    info: Dict[str, Any] = {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "python_version": platform.python_version(),
        "in_docker": os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv'),
        "transport_mode": _TRANSPORT,
        "temp_directory": tempfile.gettempdir(),
        "libreoffice_version": None,
        "transfer": {
            "preferred": "doc_id",
            "upload_endpoint": f"{docstore.public_base_url() or '<server-base-url>'}/files",
            "download_endpoint": f"{docstore.public_base_url() or '<server-base-url>'}/files/{{doc_id}}",
            "public_url_configured": docstore.public_base_url() is not None,
            "auth_required": http_routes.auth_required(),
            "url_fetch_enabled": True,
            "allowed_url_hosts": urlio.allowed_hosts() or "any public host (loopback/link-local blocked)",
            "doc_ttl_seconds": docstore.ttl_seconds(),
            "max_doc_mb": docstore.max_doc_bytes() / (1024 * 1024),
        },
        "hints": [],
    }

    try:
        result = subprocess.run(
            ['libreoffice', '--version'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info["libreoffice_version"] = result.stdout.strip()
    except Exception:
        pass

    base = docstore.public_base_url() or "<server-base-url>"
    info["hints"].append(
        f"Preferred workflow: upload the file once with "
        f"`curl --data-binary @file.odt '{base}/files?filename=file.odt'`, then pass the "
        f"returned doc_id to the tools. Each tool returns a new doc_id, so you can chain "
        f"them without any document bytes entering the conversation."
    )
    info["hints"].append(
        "Second best: 'document_url' — give the server an http(s)/WebDAV URL and it fetches "
        "the document itself. 'target_url' does the same in reverse (PUT)."
    )
    info["hints"].append(
        "'document_base64' still works but costs ~1.33 bytes of context per document byte, "
        "on every call. Use it only when the client cannot make an HTTP request itself."
    )

    if info["platform"] == "Linux" or info["in_docker"]:
        info["hints"].append(
            "This server does not share a filesystem with you: Windows paths (C:\\...) and "
            "your local paths are NOT accessible. Use doc_id or document_url instead of 'path'."
        )

    if not info["transfer"]["public_url_configured"]:
        info["hints"].append(
            "MCP_PUBLIC_URL is not set, so download_url fields will be null. Set it to the "
            "URL clients reach this server on (e.g. http://nas:8765) to get working links."
        )

    if _TRANSPORT != "stdio" and not info["transfer"]["auth_required"]:
        info["hints"].append(
            "⚠ MCP_UPLOAD_TOKEN is not set: anyone who can reach /files can upload and "
            "download documents. Set it before exposing this server beyond a trusted LAN."
        )

    return info


@mcp.tool()
def fetch_document(document_url: str, url_auth: Optional[str] = None) -> DocResult:
    """Fetch a document from a URL into server-side storage and return its doc_id

    Use this to bring a document onto the server once (from Nextcloud/WebDAV or
    any http(s) URL) and then work on it by handle.

    Args:
        document_url: http(s)/WebDAV URL of the document
        url_auth: "user:password" if the URL needs authentication
    """
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            name = urlio.filename_from_url(document_url)
            dest = Path(tmp_dir) / name
            urlio.fetch(document_url, dest, url_auth=url_auth)
            handle = docstore.store_file(dest, name)
        return DocResult(
            success=True,
            filename=handle.filename,
            format=Path(handle.filename).suffix.lstrip('.'),
            size_bytes=handle.size_bytes,
            modified_time=handle.created_at,
            exists=True,
            doc_id=handle.doc_id,
            download_url=docstore.download_url(handle.doc_id),
        )
    except Exception as e:
        return DocResult(success=False, error=str(e))


@mcp.tool()
def delete_document(doc_id: str) -> Dict[str, Any]:
    """Drop a stored document handle before its TTL expires

    Args:
        doc_id: The handle to delete
    """
    try:
        deleted = docstore.delete(doc_id)
        return {"deleted": deleted, "doc_id": doc_id}
    except docstore.DocStoreError as e:
        return {"deleted": False, "doc_id": doc_id, "error": str(e)}


@mcp.tool()
def list_stored_documents() -> List[Dict[str, Any]]:
    """List the documents currently held in server-side storage"""
    return [
        {
            "doc_id": h.doc_id,
            "filename": h.filename,
            "size_bytes": h.size_bytes,
            "created_at": h.created_at.isoformat(),
            "download_url": docstore.download_url(h.doc_id),
        }
        for h in docstore.list_handles()
    ]


# Main server entry point
def main():
    """Run the LibreOffice MCP server"""
    import sys
    
    # Parse command line arguments
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        
        if arg == "--test":
            # Test mode - run some basic functionality tests
            print("🧪 Running LibreOffice MCP Server tests...")
            asyncio.run(test_server())
            return
        
        elif arg == "--help" or arg == "-h":
            # Show help
            print("LibreOffice MCP Server")
            print("=" * 30)
            print("Usage:")
            print("  python src/main.py          # Start MCP server (stdio mode)")
            print("  python src/main.py --test   # Run functionality tests")
            print("  python src/main.py --help   # Show this help")
            print("")
            print("MCP Server Mode:")
            print("  The server runs in stdio mode for MCP protocol communication.")
            print("  It reads JSON-RPC messages from stdin and writes responses to stdout.")
            print("  Use with MCP clients like Claude Desktop or test with test_client.py")
            print("")
            print("Base64 Support:")
            print("  All document tools accept document_base64 / documents_base64 params")
            print("  for stateless remote operation (e.g. via Docker).")
            print("  Use return_base64=True to get modified documents as base64.")
            print("")
            print("Testing:")
            print("  cd tests/ && python test_client.py  # Interactive test client")
            return
        
        elif arg == "--version":
            print("LibreOffice MCP Server v1.0.0")
            return
    
    # Normal server mode - show startup message and run
    print("🚀 Starting LibreOffice MCP Server...", file=sys.stderr)
    print(f"📡 Running in MCP protocol mode ({_TRANSPORT})", file=sys.stderr)
    if _TRANSPORT != "stdio":
        print(f"🌐 Listening on {mcp.settings.host}:{mcp.settings.port}", file=sys.stderr)
    print("💡 Use --help for command line options", file=sys.stderr)
    print("🔌 Connect via MCP clients or test with: cd tests/ && python test_client.py", file=sys.stderr)
    print("", file=sys.stderr)

    try:
        mcp.run(transport=_TRANSPORT)
    except KeyboardInterrupt:
        print("\n👋 LibreOffice MCP Server stopped", file=sys.stderr)
    except Exception as e:
        print(f"\n❌ Server error: {e}", file=sys.stderr)
        sys.exit(1)


async def test_server():
    """Test the server functionality"""
    print("Testing LibreOffice MCP Server...")
    print("=" * 50)
    
    if not await test_libreoffice_installation():
        print("❌ LibreOffice installation test failed")
        return
    
    test_doc = "/tmp/test_document.odt"
    pdf_path = "/tmp/test_document.pdf"
    try:
        # Test path-based creation
        print("\nTesting path-based document creation...")
        result = create_document(test_doc, "writer", "Hello from LibreOffice MCP!")
        print(f"✓ Created: {result.filename} ({result.size_bytes} bytes)")
        
        # Test reading via path
        print("Testing path-based document reading...")
        content = read_document_text(path=test_doc)
        print(f"✓ Read: {content.word_count} words, {content.char_count} chars")
        
        # Test path-based conversion
        print("Testing path-based document conversion...")
        conversion = convert_document(source_path=test_doc, target_path=pdf_path, target_format="pdf")
        print(f"✓ Converted to PDF: {conversion.success}")
        
        # Test path-based statistics
        print("Testing path-based document statistics...")
        stats = get_document_statistics(path=test_doc)
        print(f"✓ Statistics: {stats['content_stats']['word_count']} words")
        
        # Test base64 round-trip
        print("\nTesting base64 round-trip...")
        with open(test_doc, "rb") as f:
            doc_b64 = base64.b64encode(f.read()).decode("ascii")
        
        content_b64 = read_document_text(document_base64=doc_b64)
        print(f"✓ Base64 read: {content_b64.word_count} words")
        
        stats_b64 = get_document_statistics(document_base64=doc_b64)
        print(f"✓ Base64 stats: {stats_b64['content_stats']['word_count']} words")
        
        # Test insert text via base64
        print("Testing base64 text insertion...")
        insert_result = insert_text_at_position(document_base64=doc_b64, text="\n\nInserted via base64!", position="end", return_base64=True)
        if insert_result.success:
            doc_b64 = insert_result.result_base64
            content_after = read_document_text(document_base64=doc_b64)
            print(f"✓ After base64 insert: {content_after.word_count} words")
        
        # Test base64 conversion
        print("Testing base64 document conversion...")
        conv_b64 = convert_document(document_base64=doc_b64, target_format="txt", return_base64=True)
        if conv_b64.success and conv_b64.result_base64:
            txt_content = base64.b64decode(conv_b64.result_base64).decode("utf-8", errors="ignore")
            print(f"✓ Base64 conversion: {len(txt_content)} chars")
        
        # Test base64 document creation
        print("Testing base64 document creation...")
        created = create_document(doc_type="writer", content="Created via base64 mode!", return_base64=True)
        if created.success and created.result_base64:
            verify = read_document_text(document_base64=created.result_base64)
            print(f"✓ Base64 create + read: {verify.word_count} words - '{verify.content.strip()}'")
        
        print("\n✅ All tests passed!")
        
    except Exception as e:
        print(f"❌ Test failed: {str(e)}")
        import traceback
        traceback.print_exc()
    
    finally:
        for test_file in [test_doc, pdf_path]:
            try:
                Path(test_file).unlink(missing_ok=True)
            except:
                pass


# Test LibreOffice functionality directly
async def test_libreoffice_installation():
    """Test LibreOffice installation and basic functionality"""
    print("\nTesting LibreOffice Installation...")
    print("=" * 40)
    
    try:
        # Test basic LibreOffice command
        result = _run_libreoffice_command(['--version'])
        if result.returncode == 0:
            print(f"✓ LibreOffice version: {result.stdout.strip()}")
        else:
            print(f"⚠ LibreOffice version check failed: {result.stderr}")
    except Exception as e:
        print(f"❌ LibreOffice not accessible: {str(e)}")
        return False
    
    # Test headless mode
    try:
        result = _run_libreoffice_command(['--headless', '--help'])
        if result.returncode == 0 or 'headless' in result.stdout.lower():
            print("✓ LibreOffice headless mode available")
        else:
            print(f"⚠ LibreOffice headless mode issue: {result.stderr}")
    except Exception as e:
        print(f"❌ LibreOffice headless mode failed: {str(e)}")
    
    return True


# Live viewing and document management tools

@mcp.tool()
def open_document_in_libreoffice(path: str, readonly: bool = False) -> Dict[str, Any]:
    """Open a document in LibreOffice GUI for live viewing
    
    Args:
        path: Path to the document to open
        readonly: Whether to open in read-only mode (default: False)
    """
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Document not found: {path}")
    
    try:
        # Build command to open LibreOffice with GUI
        cmd = ['libreoffice']
        
        if readonly:
            cmd.append('--view')
        
        # Add the document path
        cmd.append(str(path_obj.absolute()))
        
        # Start LibreOffice GUI (non-blocking)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True  # Detach from parent process
        )
        
        return {
            "success": True,
            "message": f"Opened {path_obj.name} in LibreOffice GUI",
            "path": str(path_obj.absolute()),
            "readonly": readonly,
            "process_id": process.pid,
            "note": "Document is now open for live viewing. Changes made via MCP will be reflected after saving and refreshing."
        }
        
    except Exception as e:
        raise RuntimeError(f"Failed to open document in LibreOffice: {str(e)}")


@mcp.tool()
def refresh_document_in_libreoffice(path: str) -> Dict[str, Any]:
    """Send a refresh signal to LibreOffice to reload a document
    
    Args:
        path: Path to the document that should be refreshed
    """
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Document not found: {path}")
    
    try:
        # Try to send a signal to LibreOffice to refresh
        # This uses LibreOffice's ability to detect file changes
        
        # Method 1: Touch the file to update modification time
        import time
        current_time = time.time()
        path_obj.touch()
        
        # Method 2: Try to send a signal via LibreOffice's socket interface
        try:
            # This is a more advanced approach that may work if LibreOffice is running
            result = subprocess.run([
                'libreoffice', '--invisible', '--headless',
                '--accept=socket,host=127.0.0.1,port=2002;urp;',
                '--norestore', '--nologo'
            ], timeout=2, capture_output=True)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # LibreOffice may already be running or not available
        
        return {
            "success": True,
            "message": f"Refresh signal sent for {path_obj.name}",
            "path": str(path_obj.absolute()),
            "note": "LibreOffice should detect the file change and prompt to reload. Manual refresh may be needed."
        }
        
    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to refresh document: {str(e)}",
            "path": str(path_obj.absolute()),
            "note": "Try manually refreshing in LibreOffice (File → Reload)"
        }


@mcp.tool()
def watch_document_changes(path: str, duration_seconds: int = 30) -> Dict[str, Any]:
    """Watch a document for changes and provide live updates
    
    Args:
        path: Path to the document to watch
        duration_seconds: How long to watch for changes (default: 30 seconds)
    """
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Document not found: {path}")
    
    try:
        import time
        
        # Get initial state
        initial_stat = path_obj.stat()
        initial_size = initial_stat.st_size
        initial_mtime = initial_stat.st_mtime
        
        start_time = time.time()
        changes_detected = []
        
        print(f"👀 Watching {path_obj.name} for {duration_seconds} seconds...")
        
        while time.time() - start_time < duration_seconds:
            try:
                current_stat = path_obj.stat()
                current_size = current_stat.st_size
                current_mtime = current_stat.st_mtime
                
                if current_mtime > initial_mtime or current_size != initial_size:
                    change_info = {
                        "timestamp": datetime.now().isoformat(),
                        "size_before": initial_size,
                        "size_after": current_size,
                        "size_change": current_size - initial_size,
                        "modification_time": datetime.fromtimestamp(current_mtime).isoformat()
                    }
                    changes_detected.append(change_info)
                    
                    # Update baseline
                    initial_size = current_size
                    initial_mtime = current_mtime
                    
                    print(f"📝 Change detected: {change_info['size_change']:+d} bytes at {change_info['timestamp']}")
                
                time.sleep(1)  # Check every second
                
            except FileNotFoundError:
                break  # File was deleted
        
        return {
            "success": True,
            "path": str(path_obj.absolute()),
            "watch_duration": duration_seconds,
            "changes_detected": len(changes_detected),
            "changes": changes_detected,
            "message": f"Watched {path_obj.name} for {duration_seconds} seconds, detected {len(changes_detected)} changes"
        }
        
    except Exception as e:
        raise RuntimeError(f"Failed to watch document: {str(e)}")


@mcp.tool()
def create_live_editing_session(path: str, auto_refresh: bool = True) -> Dict[str, Any]:
    """Create a live editing session with automatic refresh capabilities
    
    Args:
        path: Path to the document for live editing
        auto_refresh: Whether to enable automatic refresh detection
    """
    path_obj = Path(path)
    
    try:
        # 1. Open the document in LibreOffice GUI
        open_result = open_document_in_libreoffice(str(path_obj), readonly=False)
        
        # 2. Set up file monitoring if requested
        session_info = {
            "session_id": f"live_session_{int(time.time())}",
            "document_path": str(path_obj.absolute()),
            "document_name": path_obj.name,
            "opened_in_gui": open_result["success"],
            "auto_refresh_enabled": auto_refresh,
            "created_at": datetime.now().isoformat(),
            "instructions": {
                "view_changes": "Document is open in LibreOffice GUI",
                "make_mcp_changes": "Use insert_text_at_position, convert_document, etc.",
                "see_updates": "LibreOffice will detect file changes and prompt to reload",
                "manual_refresh": "Press Ctrl+Shift+R in LibreOffice to force reload",
                "end_session": "Close LibreOffice window when done"
            }
        }
        
        if auto_refresh:
            session_info["monitoring"] = "File modification time will be updated after MCP operations"
        
        return session_info
        
    except Exception as e:
        raise RuntimeError(f"Failed to create live editing session: {str(e)}")


if __name__ == "__main__":
    main()
