"""Shared fixtures. Database tests run against a real Postgres with pgvector, never a mock.

Where the Postgres comes from, in order:

1. ``RESONANT_ARCHIVE_TEST_DSN``: an admin-capable DSN (CI's pgvector service container, or the bundled compose
   database). Each test gets its own throwaway database created on that server.
2. A throwaway local cluster started with ``initdb`` / ``pg_ctl`` on a unix socket in a temp directory (needs the
   Postgres server binaries and the pgvector extension files installed on the machine).
3. Neither available: the database tests are skipped, loudly, with the reason.
"""

from __future__ import annotations

import glob
import itertools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ENV_TEST_DSN = "RESONANT_ARCHIVE_TEST_DSN"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """No test may read or write the real user config or point at a real database."""
    for var in ("RESONANT_ARCHIVE_DSN", "RESONANT_ARCHIVE_READONLY_DSN", "RESONANT_ARCHIVE_CONFIG",
                "RESONANT_ARCHIVE_DATA_DIR", "RESONANT_EMBEDDER_MODEL_DIR", "PGCONNECT_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))            # Path.home() on Windows reads this, not HOME
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))


def _find_pg_bin() -> Path | None:
    initdb = shutil.which("initdb")
    if initdb:
        return Path(initdb).parent
    for cand in sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"), reverse=True):
        return Path(cand).parent
    for cand in sorted(glob.glob("/usr/local/pgsql*/bin/initdb") + glob.glob("/opt/homebrew/opt/postgresql*/bin/initdb")):
        return Path(cand).parent
    return None


@pytest.fixture(scope="session")
def pg_admin_dsn():
    """Admin DSN of a Postgres usable for tests (see module docstring)."""
    env = os.environ.get(ENV_TEST_DSN)
    if env:
        yield env
        return
    binroot = _find_pg_bin()
    if binroot is None:
        pytest.skip(f"no Postgres for database tests: set {ENV_TEST_DSN} or install the Postgres server binaries")
    base = Path(tempfile.mkdtemp(prefix="ra-pg-"))
    data = base / "data"
    sock = base / "sock"
    sock.mkdir()
    port = 55000 + os.getpid() % 5000
    subprocess.run([str(binroot / "initdb"), "-D", str(data), "-A", "trust", "-E", "UTF8", "--no-sync", "-U", "postgres"],
                   check=True, capture_output=True)
    opts = f"-c listen_addresses='' -c unix_socket_directories={sock} -p {port} -c fsync=off -c max_connections=60"
    started = subprocess.run([str(binroot / "pg_ctl"), "-D", str(data), "-w", "-l", str(base / "log"), "-o", opts, "start"],
                             capture_output=True, text=True)
    if started.returncode != 0:
        log = (base / "log").read_text() if (base / "log").exists() else ""
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip(f"could not start a throwaway Postgres: {started.stderr.strip()[:300]} {log[-300:]}")
    dsn = f"host={sock} port={port} user=postgres dbname=postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            avail = {r[0] for r in c.execute("SELECT name FROM pg_available_extensions").fetchall()}
        if not {"vector", "pg_trgm"} <= avail:
            pytest.skip(f"the local Postgres lacks pgvector/pg_trgm (has {sorted(avail & {'vector', 'pg_trgm'})})")
        yield dsn
    finally:
        subprocess.run([str(binroot / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


_counter = itertools.count(1)


@pytest.fixture
def blank_dsn(pg_admin_dsn):
    """A new empty database (no extensions, no schema). Dropped after the test."""
    name = f"t_{os.getpid()}_{next(_counter)}"
    with psycopg.connect(pg_admin_dsn, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(pg_admin_dsn, dbname=name)
    try:
        yield dsn
    finally:
        with psycopg.connect(pg_admin_dsn, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def dsn(blank_dsn):
    """A new database with the schema applied."""
    from resonant_archive import db

    result = db.init(blank_dsn)
    assert result.ok, result
    return blank_dsn


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn) as c:
        yield c


def dsn_parts(dsn: str) -> dict:
    return conninfo_to_dict(dsn)
