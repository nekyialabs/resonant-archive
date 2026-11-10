"""Where the embedder keeps its model files and what it listens on.

Everything here is overridable by environment variable; nothing is read from the
repository tree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MODEL_DIRNAME = "nomic-embed-text-v1.5"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8899


def data_dir() -> Path:
    """Data dir for resonant-archive: ``~/.local/share/resonant-archive`` on Linux, ``%LOCALAPPDATA%/resonant-archive``
    on Windows."""
    override = os.environ.get("RESONANT_ARCHIVE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        from ..config import data_dir as _windows_data_dir   # one answer for the CLI and the embedder

        return _windows_data_dir()
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "resonant-archive"


def model_dir() -> Path:
    """Directory holding model.onnx, tokenizer.json and friends."""
    override = os.environ.get("RESONANT_EMBEDDER_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    return data_dir() / "models" / MODEL_DIRNAME


def server_host() -> str:
    return os.environ.get("RESONANT_EMBEDDER_HOST", DEFAULT_HOST)


def server_port() -> int:
    return int(os.environ.get("RESONANT_EMBEDDER_PORT", str(DEFAULT_PORT)))


def server_url() -> str:
    """Base URL clients use to reach the embed server."""
    override = os.environ.get("RESONANT_EMBEDDER_URL")
    if override:
        return override.rstrip("/")
    host = server_host()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"http://{host}:{server_port()}"
