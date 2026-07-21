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

This MCP runs remotely (Docker on Linux). It has no access to your local machine's filesystem.

### Golden rule

If the document is on your local machine → **Do NOT use `path`**. Always use `document_base64`.

### What to do if you see "Document not found"

1. **Quick diagnosis**: call `get_server_info()` to verify platform and LO version
2. **Most common cause**: you passed a local path to the remote Linux server
3. **Solution**: read the local file with your local MCP (e.g. FileSystem tool), encode it to base64, and pass it as `document_base64=<b64_content>`

## Base64 pattern (stateless) — Full workflow

All tools accept `document_base64` / `documents_base64` as an alternative to file paths. The server never accesses the client's local filesystem — documents travel as base64 in the request/response.

### Typical workflow with OpenCode / Claude Code

```
You have a document at ~/document.odt

STEP 1: Read the local file
  → Use your local MCP (e.g. FileSystem) to read the file bytes

STEP 2: Encode to base64
  → Convert the bytes to base64

STEP 3: Call the MCP tool with document_base64
  → Pass the encoded content as document_base64=<b64>

STEP 4: Save the result (if the tool returns base64)
  → Decode result_base64 and write to the local file
```

### Example: read text from a document

```python
import base64

# 1. Read local file
with open("~/document.odt", "rb") as f:
    doc_b64 = base64.b64encode(f.read()).decode("ascii")

# 2. Call the MCP
result = await client.call_tool("read_document_text", {
    "document_base64": doc_b64
})

# Result is plain text
print(result.content)
```

### Example: modify and save

```python
import base64

# 1. Read local file
with open("~/document.odt", "rb") as f:
    doc_b64 = base64.b64encode(f.read()).decode("ascii")

# 2. Insert text as a tracked change
result = await client.call_tool("insert_tracked_text", {
    "document_base64": doc_b64,
    "anchor_text": "Existing text",
    "new_text": "New text to insert",
    "author": "First Last",
    "insert_mode": "after"
})

# 3. Save the modified document
if result.structuredContent.get("success"):
    modified_b64 = result.structuredContent["document_base64"]
    with open("~/document_modified.odt", "wb") as f:
        f.write(base64.b64decode(modified_b64))
```

## Available tools

| Tool | Input base64 | Output base64 | Description |
|---|---|---|---|
| `get_server_info` | — | — | Server info (OS, LO version, Docker, hints) |
| `create_document` | `return_base64=True` | `result_base64` | Create a new document |
| `read_document_text` | `document_base64` | — (plain text) | Extract text |
| `convert_document` | `document_base64` | `result_base64` | Convert format |
| `get_document_info` | `document_base64` | — (metadata) | Document info |
| `read_spreadsheet_data` | `document_base64` | — (CSV data) | Read spreadsheet |
| `insert_text_at_position` | `document_base64` | `result_base64` | Insert text (start/end/replace) |
| `insert_tracked_text` | `document_base64` | `result_base64` | Insert as tracked change |
| `get_document_statistics` | `document_base64` | — (stats) | Document statistics |
| `search_documents` | `documents_base64` | — (results) | Search text in documents |
| `merge_text_documents` | `documents_base64` | `result_base64` | Merge documents |
| `open_document_in_libreoffice` | `document_base64` | — | Open in LO GUI (persistent tmp) |
| `create_live_editing_session` | `document_base64` | — | Live editing session |

## Remote Deployment (Docker)

```bash
# Build
docker build -t mcp-libre .

# Run
docker run -d \
  --name mcp-libre \
  -p 8765:8765 \
  -e MCP_ALLOWED_HOSTS="your-server:8765" \
  --restart unless-stopped \
  mcp-libre

# Logs
docker logs mcp-libre

# Stop/remove
docker stop mcp-libre && docker rm mcp-libre
```

## Local Development

```bash
# Activate venv and test
cd src
python main.py --test

# Or via MCP test client
cd tests
python test_client.py
```
