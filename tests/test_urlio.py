"""URL transfer: the SSRF guard, the size cap, and auth propagation."""

import httpx
import pytest

import urlio


@pytest.fixture
def mock_transport(monkeypatch):
    """Route every httpx request to a handler the test controls."""
    calls = []

    def install(handler):
        transport = httpx.MockTransport(handler)
        real_stream, real_put = httpx.stream, httpx.put

        def stream(method, url, **kwargs):
            kwargs.pop("follow_redirects", None)
            client = httpx.Client(transport=transport)
            return client.stream(method, url, follow_redirects=True, **kwargs)

        def put(url, **kwargs):
            kwargs.pop("follow_redirects", None)
            with httpx.Client(transport=transport) as client:
                return client.put(url, **kwargs)

        monkeypatch.setattr(httpx, "stream", stream)
        monkeypatch.setattr(httpx, "put", put)
        return calls

    install.calls = calls
    return install


def _ok(body=b"content"):
    def handler(request):
        return httpx.Response(200, content=body)

    return handler


def _recording(calls, body=b"content", status=200):
    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=body)

    return handler


# --- guard ---------------------------------------------------------------

@pytest.mark.parametrize("url", ["ftp://host/x.odt", "file:///etc/passwd", "gopher://x"])
def test_non_http_schemes_are_refused(url, workspace):
    with pytest.raises(urlio.UrlTransferError, match="http"):
        urlio.check_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost/x", "http://[::1]/x"])
def test_loopback_is_refused_by_default(url, workspace, monkeypatch):
    monkeypatch.delenv("MCP_URL_ALLOWED_HOSTS", raising=False)
    with pytest.raises(urlio.UrlTransferError, match="loopback|link-local|reserved"):
        urlio.check_url(url)


def test_allowlist_permits_a_named_internal_host(workspace, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "localhost")
    urlio.check_url("http://localhost:8100/remote.php/dav/x.odt")  # must not raise


def test_allowlist_excludes_everything_else(workspace, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "nextcloud.example")
    with pytest.raises(urlio.UrlTransferError, match="not in MCP_URL_ALLOWED_HOSTS"):
        urlio.check_url("https://evil.example/x.odt")


# --- fetch ---------------------------------------------------------------

def test_fetch_writes_the_body(workspace, tmp_path, mock_transport, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "files.example")
    mock_transport(_ok(b"odt bytes"))
    dest = tmp_path / "out.odt"
    assert urlio.fetch("https://files.example/x.odt", dest) == 9
    assert dest.read_bytes() == b"odt bytes"


def test_fetch_enforces_the_size_cap(workspace, tmp_path, mock_transport, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "files.example")
    monkeypatch.setenv("MCP_MAX_DOC_MB", "0.001")
    mock_transport(_ok(b"x" * 5000))
    with pytest.raises(urlio.UrlTransferError, match="limit"):
        urlio.fetch("https://files.example/x.odt", tmp_path / "out.odt")


def test_fetch_surfaces_http_errors(workspace, tmp_path, mock_transport, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "files.example")
    calls = mock_transport.calls
    mock_transport(_recording(calls, b"nope", status=404))
    with pytest.raises(urlio.UrlTransferError, match="404"):
        urlio.fetch("https://files.example/x.odt", tmp_path / "out.odt")


def test_fetch_rejects_an_empty_body(workspace, tmp_path, mock_transport, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "files.example")
    mock_transport(_ok(b""))
    with pytest.raises(urlio.UrlTransferError, match="empty"):
        urlio.fetch("https://files.example/x.odt", tmp_path / "out.odt")


def test_url_auth_becomes_basic_auth(workspace, tmp_path, mock_transport, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "nextcloud.example")
    calls = mock_transport.calls
    mock_transport(_recording(calls))
    urlio.fetch("https://nextcloud.example/x.odt", tmp_path / "out.odt", url_auth="alex:pw")
    assert calls[0].headers["authorization"].startswith("Basic ")


def test_bearer_env_is_used_when_no_url_auth(workspace, tmp_path, mock_transport, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "files.example")
    monkeypatch.setenv("MCP_URL_BEARER", "tok")
    calls = mock_transport.calls
    mock_transport(_recording(calls))
    urlio.fetch("https://files.example/x.odt", tmp_path / "out.odt")
    assert calls[0].headers["authorization"] == "Bearer tok"


# --- put -----------------------------------------------------------------

def test_put_uploads_the_file(workspace, tmp_path, mock_transport, monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWED_HOSTS", "nextcloud.example")
    source = tmp_path / "x.odt"
    source.write_bytes(b"payload")
    calls = mock_transport.calls
    mock_transport(_recording(calls, status=201))
    assert urlio.put("https://nextcloud.example/x.odt", source) == 201
    assert calls[0].content == b"payload"


def test_put_refuses_a_blocked_target(workspace, tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_URL_ALLOWED_HOSTS", raising=False)
    source = tmp_path / "x.odt"
    source.write_bytes(b"payload")
    with pytest.raises(urlio.UrlTransferError):
        urlio.put("http://127.0.0.1/x.odt", source)


def test_filename_from_url():
    assert urlio.filename_from_url("https://x.example/a/b/report.odt") == "report.odt"
    assert urlio.filename_from_url("https://x.example/") == "document.odt"
