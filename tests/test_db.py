"""Schema, migrations, init and doctor against a real Postgres."""

from __future__ import annotations

import psycopg
import pytest

from resonant_archive import config, db

from .permcheck import is_private


def test_init_creates_schema_and_is_idempotent(blank_dsn):
    first = db.init(blank_dsn)
    assert first.ok and first.applied == [1, 2, 3] and first.version == db.latest_version()
    second = db.init(blank_dsn)
    assert second.ok and second.applied == []
    with psycopg.connect(blank_dsn) as c:
        tables = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
    assert set(db.EXPECTED_TABLES) <= tables


def test_messages_columns_and_generated_tsv(conn):
    conn.execute(
        "INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq) "
        "VALUES ('a', 'notes', 'c1', now(), 'user', 'The quick brown fox', 0)"
    )
    row = conn.execute("SELECT tsv @@ to_tsquery('simple', 'fox'), embed_status, hidden, context, tags FROM messages").fetchone()
    assert row == (True, "pending", False, "default", [])
    # the generated column cannot be written to
    with pytest.raises(psycopg.Error):
        conn.execute("UPDATE messages SET tsv = NULL")


def test_embedding_is_768_dimensions(conn):
    ok = "[" + ",".join(["0.1"] * 768) + "]"
    conn.execute(
        "INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq, embedding) "
        "VALUES ('a', 's', 'c', now(), 'user', 'x', 0, %s::vector)", (ok,))
    with pytest.raises(psycopg.Error):
        conn.execute(
            "INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq, embedding) "
            "VALUES ('b', 's', 'c', now(), 'user', 'x', 1, '[1,2,3]'::vector)")


def test_speaker_and_status_constraints(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq) "
                     "VALUES ('a', 's', 'c', now(), 'wizard', 'x', 0)")
    conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq, embed_status) "
                     "VALUES ('a', 's', 'c', now(), 'user', 'x', 0, 'maybe')")


def test_anchors_cascade_with_messages(conn):
    conn.execute("INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq) "
                 "VALUES ('a', 's', 'c', now(), 'user', 'x', 0)")
    conn.execute("INSERT INTO anchors (token, kind, msg_id, ts) VALUES ('/a/b', 'path', 'a', now())")
    conn.execute("DELETE FROM messages WHERE id = 'a'")
    assert conn.execute("SELECT count(*) FROM anchors").fetchone()[0] == 0


def test_trigram_and_hnsw_indexes_exist(conn):
    names = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename='messages'").fetchall()}
    assert {"messages_tsv_gin", "messages_text_trgm", "messages_embedding", "messages_source_ts", "messages_conv_seq"} <= names


def test_edited_migration_is_refused(dsn):
    with psycopg.connect(dsn) as c:
        c.execute("UPDATE schema_migrations SET checksum = 'tampered'")
        c.commit()
        with pytest.raises(db.DatabaseError, match="edited after it was applied"):
            db.migrate(c)


def test_connect_readonly_refuses_writes(dsn):
    with db.connect(dsn, readonly=True) as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq) "
                      "VALUES ('a', 's', 'c', now(), 'user', 'x', 0)")


# Port 1 refuses at once on Linux, but libpq on Windows waited 130 s for it: these DSNs name their own short timeout
# (a DSN's own connect_timeout wins over the default), and test_connect_timeout_default_* covers the default.
def test_connect_failure_hides_password():
    with pytest.raises(db.DatabaseError) as err:
        db.connect("postgresql://u:hunter2secret@127.0.0.1:1/none?connect_timeout=2")
    assert "hunter2secret" not in str(err.value)


def test_connect_timeout_default_applies_unless_the_dsn_or_environment_sets_one(monkeypatch):
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    assert config.connect_kwargs("postgresql://u@h/d") == {"connect_timeout": config.CONNECT_TIMEOUT_S}
    assert config.connect_kwargs(None) == {"connect_timeout": config.CONNECT_TIMEOUT_S}
    assert config.connect_kwargs("postgresql://u@h/d?connect_timeout=3") == {}
    assert config.connect_kwargs("host=h dbname=d connect_timeout=3") == {}
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "4")
    assert config.connect_kwargs("postgresql://u@h/d") == {}


def _without_reader(checks):
    return [c for c in checks if c.name != "read-only role"]


def test_doctor_healthy(dsn, monkeypatch):
    result = db.init(dsn, reader=True)
    monkeypatch.setenv(config.ENV_READONLY_DSN, result.reader.dsn)
    checks = db.doctor(dsn)
    assert not db.failed(checks), checks
    names = {c.name for c in checks}
    assert {"connection", "extension vector", "extension pg_trgm", "migrations", "tables", "embedding column"} <= names


def test_doctor_on_empty_database_names_the_fix(blank_dsn):
    checks = db.doctor(blank_dsn)
    bad = {c.name: c for c in db.failed(_without_reader(checks))}
    assert "extension vector" in bad and "CREATE EXTENSION IF NOT EXISTS vector;" in bad["extension vector"].fix
    assert "schema" in bad


def test_doctor_fix_repairs(blank_dsn):
    checks = db.doctor(blank_dsn, fix=True)
    assert not db.failed(_without_reader(checks)), checks


def test_doctor_reports_unreachable_database_without_raising():
    checks = db.doctor("postgresql://u:pw@127.0.0.1:1/none?connect_timeout=2")
    assert checks[0].name == "connection" and not checks[0].ok
    assert "pw@" not in checks[0].detail


def test_doctor_without_config_says_how_to_configure():
    checks = db.doctor()
    assert checks[0].name == "config" and not checks[0].ok


def test_doctor_extra_checks_run_and_crashes_are_contained(dsn):
    def good(conn):
        return [db.Check("embedder", True, "fine")]

    def bad(conn):
        raise RuntimeError("boom")

    checks = db.doctor(dsn, extra_checks=[good, bad])
    by = {c.name: c for c in checks}
    assert by["embedder"].ok
    assert not by["bad"].ok and "boom" in by["bad"].detail


def test_init_reports_missing_extension(blank_dsn, monkeypatch):
    monkeypatch.setattr(db, "EXTENSIONS", ("vector", "pg_trgm", "definitely_not_an_extension"))
    result = db.init(blank_dsn)
    assert not result.ok and result.missing_extensions == ["definitely_not_an_extension"]
    assert "CREATE EXTENSION IF NOT EXISTS definitely_not_an_extension;" in result.fix_hint()
    assert result.applied == []


def test_config_resolution_order(tmp_path, monkeypatch):
    with pytest.raises(config.ConfigError):
        config.resolve_dsn()
    path = config.write_dsn("postgresql://a:b@h/d", readonly_dsn="postgresql://r:s@h/d")
    assert is_private(path)
    assert config.resolve_dsn() == "postgresql://a:b@h/d"
    assert config.resolve_dsn(readonly=True) == "postgresql://r:s@h/d"
    monkeypatch.setenv(config.ENV_DSN, "postgresql://env@h/d")
    assert config.resolve_dsn() == "postgresql://env@h/d"
    assert config.resolve_dsn("explicit") == "explicit"


def test_config_write_keeps_other_sections(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[embedder]\nurl = "http://127.0.0.1:8899"\n')
    config.write_dsn("postgresql://a:b@h/d", path=p)
    data = config.load_file(p)
    assert data["embedder"]["url"] == "http://127.0.0.1:8899" and data["database"]["dsn"] == "postgresql://a:b@h/d"


def test_redact_dsn():
    assert "secret" not in config.redact_dsn("postgresql://u:secret@h/d")
    assert "secret" not in config.redact_dsn("host=h user=u password=secret dbname=d")
