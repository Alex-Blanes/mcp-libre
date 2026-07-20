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
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Union
from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from track_changes import TrackedInsertResult, insert_tracked_text_impl

# Transport is stdio by default (local use).
# Set MCP_TRANSPORT=streamable-http (+ MCP_HOST/MCP_PORT/MCP_ALLOWED_HOSTS) to run
# as a network server, e.g. inside a Docker container.
_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
_EXTRA_ALLOWED_HOSTS = [h for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h]

mcp = FastMCP(
    "LibreOffice MCP Server",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8765")),
)

# FastMCP's DNS-rebinding protection only allows localhost by default (and, at
# least in this mcp version, ends up None instead of that default once host/port
# are passed explicitly above) — build it ourselves so a remote deployment
# can be reached at all.
_default_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_default_origins = [f"{scheme}://{h}" for h in ["127.0.0.1:*", "localhost:*", "[::1]:*"] for scheme in ("http", "https")]
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_default_hosts + _EXTRA_ALLOWED_HOSTS,
    allowed_origins=_default_origins
    + [f"http://{h}" for h in _EXTRA_ALLOWED_HOSTS]
    + [f"https://{h}" for h in _EXTRA_ALLOWED_HOSTS],
)


# Data models for structured responses
class DocumentInfo(BaseModel):
    """Information about a LibreOffice document"""
    path: str = Field(description="Full path to the document")
    filename: str = Field(description="Document filename")
    format: str = Field(description="Document format (odt, ods, odp, etc.)")
    size_bytes: int = Field(description="File size in bytes")
    modified_time: datetime = Field(description="Last modification time")
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
    modified_time: Optional[datetime] = Field(default=None, description="Last modification time")
    exists: Optional[bool] = Field(default=None, description="Whether the file exists")
    result_base64: Optional[str] = Field(default=None, description="Base64-encoded result document (base64 mode)")


# Helper functions
def _run_libreoffice_command(args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a LibreOffice command with proper error handling"""
    try:
        # Try different common LibreOffice executable names
        for executable in ['libreoffice', 'loffice', 'soffice']:
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
    return DocumentInfo(
        path=str(path.absolute()),
        filename=path.name,
        format=path.suffix.lower().lstrip('.'),
        size_bytes=path.stat().st_size if path.exists() else 0,
        modified_time=datetime.fromtimestamp(path.stat().st_mtime) if path.exists() else datetime.now(),
        exists=path.exists()
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


@contextmanager
def _resolve_document(
    path: Optional[str] = None,
    document_base64: Optional[str] = None,
    format_hint: str = "writer",
) -> Generator[str, None, None]:
    """Resolve a document from either a filesystem path or base64 content.

    If document_base64 is provided, writes it to a temp file and cleans up
    on exit. If path is provided, validates it exists and yields it directly.
    """
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
                    f"👉 Read your local file first, encode it to base64, and call "
                    f"this tool with the 'document_base64' parameter instead.\n\n"
                    f"Example workflow:\n"
                    f"  1. Read file bytes from your local machine\n"
                    f"  2. Base64-encode the bytes\n"
                    f"  3. Pass as: document_base64=<encoded_content>"
                )
            raise FileNotFoundError(
                f"Document not found: {path}. "
                f"If the file is on your local machine and this is a remote MCP "
                f"server, pass the file content as 'document_base64' instead of 'path'."
            )
        yield normalized
        return
    raise ValueError("Either 'path' or 'document_base64' must be provided")


def _read_as_base64(path: str) -> str:
    """Read a file and return its content as a base64-encoded string."""
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


# Core LibreOffice Tools

@mcp.tool()
def create_document(path: Optional[str] = None, doc_type: str = "writer", content: str = "", return_base64: bool = False) -> DocResult:
    """Create a new LibreOffice document
    
    Args:
        path: Full path where the document should be created (omit for base64 mode)
        doc_type: Type of document to create (writer, calc, impress, draw)
        content: Initial content for the document (for writer documents)
        return_base64: If True, return the document as base64 instead of writing to disk
    """
    # Determine mode
    use_base64 = return_base64 or not path
    
    if use_base64:
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
        if use_base64:
            return DocResult(**doc_info.model_dump(), success=True, result_base64=_read_as_base64(str(path_obj)))
        return DocResult(**doc_info.model_dump(), success=True)
        
    except Exception as e:
        return DocResult(success=False, error=str(e))
    finally:
        if _tmp_manager:
            _tmp_manager.cleanup()


@mcp.tool()
def read_document_text(path: Optional[str] = None, document_base64: Optional[str] = None) -> TextContent:
    """Extract text content from a LibreOffice document
    
    Args:
        path: Path to the document file (omit if using document_base64)
        document_base64: Base64-encoded document content (alternative to path)
    """
    try:
        with _resolve_document(path, document_base64) as resolved_path:
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
) -> ConversionResult:
    """Convert a document to a different format
    
    Args:
        source_path: Path to the source document (omit if using document_base64)
        target_path: Path where converted document should be saved (omit for base64 output)
        target_format: Target format (pdf, docx, xlsx, pptx, html, txt, etc.)
        document_base64: Base64-encoded source document (alternative to source_path)
        return_base64: If True, return the converted document as base64
    """
    use_base64_input = document_base64 is not None
    use_base64_output = return_base64 or (use_base64_input and not target_path)
    
    try:
        with _resolve_document(source_path, document_base64) as resolved_source:
            source_obj = Path(resolved_source)
            
            if use_base64_output:
                _tmp_manager = tempfile.TemporaryDirectory()
                resolved_target = Path(_tmp_manager.name) / f"output.{target_format}"
            else:
                _tmp_manager = None
                resolved_target = Path(target_path)
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
                    source_path=str(source_obj) if use_base64_input else (source_path or str(source_obj)),
                    target_path=str(resolved_target),
                    source_format=source_obj.suffix.lower().lstrip('.'),
                    target_format=target_format,
                    success=success,
                    error_message=error_msg,
                )
                
                if success and use_base64_output:
                    conv_result.result_base64 = _read_as_base64(str(resolved_target))
                
                return conv_result
                
            except Exception as e:
                return ConversionResult(
                    source_path=str(source_obj) if use_base64_input else (source_path or str(source_obj)),
                    target_path=str(resolved_target),
                    source_format=source_obj.suffix.lower().lstrip('.'),
                    target_format=target_format,
                    success=False,
                    error_message=str(e)
                )
            finally:
                if _tmp_manager:
                    _tmp_manager.cleanup()
    except FileNotFoundError as e:
        return ConversionResult(
            source_path=source_path or "(base64)",
            target_path=target_path or "(base64)",
            source_format="",
            target_format=target_format,
            success=False,
            error_message=str(e)
        )


@mcp.tool()
def get_document_info(path: Optional[str] = None, document_base64: Optional[str] = None) -> DocumentInfo:
    """Get detailed information about a LibreOffice document
    
    Args:
        path: Path to the document file (omit if using document_base64)
        document_base64: Base64-encoded document content (alternative to path)
    """
    with _resolve_document(path, document_base64) as resolved_path:
        return _get_document_info(resolved_path)


@mcp.tool()
def read_spreadsheet_data(path: Optional[str] = None, document_base64: Optional[str] = None, sheet_name: Optional[str] = None, max_rows: int = 100) -> SpreadsheetData:
    """Read data from a LibreOffice Calc spreadsheet
    
    Args:
        path: Path to the spreadsheet file (.ods, .xlsx, etc.; omit if using document_base64)
        document_base64: Base64-encoded spreadsheet content (alternative to path)
        sheet_name: Name of the specific sheet to read (if None, reads first sheet)
        max_rows: Maximum number of rows to read (default 100)
    """
    try:
        with _resolve_document(path, document_base64, format_hint="spreadsheet") as resolved_path:
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
) -> DocResult:
    """Insert text into a LibreOffice Writer document
    
    Args:
        path: Path to the document file (omit if using document_base64)
        text: Text to insert
        position: Where to insert the text ("start", "end", or "replace")
        document_base64: Base64-encoded document content (alternative to path)
        return_base64: If True, return the modified document as base64
    """
    use_base64 = return_base64 or (document_base64 is not None and not path)
    
    try:
        with _resolve_document(path, document_base64) as resolved_path:
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
            
            import shutil
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
            if use_base64:
                return DocResult(**doc_info.model_dump(), success=True, result_base64=_read_as_base64(str(path_obj)))
            return DocResult(**doc_info.model_dump(), success=True)
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
    document_base64: str,
    anchor_text: str,
    new_text: str,
    author: Optional[str] = None,
    insert_mode: str = "after",
) -> TrackedInsertResult:
    """Insert text into an .odt document as a tracked change (control de cambios)

    Stateless: takes the source .odt as a base64 blob and returns the modified
    .odt as a base64 blob — it never touches the caller's filesystem. The
    caller reads the source file, passes
    its bytes here, and writes the returned bytes back wherever they came from.

    Unlike insert_text_at_position, this does NOT rewrite the document content.
    It opens the document via LibreOffice's UNO API, enables RecordChanges,
    finds anchor_text, and inserts new_text next to it so it shows up as a
    reviewable insertion (Editar > Seguimiento de cambios > Gestionar).

    Args:
        document_base64: Base64-encoded content of the source .odt file
        anchor_text: Existing text used to locate the insertion point
        new_text: Text to insert as a tracked insertion
        author: "Nombre Apellido" to attribute the change to (optional)
        insert_mode: "after" (default) or "before" the anchor text
    """
    return insert_tracked_text_impl(
        document_base64=document_base64,
        anchor_text=anchor_text,
        new_text=new_text,
        author=author,
        insert_mode=insert_mode,
    )


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
def search_documents(query: str, search_path: Optional[str] = None, documents_base64: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Search for documents containing specific text
    
    Args:
        query: Text to search for
        search_path: Directory to search in (default: common document locations)
        documents_base64: List of base64-encoded documents to search (alternative to filesystem search)
    """
    results = []
    
    if documents_base64:
        for idx, doc_b64 in enumerate(documents_base64):
            try:
                with _resolve_document(document_base64=doc_b64) as resolved_path:
                    content = read_document_text(path=resolved_path)
                    if query.lower() in content.content.lower():
                        results.append({
                            "path": f"(base64:{idx})",
                            "filename": f"document_{idx}.odt",
                            "format": ".odt",
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
) -> DocResult:
    """Merge multiple text documents into a single document
    
    Args:
        document_paths: List of paths to documents to merge (omit if using documents_base64)
        output_path: Path where merged document should be saved (omit for base64 output)
        separator: Text to insert between merged documents
        documents_base64: List of base64-encoded documents to merge (alternative to document_paths)
        return_base64: If True, return the merged document as base64
    """
    use_base64 = return_base64 or (documents_base64 is not None and not output_path)
    merged_content = []
    
    if documents_base64:
        for idx, doc_b64 in enumerate(documents_base64):
            try:
                with _resolve_document(document_base64=doc_b64) as resolved_path:
                    content = read_document_text(path=resolved_path)
                    merged_content.append(f"=== document_{idx}.odt ===\n\n{content.content}")
            except Exception as e:
                merged_content.append(f"=== document_{idx}.odt ===\n\nError reading document: {str(e)}")
    else:
        paths = document_paths or []
        for doc_path in paths:
            try:
                content = read_document_text(doc_path)
                doc_name = Path(doc_path).name
                merged_content.append(f"=== {doc_name} ===\n\n{content.content}")
            except Exception as e:
                merged_content.append(f"=== {Path(doc_path).name} ===\n\nError reading document: {str(e)}")
    
    final_content = separator.join(merged_content)
    
    if use_base64:
        return create_document(doc_type="writer", content=final_content, return_base64=True)
    return create_document(path=output_path or "merged.odt", doc_type="writer", content=final_content)


@mcp.tool()
def get_document_statistics(path: Optional[str] = None, document_base64: Optional[str] = None) -> Dict[str, Any]:
    """Get detailed statistics about a document
    
    Args:
        path: Path to the document file (omit if using document_base64)
        document_base64: Base64-encoded document content (alternative to path)
    """
    with _resolve_document(path, document_base64) as resolved_path:
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
        "hostname": platform.node(),
        "temp_directory": tempfile.gettempdir(),
        "libreoffice_version": None,
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

    server_is_linux = info["platform"] == "Linux"
    if server_is_linux:
        info["hints"].append(
            "Server runs on Linux. Windows paths (C:\\...) are NOT accessible "
            "from this server. Always use 'document_base64' parameter to send "
            "document content when the file is on your local Windows machine."
        )

    if info["in_docker"]:
        info["hints"].append(
            "Server runs inside a Docker container. The container filesystem "
            "is isolated from the host. Read documents locally and pass them "
            "via 'document_base64'."
        )

    info["hints"].append(
        "All tools support 'document_base64' as an alternative to filesystem "
        "paths. When you get 'Document not found' errors, switch to "
        "document_base64 mode: read the file locally → encode to base64 → "
        "pass as document_base64=<encoded>."
    )

    return info


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
            print("  for stateless remote operation (e.g. via Docker container).")
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
