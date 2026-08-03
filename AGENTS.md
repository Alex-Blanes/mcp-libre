# mcp-libre — LibreOffice MCP Server

MCP server for LibreOffice document operations (Writer, Calc, Impress) deployable via Docker anywhere.

## Connection

The MCP server is configured in `~/.opencode/opencode.json`:

```json
{
  "mcp": {
    "libreoffice": {
      "type": "remote",
      "url": "http://your-server:8765/mcp"
    }
  }
}
```

## Before using any tool (READ FIRST)

This MCP runs remotely (Docker on Linux). It has **no access to your local machine's
filesystem**, so `path` only ever refers to the *server's* disk.

Get the document to the server one of these ways, best first:

| # | Way in | When |
|---|---|---|
| 1 | **`doc_id`** | Default. Upload once over plain HTTP, then work by handle |
| 2 | **`document_url`** | The file already has a URL the server can reach (Nextcloud/WebDAV) |
| 3 | **`path`** | The file is on the server's own filesystem |
| 4 | **`document_base64`** | Last resort: the client cannot make an HTTP request itself |

### Why not base64

A base64 blob travels *through the model's context*: ~1.33 bytes of context per byte
of document, on **every call**. A 100 KB .odt is ~35k tokens, and a create → edit →
convert chain pays it three times. Handles cost ~32 characters.

## The doc_id workflow

```bash
# 1. Upload once — plain HTTP, outside the MCP channel, zero tokens
curl -H "Authorization: Bearer $MCP_UPLOAD_TOKEN" \
     --data-binary @informe.odt \
     "http://your-server:8765/files?filename=informe.odt"
# → {"doc_id":"8c146cac38b44ee58e873b54c70a584f", "download_url":"...", ...}

# 2. Work by handle over MCP. Every tool returns a NEW doc_id, so steps chain:
#      insert_text_at_position(doc_id="8c14…")      → doc_id "9a93…"
#      convert_document(doc_id="9a93…", target_format="pdf") → doc_id "7acb…"
#    The source is never modified; each step produces a new handle.

# 3. Download the result
curl -H "Authorization: Bearer $MCP_UPLOAD_TOKEN" \
     -O -J "http://your-server:8765/files/7acbd502966e40f3af63b2aa2f85bf7f"
```

Handles expire after `MCP_DOC_TTL` (default 1h). `list_stored_documents()` shows what's
live; `delete_document(doc_id)` drops one early.

### HTTP endpoints

| Route | Method | Purpose |
|---|---|---|
| `/files?filename=x.odt` | POST | Upload raw body (or multipart) → `doc_id` |
| `/files/{doc_id}` | GET | Download |
| `/files/{doc_id}` | DELETE | Drop the handle |
| `/health` | GET | Liveness (never requires a token) |

`/files*` requires `Authorization: Bearer $MCP_UPLOAD_TOKEN` when that variable is set.
**If it isn't set, the store is open to anyone who can reach the port.**

## The URL workflow

No upload step at all — the server moves the bytes itself:

```python
# Read a document straight from Nextcloud
await client.call_tool("read_document_text", {
    "document_url": "https://nextcloud.example/remote.php/dav/files/user/informe.odt",
    "url_auth": "user:app-password",
})

# Convert and write the result back over WebDAV
await client.call_tool("convert_document", {
    "document_url": "https://nextcloud.example/remote.php/dav/files/user/informe.odt",
    "target_format": "pdf",
    "target_url": "https://nextcloud.example/remote.php/dav/files/user/informe.pdf",
    "url_auth": "user:app-password",
})
```

`fetch_document(document_url)` ingests a URL into a `doc_id` without doing anything else.

Fetches are guarded: set `MCP_URL_ALLOWED_HOSTS` for a strict allowlist. With it unset,
loopback/link-local/reserved addresses are refused and everything else is allowed.

## Available tools

Every tool below accepts `doc_id`, `document_url`, `path` and `document_base64` as input
(plural `doc_ids` / `document_urls` / `documents_base64` where it takes a list).

| Tool | Output | Description |
|---|---|---|
| `get_server_info` | info + `transfer` block | Platform, LO version, and how to send documents |
| `create_document` | `doc_id` | Create a new document |
| `read_document_text` | plain text | Extract text |
| `convert_document` | `doc_id` | Convert format |
| `get_document_info` | metadata | Document info |
| `read_spreadsheet_data` | CSV data | Read spreadsheet |
| `insert_text_at_position` | `doc_id` | Insert text (start/end/replace) |
| `insert_tracked_text` | `doc_id` | Insert as a tracked change |
| `get_document_statistics` | stats | Document statistics |
| `search_documents` | results | Search text in documents |
| `merge_text_documents` | `doc_id` | Merge documents |
| `fetch_document` | `doc_id` | Pull a URL into server-side storage |
| `list_stored_documents` / `delete_document` | — | Manage handles |
| `batch_convert_documents` | paths | Server-side directory conversion |
| `open_document_in_libreoffice`, `refresh_document_in_libreoffice`, `watch_document_changes`, `create_live_editing_session` | — | Server-side GUI only; `path` only |

Tools that produce a document write it wherever you ask:

| You pass | You get |
|---|---|
| nothing | `doc_id` + `download_url` (default) |
| `target_url` | the file PUT to that URL |
| `target_path` / `output_path` | written to the **server's** filesystem |
| `return_base64=True` | `result_base64` inline (expensive) |

## Remote Deployment (Docker)

```bash
docker compose up -d          # see docker-compose.yml
```

or by hand:

```bash
docker build -t mcp-libre .
docker run -d --name mcp-libre -p 8765:8765 \
  -v ./data:/data \
  -e MCP_ALLOWED_HOSTS="your-server:8765" \
  -e MCP_PUBLIC_URL="http://your-server:8765" \
  -e MCP_UPLOAD_TOKEN="$(openssl rand -hex 24)" \
  --restart unless-stopped \
  mcp-libre
```

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `streamable-http` to serve over the network |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8765` | Bind address |
| `MCP_ALLOWED_HOSTS` | — | **Required remotely.** `host:port` entries allowed past FastMCP's DNS-rebinding check |
| `MCP_PUBLIC_URL` | — | URL clients reach the server on; without it `download_url` is null |
| `MCP_UPLOAD_TOKEN` | — | Bearer token for `/files`. Unset = open store |
| `MCP_WORKSPACE` | temp dir | Where handles are stored |
| `MCP_DOC_TTL` | `3600` | Handle lifetime in seconds (`0` = never expire) |
| `MCP_MAX_DOC_MB` | `50` | Size cap for uploads and URL fetches |
| `MCP_URL_ALLOWED_HOSTS` | — | Allowlist for `document_url` / `target_url` |
| `MCP_URL_BEARER` | — | Bearer token sent with URL fetches |
| `SOFFICE_BIN` | — | Explicit path to `soffice` when it isn't on `PATH` |
| `LIBREOFFICE_PYTHON` | — | LibreOffice's bundled Python (needed by `insert_tracked_text`) |

## Local Development

```bash
uv run pytest                 # unit tests, no LibreOffice needed
python src/main.py --test     # functional smoke test
python tests/test_client.py   # drive it as an MCP client
```
