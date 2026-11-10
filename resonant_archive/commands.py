"""Command functions for the CLI to wire up (cli.py belongs to the CLI lane; nothing here prints or exits).

    init(dsn)                      create extensions + apply migrations in an existing Postgres
    init_docker(project_dir)       write .env (random password), start docker compose, save the DSN, run init
    doctor(dsn, fix)               list of db.Check; db.failed(checks) are the failures
    list_importers()               [(name, description, default source)]
    detect_importer(path)          names of importers that can read a path
    import_path(importer, path, ...)  run an import, returns pipeline.ImportReport
    stats(dsn)                     counts by source and speaker, pending embeddings, memories

Every function raises ``config.ConfigError`` / ``db.DatabaseError`` with a message fit to show a person.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import config, db, fsutil, importers, pipeline
from .importers.base import Options

ENV_PASSWORD = "POSTGRES_PASSWORD"
ENV_PORT = "RESONANT_ARCHIVE_PORT"


def init(dsn: str | None = None, *, create_extensions: bool = True, readonly: bool = True,
         save_readonly: bool = True) -> db.InitResult:
    """Prepare an existing Postgres (bring your own). Idempotent.

    ``readonly`` also creates the ``archive_reader`` login (SELECT only) and, with ``save_readonly``, writes its
    address to the config file as ``readonly_dsn`` so the MCP server and recall use it. The owner's address is not
    touched."""
    target = config.resolve_dsn(dsn)
    keep = config.get("database", "readonly_dsn", config.ENV_READONLY_DSN)
    result = db.init(target, create_extensions=create_extensions, reader=readonly and save_readonly,
                     keep_readonly=str(keep) if keep else None)
    if result.reader and result.reader.dsn and result.reader.dsn != keep:
        config.write_dsn(None, readonly_dsn=result.reader.dsn)
    return result


#: Passwords nobody should keep: the .env.example placeholder, common stand-ins, anything short.
MIN_PASSWORD_LENGTH = 16
_PLACEHOLDERS = ("change-me", "changeme", "change_me", "your-password", "yourpassword", "example", "password",
                 "secret", "postgres", "resonant", "<", "xxxx")


def weak_password(pw: str | None) -> bool:
    """True for a missing, short or placeholder password (the value shipped in .env.example, for one)."""
    if not pw or len(pw) < MIN_PASSWORD_LENGTH:
        return True
    low = pw.lower()
    return any(p in low for p in _PLACEHOLDERS)


def _read_env(env: Path) -> dict[str, str]:
    try:
        text = env.read_text(encoding="utf-8")
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#"))


def env_file_state(project_dir: Path) -> str:
    """``missing`` (no .env), ``weak`` (its password is a placeholder or too short) or ``ok``."""
    env = Path(project_dir) / ".env"
    if not env.exists():
        return "missing"
    return "weak" if weak_password(_read_env(env).get(ENV_PASSWORD)) else "ok"


def write_env_file(project_dir: Path, port: int = config.DEFAULT_PORT, *, overwrite: bool = False) -> tuple[Path, str]:
    """Write ``<project_dir>/.env`` (mode 0600) with a random database password. An existing file is kept (and its
    mode set to 0600) unless ``overwrite``, or its password is missing, too short or a placeholder such as the one in
    .env.example: then a new password replaces it and the other lines stay. Returns (path, password)."""
    env = Path(project_dir) / ".env"
    other: dict[str, str] = {}
    if env.exists() and not overwrite:
        values = _read_env(env)
        if not weak_password(values.get(ENV_PASSWORD)):
            fsutil.make_private(env)
            return env, values[ENV_PASSWORD]
        other = {k: v for k, v in values.items() if k != ENV_PASSWORD}
    other.setdefault(ENV_PORT, str(port))
    password = secrets.token_urlsafe(24)
    fsutil.write_private_text(env, f"{ENV_PASSWORD}={password}\n" + "".join(f"{k}={v}\n" for k, v in other.items()))
    return env, password


def default_project_dir() -> Path:
    """Where ``init --docker`` keeps docker-compose.yml and .env when --project-dir is not given: the data folder,
    not the current directory (a stray .env in a repository is how passwords get committed)."""
    new = config.data_dir() / "docker"
    old = Path.home() / ".local" / "share" / config.APP / "docker"   # where Windows kept it before it had its own folder
    return old if fsutil.WINDOWS and not new.exists() and old.exists() else new


def prepare_project_dir(project_dir: Path) -> Path:
    """Create the folder (mode 0700 when new) and return it resolved."""
    project_dir = Path(project_dir).expanduser()
    if not project_dir.exists():
        project_dir.mkdir(parents=True, mode=0o700)
        fsutil.make_private(project_dir, directory=True)
    return project_dir.resolve()


def bundled_compose_files() -> dict[str, str]:
    """The compose file and init SQL shipped inside the package, keyed by their path relative to a project folder."""
    from importlib import resources

    data = resources.files("resonant_archive") / "data"
    return {
        "docker-compose.yml": (data / "docker-compose.yml").read_text(encoding="utf-8"),
        "docker/init-extensions.sql": (data / "init-extensions.sql").read_text(encoding="utf-8"),
    }


def ensure_compose_files(project_dir: str | Path) -> bool:
    """Make sure ``project_dir`` has docker-compose.yml (and the init SQL it mounts). If it has no compose file, write
    the copies bundled in the package, so a pip install works without a clone. Never overwrites an existing file.
    Returns True when the bundled copies were written."""
    project_dir = Path(project_dir)
    if (project_dir / "docker-compose.yml").exists():
        return False
    try:
        for rel, text in bundled_compose_files().items():
            target = project_dir / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise config.ConfigError(f"could not write the bundled docker-compose.yml into {project_dir}: {exc}") from None
    return True


def docker_dsn(password: str, port: int = config.DEFAULT_PORT) -> str:
    return f"postgresql://{config.DEFAULT_USER}:{password}@127.0.0.1:{port}/{config.DEFAULT_DB}"


def init_docker(project_dir: str | Path, *, port: int = config.DEFAULT_PORT, wait_seconds: int = 90) -> db.InitResult:
    """Bring up the bundled pgvector container (docker-compose.yml in ``project_dir``), save the DSN to the config
    file and run ``init``. Needs docker with the compose plugin."""
    project_dir = Path(project_dir)
    ensure_compose_files(project_dir)
    if shutil.which("docker") is None:
        raise config.ConfigError("docker was not found on PATH. Install it, or use your own Postgres: "
                                 "`resonant-archive init --dsn postgresql://...` (see docs/SCHEMA.md)")
    _, password = write_env_file(project_dir, port)
    up = subprocess.run(["docker", "compose", "up", "-d", "--wait"], cwd=project_dir, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", creationflags=fsutil.NO_WINDOW)
    if up.returncode != 0:
        raise db.DatabaseError("docker compose up failed: " + " ".join(up.stderr.split())[:400])
    dsn = docker_dsn(password, port)
    deadline = time.time() + wait_seconds
    while True:
        try:
            db.connect(dsn).close()
            break
        except db.DatabaseError:
            if time.time() > deadline:
                raise
            time.sleep(1)
    keep = config.get("database", "readonly_dsn", config.ENV_READONLY_DSN)
    result = db.init(dsn, reader=True, keep_readonly=str(keep) if keep else None)
    config.write_dsn(dsn, readonly_dsn=result.reader.dsn if result.reader and result.reader.dsn else None)
    return result


def doctor(dsn: str | None = None, *, fix: bool = False, extra_checks=()) -> list[db.Check]:
    return db.doctor(dsn, fix=fix, extra_checks=extra_checks)


def list_importers() -> list[tuple[str, str, str]]:
    return [(n, i.description, i.default_source) for n, i in sorted(importers.available().items())]


def detect_importer(path: str | Path) -> list[str]:
    return importers.detect(path)


def import_path(
    importer: str,
    path: str | Path,
    *,
    dsn: str | None = None,
    source: str | None = None,
    timezone: str | None = None,
    date_order: str = "dmy",
    projects: list[str] | None = None,
    known_secrets: list[str] = (),  # type: ignore[assignment]
    json_configs: list[str] = (),  # type: ignore[assignment]
    force: bool = False,
    rebuild: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    confirm: Callable[[int, int], bool] | None = None,
    extra: dict | None = None,
    progress: Callable | None = None,
) -> pipeline.ImportReport:
    """Run one import (``resonant-archive import <importer> <path>``). ``importer='auto'`` picks the single importer
    that detects the path and refuses when none or several do. ``rebuild`` removes rows only with ``yes`` (or an agreeing
    ``confirm``) and never together with ``projects`` (see ``pipeline.run_import``)."""
    if rebuild and projects:
        raise config.ConfigError("--rebuild cannot be combined with --project: a filtered run has not read the rest of "
                                 "the input, so it cannot tell what is missing from it")
    if (yes or dry_run) and not rebuild:
        raise config.ConfigError("--yes and --dry-run only apply to --rebuild")
    if importer == "auto":
        found = importers.detect(path)
        if not found:
            raise config.ConfigError(f"no importer recognises {path}; name one of: {', '.join(sorted(importers.available()))}")
        importer = found[0]
    opts = Options(source=source, timezone=timezone, date_order=date_order, projects=projects, extra=dict(extra or {}))
    return pipeline.run_import(importer, path, dsn=config.resolve_dsn(dsn), options=opts,
                               known_secrets=list(known_secrets), json_configs=list(json_configs), force=force,
                               rebuild=rebuild, yes=yes, dry_run=dry_run, confirm=confirm,
                               progress=progress)


def stats(dsn: str | None = None) -> dict:
    """Plain counts for ``resonant-archive stats``."""
    with db.connect(dsn, readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT source, count(*), count(*) FILTER (WHERE NOT hidden), min(ts), max(ts) "
                    "FROM messages GROUP BY source ORDER BY source")
        by_source = [dict(source=r[0], messages=r[1], visible=r[2], first=r[3], last=r[4]) for r in cur.fetchall()]
        cur.execute("SELECT speaker, count(*) FROM messages GROUP BY speaker ORDER BY speaker")
        by_speaker = dict(cur.fetchall())
        cur.execute("SELECT embed_status, count(*) FROM messages GROUP BY embed_status")
        embed = dict(cur.fetchall())
        cur.execute("SELECT count(*) FROM conversations")
        convs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories")
        mems = cur.fetchone()[0]
    return {"sources": by_source, "speakers": by_speaker, "embedding": embed, "conversations": convs, "memories": mems}
