# LibreOffice MCP Server

MCP server for LibreOffice document operations (Writer, Calc, Impress). Supports
both local (stdio) and remote (streamable-http / Docker) deployment.

## Deployment modes

| Mode | Transport | Use case |
|------|-----------|----------|
| **Local** | stdio | MCP client spawns server as a subprocess on the same machine |
| **Remote** | streamable-http | Server runs in a Docker container or remote host, clients connect over HTTP |

## Base64 stateless pattern

When the server runs remotely (e.g. Docker container), it cannot access the client's
filesystem. All document tools accept `document_base64` / `documents_base64` as an
alternative to filesystem paths, and `return_base64=True` to receive the modified
document as a base64-encoded string.

```
Client (any OS)                Server (Linux / Docker)
─────────────────              ────────────────────────
1. Read local file bytes
2. Encode to base64 ─────────> document_base64=<b64>
                               3. Decode, process with LibreOffice
4. Decode result   <─────────  result_base64=<modified_b64>
5. Write local file
```

This makes the server fully stateless — it never needs access to the client's disk,
cloud storage, or any external filesystem.

## Architecture

### `_resolve_document()`

Unified document resolution used by all tools. Accepts either a filesystem `path`
or `document_base64`. When base64 is provided, writes to a temp file, yields the
path, and auto-cleans on exit.

### Smart error detection

The resolver detects cross-platform path issues at runtime. If a Windows-style path
(`C:\Users\...`) is sent to a Linux server, it returns a descriptive error guiding
the user to `document_base64` mode, instead of a cryptic "Document not found".

### `get_server_info()`

Diagnostic tool that returns the server's platform, LibreOffice version, Docker status,
and operational hints. Useful as a first step when something fails.

## What this fork adds (diff from upstream)

*This fork builds on [patrup/mcp-libre](https://github.com/patrup/mcp-libre)*

### New features

- **Remote transport mode**: `MCP_TRANSPORT=streamable-http` support with DNS-rebinding
  bypass for non-localhost deployments
- **Base64 everywhere**: all document tools accept `document_base64` + `return_base64`
- **Smart error messages**: Windows-path detection on Linux/Docker with actionable guidance
- **`get_server_info()` tool**: platform, LO version, Docker status, hints for debugging
- **Tracked changes**: `insert_tracked_text()` inserts text as LibreOffice reviewable
  tracked changes via UNO API, stateless (base64 in/out)
- **`DocResult` model**: structured success/error responses with optional `result_base64`
  field for remote-safe document mutation
- **Path normalization**: automatic backslash-to-forward-slash conversion

### Infrastructure

- **Dockerfile**: Debian Bookworm with LO Writer + UNO Python + separate venv for MCP deps
- **UNO bridge** (`src/track_changes.py` + `scripts/uno/`): subprocess-based bridge
  between the MCP server's venv (no `uno`) and LibreOffice's bundled Python

## Tools

| Tool | base64 input | base64 output | Description |
|------|:-----------:|:------------:|-------------|
| `get_server_info` | — | — | Server environment info |
| `create_document` | `return_base64=True` | `result_base64` | Create new document |
| `read_document_text` | `document_base64` | — (text) | Extract text |
| `convert_document` | `document_base64` | `result_base64` | Convert format |
| `get_document_info` | `document_base64` | — (metadata) | Document metadata |
| `read_spreadsheet_data` | `document_base64` | — (CSV) | Read spreadsheet |
| `insert_text_at_position` | `document_base64` | `result_base64` | Insert text |
| `insert_tracked_text` | `document_base64` | `result_base64` | Tracked change insertion |
| `get_document_statistics` | `document_base64` | — (stats) | Word/char counts |
| `search_documents` | `documents_base64` | — (matches) | Search in documents |
| `merge_text_documents` | `documents_base64` | `result_base64` | Merge documents |
| `batch_convert_documents` | — | — | Batch format conversion |

### GUI tools (local/stdio only)

These tools require a local LibreOffice GUI and are **not available in remote mode**:

- `open_document_in_libreoffice` — Open document in LO for live viewing
- `create_live_editing_session` — Live editing with auto-refresh
- `watch_document_changes` — Monitor document changes
- `refresh_document_in_libreoffice` — Force document reload

## Quick start

### Local (stdio)

```bash
pip install -e .
python src/main.py
```

### Remote (Docker)

```bash
docker build -t mcp-libre .
docker run -d --name mcp-libre \
  -p 8765:8765 \
  -e MCP_ALLOWED_HOSTS="your-host:8765" \
  --restart unless-stopped \
  mcp-libre
```

Configure your MCP client:

```json
{
  "mcp": {
    "libreoffice": {
      "type": "remote",
      "url": "http://your-host:8765/mcp"
    }
  }
}
```

## Usage examples

### Read a document (remote mode)

```python
import base64

with open("document.odt", "rb") as f:
    doc_b64 = base64.b64encode(f.read()).decode()

# Call via MCP client
result = await client.call_tool("read_document_text", {
    "document_base64": doc_b64
})
print(result.content)
```

### Insert tracked changes

```python
result = await client.call_tool("insert_tracked_text", {
    "document_base64": doc_b64,
    "anchor_text": "Existing paragraph text",
    "new_text": "New text to insert as tracked change",
    "author": "Jane Doe",
    "insert_mode": "after"
})

if result.structuredContent["success"]:
    with open("modified.odt", "wb") as f:
        f.write(base64.b64decode(result.structuredContent["document_base64"]))
```

## Roadmap

- **Feature gating**: disable GUI tools when running in streamable-http mode
- **Health endpoint**: `/health` HTTP endpoint for Docker health checks
- **Remote config templates**: generate config for `type: remote` deployments
- **Fix `track_changes.py` default**: default `LIBREOFFICE_PYTHON` to `/usr/bin/python3` instead of Windows path
- **English error messages**: translate current Spanish error strings to English
- **Authentication**: optional API key / token for deployments beyond private networks

## Requirements

- **LibreOffice**: 24.2+ (7.4+ works via Docker)
- **Python**: 3.11+ (3.12+ for local development)
- **Docker** (optional): for remote deployment

## Development

```bash
# Install deps
pip install -e .

# Run tests
python src/main.py --test

# Run server
python src/main.py
```

## License

MIT — see [LICENSE](LICENSE).
