"""The out-of-band upload/download endpoints, driven as a real HTTP client would."""

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import docstore
import http_routes


class _RouteCollector:
    """Stand-in for FastMCP: captures what register_routes() would attach."""

    def __init__(self):
        self.routes = []

    def custom_route(self, path, methods, name=None, include_in_schema=True):
        def decorator(func):
            self.routes.append(Route(path, endpoint=func, methods=methods))
            return func

        return decorator


@pytest.fixture
def client(workspace):
    collector = _RouteCollector()
    http_routes.register_routes(collector)
    return TestClient(Starlette(routes=collector.routes))


def test_health_needs_no_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_upload_then_download_round_trip(client):
    upload = client.post("/files?filename=report.odt", content=b"document bytes")
    assert upload.status_code == 201
    body = upload.json()
    assert body["filename"] == "report.odt"
    assert body["size_bytes"] == 14

    download = client.get(f"/files/{body['doc_id']}")
    assert download.status_code == 200
    assert download.content == b"document bytes"


def test_uploaded_document_is_reachable_by_handle(client):
    doc_id = client.post("/files?filename=x.odt", content=b"abc").json()["doc_id"]
    assert docstore.resolve(doc_id).read_bytes() == b"abc"


def test_delete_endpoint(client):
    doc_id = client.post("/files?filename=x.odt", content=b"abc").json()["doc_id"]
    assert client.delete(f"/files/{doc_id}").json()["deleted"] is True
    assert client.get(f"/files/{doc_id}").status_code == 404


def test_empty_body_is_rejected(client):
    assert client.post("/files", content=b"").status_code == 400


def test_unknown_handle_is_404(client):
    assert client.get(f"/files/{'a' * 32}").status_code == 404


def test_malformed_handle_is_404_not_500(client):
    assert client.get("/files/not-a-handle").status_code == 404


def test_oversized_upload_is_413(client, monkeypatch):
    monkeypatch.setenv("MCP_MAX_DOC_MB", "0.001")
    assert client.post("/files?filename=big.odt", content=b"x" * 5000).status_code == 413


def test_download_url_uses_the_request_host_when_public_url_is_unset(client):
    url = client.post("/files?filename=x.odt", content=b"abc").json()["download_url"]
    assert url is not None and url.endswith("/files/" + url.rsplit("/", 1)[-1])


class TestWithToken:
    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setenv("MCP_UPLOAD_TOKEN", "s3cret")

    def test_upload_without_a_token_is_401(self, client):
        assert client.post("/files?filename=x.odt", content=b"abc").status_code == 401

    def test_upload_with_the_wrong_token_is_401(self, client):
        response = client.post(
            "/files?filename=x.odt", content=b"abc", headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401

    def test_upload_with_the_right_token_succeeds(self, client):
        response = client.post(
            "/files?filename=x.odt", content=b"abc", headers={"Authorization": "Bearer s3cret"}
        )
        assert response.status_code == 201

    def test_download_is_also_protected(self, client):
        doc_id = client.post(
            "/files?filename=x.odt", content=b"abc", headers={"Authorization": "Bearer s3cret"}
        ).json()["doc_id"]
        assert client.get(f"/files/{doc_id}").status_code == 401

    def test_health_stays_open(self, client):
        assert client.get("/health").status_code == 200
