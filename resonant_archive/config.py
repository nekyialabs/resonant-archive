"""Configuration: where the database is, and where files live (XDG paths).

The database address (DSN) is never read from a file inside the repository. Resolution order:

1. an explicit argument,
2. the environment variable ``RESONANT_ARCHIVE_DSN`` (``RESONANT_ARCHIVE_READONLY_DSN`` for the read-only role),
3. ``$XDG_CONFIG_HOME/resonant-archive/config.toml`` (default ``~/.config/resonant-archive/config.toml``)::

       [database]
       dsn = "postgresql://owner:password@127.0.0.1:54329/resonant_archive"
       readonly_dsn = "postgresql://reader:password@127.0.0.1:54329/resonant_archive"

The file is written with mode 0600 because a DSN carries a password.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from . import fsutil

APP = "resonant-archive"

ENV_DSN = "RESONANT_ARCHIVE_DSN"
ENV_READONLY_DSN = "RESONANT_ARCHIVE_READONLY_DSN"
ENV_CONFIG = "RESONANT_ARCHIVE_CONFIG"

#: Host port the bundled docker-compose.yml publishes (loopback only). Chosen to stay clear of a local 5432.
DEFAULT_PORT = 54329
DEFAULT_DB = "resonant_archive"
DEFAULT_USER = "resonant"


class ConfigError(RuntimeError):
    """Raised when something the caller needs (usually the DSN) is not configured."""


def _xdg(var: str, fallback: str) -> Path:
    base = os.environ.get(var)
    if base and os.path.isabs(base):
        return Path(base)
    return Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def config_file() -> Path:
    override = os.environ.get(ENV_CONFIG)
    return Path(override).expanduser() if override else config_dir() / "config.toml"


def models_dir() -> Path:
    """Where the embedding model files live. The embedder lane may override with its own variable."""
    return data_dir() / "models"


def load_file(path: Path | None = None) -> dict[str, Any]:
    """The parsed config file, or an empty dict when there is none. A broken file raises ConfigError."""
    p = path or config_file()
    try:
        with open(p, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc


def get(section: str, key: str, env: str | None = None, default: Any = None) -> Any:
    """One setting: environment variable first (when ``env`` is given), then the config file, then ``default``."""
    if env and os.environ.get(env):
        return os.environ[env]
    value = load_file().get(section, {})
    if isinstance(value, dict) and key in value:
        return value[key]
    return default


def resolve_dsn(dsn: str | None = None, *, readonly: bool = False) -> str:
    """The DSN to use. ``readonly=True`` prefers the read-only role's DSN and falls back to the owner's."""
    if dsn:
        return dsn
    if readonly:
        ro = get("database", "readonly_dsn", ENV_READONLY_DSN)
        if ro:
            return str(ro)
    found = get("database", "dsn", ENV_DSN)
    if found:
        return str(found)
    raise ConfigError(
        "no database configured. Set RESONANT_ARCHIVE_DSN, or add [database] dsn = ... to "
        f"{config_file()}, or run `resonant-archive init` to start the bundled Postgres."
    )


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_dsn(dsn: str | None, readonly_dsn: str | None = None, path: Path | None = None) -> Path:
    """Write (or update) the [database] section of the config file, owner-only (mode 0600; an ACL on Windows). Other sections are kept."""
    p = path or config_file()
    existing = load_file(p)
    db = dict(existing.get("database", {}))
    if dsn:
        db["dsn"] = dsn
    if readonly_dsn:
        db["readonly_dsn"] = readonly_dsn
    existing["database"] = db
    lines: list[str] = []
    # Flat scalars first, then tables: valid TOML for the simple shapes this file holds.
    for k, v in existing.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {_toml_value(v)}")
    for section, body in existing.items():
        if isinstance(body, dict):
            lines.append("")
            lines.append(f"[{section}]")
            for k, v in body.items():
                lines.append(f"{k} = {_toml_value(v)}")
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fsutil.write_private_text(p, "\n".join(lines).lstrip("\n") + "\n")
    return p


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return _toml_str(str(v))


#: Seconds to wait for a connection when neither the DSN nor PGCONNECT_TIMEOUT says. libpq's own default is to wait
#: forever; on Windows a refused connection (nothing listening yet, Docker still starting) was measured at 130 s
#: before the error came back, so a down database hung the CLI and the MCP server for two minutes.
CONNECT_TIMEOUT_S = 10


def connect_kwargs(dsn: str | None) -> dict[str, int]:
    """``{"connect_timeout": 10}`` for ``psycopg.connect``, or ``{}`` when the DSN or PGCONNECT_TIMEOUT already
    sets one (the person's own value wins)."""
    if os.environ.get("PGCONNECT_TIMEOUT") or "connect_timeout" in (dsn or "").lower():
        return {}
    return {"connect_timeout": CONNECT_TIMEOUT_S}


def redact_dsn(dsn: str) -> str:
    """The DSN with its password hidden, for messages and logs."""
    import re

    dsn = re.sub(r"(://[^:/\s@]*:)[^\s/]*@", r"\1***@", dsn)  # the last '@' before the host ends the password
    # password=..., sslpassword=... (a key-file passphrase), as a keyword or as a URL query parameter
    return re.sub(r"(?i)((?:\b|ssl)password\s*=\s*)('[^']*'|[^\s&]+)", r"\1***", dsn)
