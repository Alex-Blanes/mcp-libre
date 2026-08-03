"""Shared fixtures. Puts src/ on the path the same way the entry point does."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Script-style demos, not pytest tests: they drive a live server, need LibreOffice
# installed, and write to fixed /tmp paths. Run them directly instead:
#   python tests/test_client.py
collect_ignore = ["test_client.py"]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point the document store at a throwaway directory for one test."""
    import docstore

    root = tmp_path / "workspace"
    monkeypatch.setenv("MCP_WORKSPACE", str(root))
    monkeypatch.delenv("MCP_DOC_TTL", raising=False)
    monkeypatch.delenv("MCP_MAX_DOC_MB", raising=False)
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    return root
