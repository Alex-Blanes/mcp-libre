FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-core \
        python3-uno \
        python3 \
        python3-venv \
        python3-pip \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# System python3 has the `uno` module (via python3-uno); the server's own
# dependencies (mcp, fastmcp, pydantic...) live in a separate venv so the two
# never conflict. See src/track_changes.py for how they're bridged.
ENV LIBREOFFICE_PYTHON=/usr/bin/python3
ENV SOFFICE_BIN=/usr/bin/soffice

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -e .

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8765
# Comma-separated extra host:port entries allowed past FastMCP's DNS-rebinding
# check, e.g. your remote server's IP/hostname. Override at `docker run` time.
ENV MCP_ALLOWED_HOSTS=""

# --- document transfer ---------------------------------------------------
# Uploaded/produced documents live here as short-lived handles (doc_id), so
# clients never have to push base64 through the MCP channel. Mount a volume if
# you want them to survive a container restart.
ENV MCP_WORKSPACE=/data/workspace
ENV MCP_DOC_TTL=3600
ENV MCP_MAX_DOC_MB=50
# The URL clients reach this server on; without it download_url fields are null.
ENV MCP_PUBLIC_URL=""
# MCP_UPLOAD_TOKEN is deliberately NOT declared here — pass it at run time
# (-e MCP_UPLOAD_TOKEN=...) so it never gets baked into an image layer. Leaving
# it unset means the /files store is OPEN to anyone who can reach the port.
# Allowlist for document_url/target_url fetches (comma-separated hosts).
ENV MCP_URL_ALLOWED_HOSTS=""

VOLUME ["/data"]

EXPOSE 8765

ENTRYPOINT ["/app/.venv/bin/python", "src/main.py"]
