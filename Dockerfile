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

EXPOSE 8765

ENTRYPOINT ["/app/.venv/bin/python", "src/main.py"]
