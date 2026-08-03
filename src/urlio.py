"""
Fetching documents from, and pushing them to, HTTP(S) URLs (incl. WebDAV).

This is the second way to keep documents out of the MCP channel: instead of
base64-ing a file through the model, the caller names a URL and the *server*
moves the bytes. Nextcloud/WebDAV shares are the intended case.

Because the server will make requests on behalf of whoever is talking to it,
every fetch goes through an SSRF guard. Set MCP_URL_ALLOWED_HOSTS for a strict
allowlist; with it unset we still refuse loopback, link-local, and cloud
metadata addresses, which are the targets that actually matter for a server
sitting on a home LAN.
"""

import ipaddress
import os
import socket
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

import docstore

DEFAULT_TIMEOUT = 30.0


class UrlTransferError(Exception):
    """A URL fetch or upload failed, or was refused by the guard."""


def allowed_hosts() -> list:
    raw = os.environ.get("MCP_URL_ALLOWED_HOSTS", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def url_timeout() -> float:
    try:
        return float(os.environ.get("MCP_URL_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


def _is_blocked_ip(host: str) -> bool:
    """True if the hostname resolves to an address we refuse to talk to."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # Can't resolve it; don't try.
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return True
    return False


def check_url(url: str) -> None:
    """Raise UrlTransferError if this URL must not be requested."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlTransferError(f"Only http:// and https:// URLs are supported, got {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UrlTransferError(f"URL has no host: {url}")

    allow = allowed_hosts()
    if allow:
        port = parsed.port
        candidates = {host, f"{host}:{port}"} if port else {host}
        if not candidates & set(allow):
            raise UrlTransferError(
                f"Host {host!r} is not in MCP_URL_ALLOWED_HOSTS ({', '.join(allow)}). "
                f"Add it to the allowlist to let the server fetch from there."
            )
        return  # An explicit allowlist is the operator's decision; honour it as-is.

    if _is_blocked_ip(host):
        raise UrlTransferError(
            f"Refusing to fetch {host!r}: it resolves to a loopback/link-local/reserved address. "
            f"Set MCP_URL_ALLOWED_HOSTS to allow specific internal hosts on purpose."
        )


def _auth_and_headers(url_auth: Optional[str]) -> Tuple[Optional[tuple], dict]:
    """Build httpx auth/headers from a 'user:pass' param or MCP_URL_BEARER."""
    headers = {}
    auth = None
    if url_auth:
        user, _, password = url_auth.partition(":")
        auth = (user, password)
    bearer = os.environ.get("MCP_URL_BEARER", "").strip()
    if bearer and not auth:
        headers["Authorization"] = f"Bearer {bearer}"
    return auth, headers


def filename_from_url(url: str, fallback: str = "document.odt") -> str:
    name = Path(urlparse(url).path).name
    return name or fallback


def fetch(url: str, dest: Path, url_auth: Optional[str] = None) -> int:
    """Download a URL into `dest`. Returns the byte count.

    Streams and enforces MCP_MAX_DOC_MB as it goes, so an oversized (or
    unbounded) response is cut off rather than buffered whole.
    """
    check_url(url)
    limit = docstore.max_doc_bytes()
    auth, headers = _auth_and_headers(url_auth)

    written = 0
    try:
        with httpx.stream(
            "GET", url, auth=auth, headers=headers, timeout=url_timeout(), follow_redirects=True
        ) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > limit:
                raise UrlTransferError(
                    f"Remote document is {declared} bytes, over the {limit}-byte limit (MCP_MAX_DOC_MB)"
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > limit:
                        raise UrlTransferError(
                            f"Remote document exceeds the {limit}-byte limit (MCP_MAX_DOC_MB)"
                        )
                    fh.write(chunk)
    except httpx.HTTPStatusError as e:
        raise UrlTransferError(f"GET {url} returned HTTP {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise UrlTransferError(f"GET {url} failed: {e}") from e

    if written == 0:
        raise UrlTransferError(f"GET {url} returned an empty body")
    return written


def put(url: str, source: Path, url_auth: Optional[str] = None) -> int:
    """Upload a file to a URL with PUT (the WebDAV convention). Returns status code."""
    check_url(url)
    auth, headers = _auth_and_headers(url_auth)
    try:
        response = httpx.put(
            url,
            content=Path(source).read_bytes(),
            auth=auth,
            headers={**headers, "Content-Type": "application/octet-stream"},
            timeout=url_timeout(),
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UrlTransferError(f"PUT {url} returned HTTP {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise UrlTransferError(f"PUT {url} failed: {e}") from e
    return response.status_code
