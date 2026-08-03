"""
Plain-HTTP upload/download endpoints, served on the same port as the MCP stream.

These are the out-of-band side of the document store: a client PUTs bytes here,
gets a `doc_id` back, and from then on passes that handle to the MCP tools. No
document content ever crosses the MCP channel, so it never enters the model's
context.

FastMCP's custom routes deliberately bypass MCP authorization (see the docstring
of FastMCP.custom_route), so /files carries its own bearer-token check.
"""

import hmac
import os
from typing import Optional
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

import docstore

_UPLOAD_TOKEN_ENV = "MCP_UPLOAD_TOKEN"


def upload_token() -> Optional[str]:
    return os.environ.get(_UPLOAD_TOKEN_ENV, "").strip() or None


def auth_required() -> bool:
    return upload_token() is not None


def _authorized(request: Request) -> bool:
    token = upload_token()
    if token is None:
        return True  # No token configured: the store is open. get_server_info warns about this.
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip(), token)


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": f"Send 'Authorization: Bearer <{_UPLOAD_TOKEN_ENV}>'."},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _request_base_url(request: Request) -> str:
    """Fall back to the Host the client actually reached us on."""
    return docstore.public_base_url() or str(request.base_url).rstrip("/")


async def _read_body(request: Request) -> tuple[bytes, Optional[str]]:
    """Accept either a raw body or a multipart upload. Returns (data, filename)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        for value in form.values():
            if hasattr(value, "read"):
                return await value.read(), getattr(value, "filename", None)
        return b"", None
    return await request.body(), None


def register_routes(mcp) -> None:
    """Attach the file endpoints to a FastMCP instance."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "mcp-libre"})

    @mcp.custom_route("/files", methods=["POST", "PUT"])
    async def upload(request: Request) -> Response:
        if not _authorized(request):
            return _unauthorized()

        data, form_filename = await _read_body(request)
        if not data:
            return JSONResponse(
                {"error": "empty_body", "detail": "Send the document bytes as the request body."},
                status_code=400,
            )

        filename = request.query_params.get("filename") or form_filename
        try:
            handle = docstore.store(data, filename)
        except docstore.DocTooLarge as e:
            return JSONResponse({"error": "too_large", "detail": str(e)}, status_code=413)

        return JSONResponse(
            {
                "doc_id": handle.doc_id,
                "filename": handle.filename,
                "size_bytes": handle.size_bytes,
                "created_at": handle.created_at.isoformat(),
                "expires_in_seconds": docstore.ttl_seconds(),
                "download_url": docstore.download_url(handle.doc_id, _request_base_url(request)),
            },
            status_code=201,
        )

    @mcp.custom_route("/files/{doc_id}", methods=["GET"])
    async def download(request: Request) -> Response:
        if not _authorized(request):
            return _unauthorized()
        doc_id = request.path_params["doc_id"]
        try:
            handle = docstore.get(doc_id)
        except docstore.DocNotFound as e:
            return JSONResponse({"error": "not_found", "detail": str(e)}, status_code=404)
        return FileResponse(
            handle.path,
            filename=handle.filename,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{quote(handle.filename)}"'},
        )

    @mcp.custom_route("/files/{doc_id}", methods=["DELETE"])
    async def remove(request: Request) -> Response:
        if not _authorized(request):
            return _unauthorized()
        doc_id = request.path_params["doc_id"]
        try:
            deleted = docstore.delete(doc_id)
        except docstore.DocNotFound as e:
            return JSONResponse({"error": "not_found", "detail": str(e)}, status_code=404)
        if not deleted:
            return JSONResponse({"error": "not_found", "detail": f"Unknown doc_id: {doc_id}"}, status_code=404)
        return JSONResponse({"deleted": True, "doc_id": doc_id})
