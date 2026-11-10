"""Database layer: connection, extensions, numbered migrations, ``init`` and ``doctor``.

Plain functions only; the command line (cli.py) wires them up. The schema itself is in
``resonant_archive/migrations/*.sql`` and described in docs/SCHEMA.md.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from importlib import resources
from typing import Callable, Iterable

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from . import config

EXTENSIONS = ("vector", "pg_trgm")
MIN_PG_MAJOR = 15
EMBEDDING_DIMS = 768
EXPECTED_TABLES = ("sources", "messages", "conversations", "anchors", "memories", "schema_migrations")

#: Server-side limit for reads through the read-only role (the search engine and MCP server).
READONLY_STATEMENT_TIMEOUT_MS = 10_000

_LOCK_KEY = 0x72657361  # arbitrary, fixed: one migrator at a time


class DatabaseError(RuntimeError):
    """A database problem the caller can show to a person as is (no stack trace needed)."""


# ---------------------------------------------------------------- connection

def connect(
    dsn: str | None = None,
    *,
    readonly: bool = False,
    statement_timeout_ms: int | None = None,
    autocommit: bool = False,
) -> psycopg.Connection:
    """Open a connection.

    ``readonly=True`` resolves the read-only DSN when one is configured and also sets
    ``default_transaction_read_only=on`` plus a 10 second ``statement_timeout`` on the session, so even the
    owner's DSN cannot write through it. Writes are then refused by the server.
    """
    target = config.resolve_dsn(dsn, readonly=readonly)
    opts = []
    if readonly:
        opts.append("-c default_transaction_read_only=on")
        statement_timeout_ms = statement_timeout_ms or READONLY_STATEMENT_TIMEOUT_MS
    if statement_timeout_ms:
        opts.append(f"-c statement_timeout={int(statement_timeout_ms)}")
    try:
        return psycopg.connect(target, autocommit=autocommit, options=" ".join(opts) or None,
                               **config.connect_kwargs(target))
    except psycopg.Error as exc:
        raise DatabaseError(f"cannot connect to {config.redact_dsn(target)}: {_why(exc)}") from exc


def _why(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


# ---------------------------------------------------------------- extensions

def create_extension_sql(name: str) -> str:
    return f"CREATE EXTENSION IF NOT EXISTS {name};"


def installed_extensions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension")
        return {r[0] for r in cur.fetchall()}


def available_extensions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM pg_available_extensions")
        return {r[0] for r in cur.fetchall()}


def ensure_extensions(conn: psycopg.Connection) -> list[str]:
    """Try to create the required extensions. Returns the names still missing afterwards (empty = fine).

    Needs the extension files on the server and, for ``vector``, a role allowed to create it (a superuser, or
    the database owner where the extension is marked trusted). The bundled docker-compose.yml creates both.
    """
    missing: list[str] = []
    have = installed_extensions(conn)
    for name in EXTENSIONS:
        if name in have:
            continue
        try:
            with conn.transaction():
                conn.execute(create_extension_sql(name))
        except psycopg.Error:
            missing.append(name)
    conn.commit()
    return missing


# ---------------------------------------------------------------- migrations

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:16]


_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def migrations() -> list[Migration]:
    """All migrations shipped in the package, in order."""
    out: list[Migration] = []
    root = resources.files("resonant_archive") / "migrations"
    for entry in root.iterdir():
        m = _FILE.match(entry.name)
        if m:
            out.append(Migration(int(m.group(1)), m.group(2), entry.read_text(encoding="utf-8")))
    out.sort(key=lambda x: x.version)
    versions = [m.version for m in out]
    if versions != list(range(1, len(versions) + 1)):
        raise DatabaseError(f"migration files are not numbered 0001, 0002, ... without gaps: {versions}")
    return out


def latest_version() -> int:
    return migrations()[-1].version


_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    integer     PRIMARY KEY,
  name       text        NOT NULL,
  checksum   text        NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def applied_versions(conn: psycopg.Connection) -> dict[int, str]:
    """{version: checksum} of migrations applied, or {} when the bookkeeping table does not exist yet."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
        return {r[0]: r[1] for r in cur.fetchall()}


def migrate(conn: psycopg.Connection) -> list[int]:
    """Apply every pending migration, each in its own transaction. Idempotent. Returns versions applied.

    Takes an advisory lock so two processes cannot migrate at once. Refuses if a migration that was applied has
    since been edited (its checksum changed): write a new numbered file instead.
    """
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    conn.commit()
    applied_now: list[int] = []
    try:
        conn.execute(_BOOTSTRAP)
        conn.commit()
        done = applied_versions(conn)
        conn.commit()
        for mig in migrations():
            if mig.version in done:
                if done[mig.version] != mig.checksum:
                    raise DatabaseError(
                        f"migration {mig.version:04d}_{mig.name} was edited after it was applied "
                        f"(database has checksum {done[mig.version]}, file has {mig.checksum}). "
                        "Leave applied migrations alone and add a new numbered file."
                    )
                continue
            try:
                with conn.transaction():
                    conn.execute(mig.sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (mig.version, mig.name, mig.checksum),
                    )
            except psycopg.Error as exc:
                raise DatabaseError(f"migration {mig.version:04d}_{mig.name} failed: {_why(exc)}") from exc
            conn.commit()
            applied_now.append(mig.version)
    finally:
        try:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            conn.commit()
        except psycopg.Error:
            pass
    return applied_now


# ---------------------------------------------------------------- read-only role

READER_ROLE = "archive_reader"


@dataclass
class ReaderResult:
    """What ``ensure_reader`` did. ``dsn`` is the address of the read-only role (None when it could not be made);
    ``note`` says why, or what happened, in a sentence fit to show a person."""

    dsn: str | None = None
    note: str = ""
    created: bool = False
    rotated: bool = False


def reader_role_name(dbname: str) -> str:
    """``archive_reader`` for the default database; a role name is cluster-wide, so another database on the same
    server gets its own (``archive_reader_<dbname>``) and two archives never share, or rotate, one password."""
    if dbname == config.DEFAULT_DB:
        return READER_ROLE
    return (READER_ROLE + "_" + re.sub(r"[^a-z0-9_]", "_", dbname.lower()))[:60]


def _reader_dsn(conn: psycopg.Connection, owner_dsn: str, role: str, password: str) -> str:
    info = conn.info
    host = info.host or ""
    if host and not host.startswith("/"):
        from urllib.parse import quote

        h = f"[{host}]" if ":" in host else host
        return f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}@{h}:{info.port}/{quote(info.dbname, safe='')}"
    return make_conninfo(owner_dsn, user=role, password=password)  # a unix socket: key=value form


def _reader_works(keep: str, conn: psycopg.Connection, role: str) -> bool:
    """Is ``keep`` already this database's read-only role, with a password that works?"""
    try:
        parts = conninfo_to_dict(keep)
        if parts.get("user") != role or parts.get("dbname") != conn.info.dbname:
            return False
        with psycopg.connect(keep, connect_timeout=5) as c:
            row = c.execute("SELECT current_user, current_database()").fetchone()
        return row == (role, conn.info.dbname)
    except psycopg.Error:
        return False


def ensure_reader(conn: psycopg.Connection, owner_dsn: str, keep: str | None = None) -> ReaderResult:
    """Create (or refresh) the read-only role and return its DSN.

    The role can log in, is not a superuser, may SELECT every table in ``public`` (and every future one) and nothing
    else; ``default_transaction_read_only`` is also set on the role itself. ``keep`` is the address already
    configured: when it still works it is kept, otherwise the role gets a new random password. The password is hashed
    here (SCRAM) before it is sent, so the server's logs never see it. Needs a role allowed to CREATE ROLE; when the
    owner is not, the result says so and nothing changes.
    """
    role = reader_role_name(conn.info.dbname)
    ident, who = sql.Identifier(role), sql.Identifier(conn.info.dbname)
    try:
        conn.rollback()
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None
        reuse = bool(keep) and exists and _reader_works(keep, conn, role)
        password = None
        if not reuse:
            password = secrets.token_urlsafe(24)
            if not exists:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(ident))
            hashed = conn.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256").decode()
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(ident, sql.Literal(hashed)))
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(who, ident))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident))
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(ident))
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        return ReaderResult(note=f"could not set up the read-only role {role}: {_why(exc)}. Create one by hand "
                                 "(docs/SCHEMA.md) and set readonly_dsn in the config file.")
    if reuse:
        return ReaderResult(dsn=keep, note=f"read-only role {role} is ready")
    return ReaderResult(dsn=_reader_dsn(conn, owner_dsn, role, password or ""),
                        note=f"read-only role {role} is ready", created=not exists, rotated=exists)


# ---------------------------------------------------------------- init

@dataclass
class InitResult:
    applied: list[int]
    missing_extensions: list[str] = field(default_factory=list)
    version: int = 0
    reader: ReaderResult | None = None

    @property
    def ok(self) -> bool:
        return not self.missing_extensions and self.version == latest_version()

    def fix_hint(self) -> str:
        return "\n".join(create_extension_sql(n) for n in self.missing_extensions)


def init(dsn: str | None = None, *, create_extensions: bool = True, reader: bool = False,
         keep_readonly: str | None = None) -> InitResult:
    """Create the extensions (when allowed) and apply all migrations. Safe to run any number of times.

    When an extension cannot be created the migrations are not attempted and ``missing_extensions`` names them;
    ``InitResult.fix_hint()`` prints the exact SQL to run as a superuser. ``reader=True`` also creates the read-only
    role (see ``ensure_reader``); ``keep_readonly`` is the read-only address already configured, if any.
    """
    with connect(dsn) as conn:
        missing = ensure_extensions(conn) if create_extensions else sorted(set(EXTENSIONS) - installed_extensions(conn))
        if missing:
            return InitResult(applied=[], missing_extensions=missing, version=max(applied_versions(conn), default=0))
        applied = migrate(conn)
        result = InitResult(applied=applied, version=max(applied_versions(conn), default=0))
        if reader:
            result.reader = ensure_reader(conn, config.resolve_dsn(dsn), keep_readonly)
        return result


# ---------------------------------------------------------------- doctor

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str | None = None
    severity: str = "error"  # error | warn | info; only meaningful when ok is False (info is never a failure)


ExtraCheck = Callable[[psycopg.Connection], Iterable[Check]]


def doctor(dsn: str | None = None, *, fix: bool = False, extra_checks: Iterable[ExtraCheck] = ()) -> list[Check]:
    """Check the database. ``fix=True`` applies what is safe to apply (extensions and pending migrations).

    ``extra_checks`` are callables ``(conn) -> Iterable[Check]``: other parts of the package (embedder, models)
    add their own checks without this module knowing about them.
    """
    checks: list[Check] = []
    try:
        target = config.resolve_dsn(dsn)
    except config.ConfigError as exc:
        return [Check("config", False, str(exc), "set RESONANT_ARCHIVE_DSN or run `resonant-archive init`")]
    try:
        conn = connect(target)
    except DatabaseError as exc:
        return [Check("connection", False, str(exc), "check the server is running and the DSN is right")]
    with conn:
        checks.append(Check("connection", True, config.redact_dsn(target)))
        major = conn.info.server_version // 10000
        checks.append(
            Check("postgres version", major >= MIN_PG_MAJOR, f"server {major}.{conn.info.server_version % 10000}",
                  f"Postgres {MIN_PG_MAJOR} or newer is required")
        )
        if fix:
            ensure_extensions(conn)
        have = installed_extensions(conn)
        avail = available_extensions(conn)
        for name in EXTENSIONS:
            if name in have:
                checks.append(Check(f"extension {name}", True, "installed"))
            elif name in avail:
                checks.append(Check(f"extension {name}", False, "available but not created in this database",
                                    f"run as a superuser in this database: {create_extension_sql(name)}"))
            else:
                checks.append(Check(f"extension {name}", False, "not installed on the server",
                                    "install pgvector / the postgresql contrib package on the server, then "
                                    + create_extension_sql(name)))
        if not (set(EXTENSIONS) <= have):
            checks.append(Check("schema", False, "skipped: required extensions are missing",
                                "fix the extensions above, then run `resonant-archive init`"))
        else:
            if fix:
                migrate(conn)
            checks.extend(_schema_checks(conn))
        checks.append(_reader_check(target))
        for extra in extra_checks:
            try:
                checks.extend(extra(conn))
            except Exception as exc:  # an extra check must never take doctor down
                checks.append(Check(getattr(extra, "__name__", "extra check"), False, f"check crashed: {_why(exc)}"))
    return checks


def _reader_check(owner_dsn: str) -> Check:
    """The MCP server and recall should connect as a role that can only read. Warn when there is none."""
    name = "read-only role"
    fix = "run `resonant-archive init` (it creates the role and saves readonly_dsn), or set readonly_dsn in the config file"
    ro = config.get("database", "readonly_dsn", config.ENV_READONLY_DSN)
    if not ro:
        return Check(name, False, "readonly_dsn is not set: the MCP server and recall connect with the owner's "
                                  "address (the session is read-only, but the login could write)", fix, severity="warn")
    try:
        a, b = conninfo_to_dict(str(ro)), conninfo_to_dict(owner_dsn)
    except psycopg.Error:
        a = b = {}
    if str(ro) == owner_dsn or (a and a.get("user") == b.get("user")):
        return Check(name, False, "readonly_dsn is the same login as dsn, so it is not read-only", fix, severity="warn")
    try:
        with psycopg.connect(str(ro), connect_timeout=5) as c:
            sup, can_write, can_create = c.execute(
                "SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                "has_table_privilege(current_user, 'messages', 'INSERT,UPDATE,DELETE'), "
                "has_schema_privilege(current_user, 'public', 'CREATE')").fetchone()
    except psycopg.Error as exc:
        return Check(name, False, f"cannot connect with readonly_dsn ({config.redact_dsn(str(ro))}): {_why(exc)}", fix,
                     severity="warn")
    if sup or can_write:
        return Check(name, False, "readonly_dsn logs in as a role that can write", fix, severity="warn")
    if can_create:
        # Postgres before 15 lets every role create objects in `public`: a read-only login that can create a table or a
        # function there can shadow the archive's own objects. The grant comes from PUBLIC, so it is revoked there.
        return Check(name, False, "readonly_dsn logs in as a role that can CREATE in schema public (usually inherited "
                                  "from PUBLIC, the default before Postgres 15)",
                     "as the owner or a superuser: REVOKE CREATE ON SCHEMA public FROM PUBLIC;  (see docs/SCHEMA.md)",
                     severity="warn")
    return Check(name, True, "readonly_dsn is a login that can only read")


def _schema_checks(conn: psycopg.Connection) -> list[Check]:
    out: list[Check] = []
    done = applied_versions(conn)
    wanted = migrations()
    pending = [m for m in wanted if m.version not in done]
    edited = [m for m in wanted if m.version in done and done[m.version] != m.checksum]
    if not done:
        out.append(Check("migrations", False, "no migrations applied", "run `resonant-archive init` (or doctor --fix)"))
    elif pending:
        out.append(Check("migrations", False, f"{len(pending)} pending (database at {max(done)}, latest {wanted[-1].version})",
                         "run `resonant-archive init` (or doctor --fix)"))
    else:
        out.append(Check("migrations", True, f"up to date at {wanted[-1].version:04d}"))
    if edited:
        out.append(Check("migration checksums", False,
                         "applied migration(s) changed on disk: " + ", ".join(f"{m.version:04d}" for m in edited),
                         "do not edit applied migrations; add a new numbered file", severity="warn"))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NULL", (list(EXPECTED_TABLES),)
        )
        missing_tables = [r[0] for r in cur.fetchall()]
    if missing_tables:
        out.append(Check("tables", False, "missing: " + ", ".join(missing_tables), "run `resonant-archive init`"))
        return out
    out.append(Check("tables", True, ", ".join(EXPECTED_TABLES)))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'messages'::regclass AND attname = 'embedding'"
        )
        row = cur.fetchone()
        dims = row[0] if row else None
        out.append(Check("embedding column", dims == EMBEDDING_DIMS, f"vector({dims})",
                         f"messages.embedding must be vector({EMBEDDING_DIMS})"))
        cur.execute("SELECT count(*), count(*) FILTER (WHERE embed_status = 'pending') FROM messages")
        total, pending_n = cur.fetchone()
        out.append(Check("messages", True, f"{total} rows, {pending_n} awaiting embedding", severity="info"))
        cur.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        sup = cur.fetchone()
        if sup and sup[0]:
            out.append(Check("role", True, "connected as a superuser; use a limited owner role and a read-only "
                                           "role for the MCP server (docs/SCHEMA.md)", severity="info"))
    return out


def failed(checks: Iterable[Check]) -> list[Check]:
    """The checks that count as failures (errors and warnings that are not ok)."""
    return [c for c in checks if not c.ok and c.severity != "info"]
