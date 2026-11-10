"""The read-only login: `init` makes it, the MCP server and recall use it, `doctor` warns when it is missing."""
from __future__ import annotations

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from typer.testing import CliRunner

from resonant_archive import cli, commands, config, db, runtime

from .permcheck import is_private

runner = CliRunner()
INSERT = "INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq) VALUES ('a', 's', 'c', now(), 'user', 'x', 0)"


def test_init_creates_a_select_only_role(blank_dsn):
    result = db.init(blank_dsn, reader=True)
    assert result.ok and result.reader and result.reader.dsn and result.reader.created
    assert conninfo_to_dict(result.reader.dsn)["user"].startswith(db.READER_ROLE)
    with psycopg.connect(blank_dsn, autocommit=True) as owner:
        owner.execute(INSERT)
    with psycopg.connect(result.reader.dsn, autocommit=True) as ro:
        assert ro.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert ro.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"
                          ).fetchone() == (False, False, False)
        for stmt in (INSERT, "UPDATE messages SET text = 'y'", "DELETE FROM messages", "TRUNCATE messages",
                     "CREATE TABLE evil (x int)", "DROP TABLE messages"):
            with pytest.raises(psycopg.Error):
                ro.execute(stmt)
        # not even by turning the session flag off
        ro.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute(INSERT)


def test_the_role_sees_tables_added_later(blank_dsn):
    result = db.init(blank_dsn, reader=True)
    with psycopg.connect(blank_dsn, autocommit=True) as owner:
        owner.execute("CREATE TABLE later (x int)")
        owner.execute("INSERT INTO later VALUES (7)")
    with psycopg.connect(result.reader.dsn) as ro:
        assert ro.execute("SELECT x FROM later").fetchone()[0] == 7


def test_init_again_keeps_a_working_address_and_replaces_a_stale_one(blank_dsn):
    first = db.init(blank_dsn, reader=True).reader
    again = db.init(blank_dsn, reader=True, keep_readonly=first.dsn).reader
    assert again.dsn == first.dsn and not again.created and not again.rotated
    # trust auth in the test cluster accepts any password, so an address for another database stands in for a stale one
    wrong_db = make_conninfo(first.dsn, dbname="postgres")
    rotated = db.init(blank_dsn, reader=True, keep_readonly=wrong_db).reader
    assert rotated.rotated and rotated.dsn != wrong_db
    assert conninfo_to_dict(rotated.dsn)["dbname"] == conninfo_to_dict(blank_dsn)["dbname"]


def test_the_password_is_not_sent_in_clear(blank_dsn):
    """ALTER ROLE gets a SCRAM verifier, so a server log with log_statement=all never holds the password."""
    r = db.init(blank_dsn, reader=True).reader
    role = conninfo_to_dict(r.dsn)["user"]
    with psycopg.connect(blank_dsn) as c:
        stored = c.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)).fetchone()
    assert stored[0].startswith("SCRAM-SHA-256$")
    assert conninfo_to_dict(r.dsn)["password"] not in stored[0]


def test_a_second_database_gets_its_own_role(pg_admin_dsn, blank_dsn):
    assert db.reader_role_name(config.DEFAULT_DB) == "archive_reader"
    other = db.reader_role_name("my.db")
    assert other != "archive_reader" and other.startswith("archive_reader_") and "." not in other
    r = db.init(blank_dsn, reader=True).reader
    assert db.reader_role_name(conninfo_to_dict(blank_dsn)["dbname"]) in r.dsn


def test_no_createrole_means_a_note_not_a_crash(dsn):
    with psycopg.connect(dsn, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'plain_owner'").fetchone():
            admin.execute("CREATE ROLE plain_owner LOGIN")
        admin.execute("GRANT ALL ON SCHEMA public TO plain_owner")
    owner_dsn = make_conninfo(dsn, user="plain_owner")
    with psycopg.connect(owner_dsn) as c:
        res = db.ensure_reader(c, owner_dsn)   # the role for this (unique) database does not exist yet
    assert res.dsn is None and "read-only role" in res.note and "by hand" in res.note


def test_commands_init_saves_readonly_dsn_and_the_search_side_uses_it(blank_dsn, monkeypatch):
    monkeypatch.setenv(config.ENV_DSN, blank_dsn)
    res = commands.init()
    assert res.reader and res.reader.dsn
    assert config.load_file()["database"]["readonly_dsn"] == res.reader.dsn
    assert "dsn" not in config.load_file()["database"]            # the owner's env address is not copied into the file
    assert runtime.readonly_dsn() == res.reader.dsn
    assert config.resolve_dsn() == blank_dsn
    # run again: nothing changes
    assert commands.init().reader.dsn == res.reader.dsn


def test_cli_init_with_dsn_saves_both_and_no_save_makes_no_role(blank_dsn, tmp_path):
    res = runner.invoke(cli.app, ["init", "--dsn", blank_dsn, "--no-save"])
    assert res.exit_code == 0, res.output
    assert not config.config_file().exists() and "no read-only role" in res.output
    res = runner.invoke(cli.app, ["init", "--dsn", blank_dsn])
    assert res.exit_code == 0, res.output
    cfg = config.load_file()["database"]
    assert cfg["dsn"] == blank_dsn and cfg["readonly_dsn"] != blank_dsn
    assert "read-only role archive_reader" in res.output
    assert cfg["readonly_dsn"] not in res.output                    # the address (and its password) is never printed
    assert is_private(config.config_file())


def test_doctor_warns_when_readonly_dsn_is_unset_or_the_owner(dsn, monkeypatch):
    by = {c.name: c for c in db.doctor(dsn)}
    assert not by["read-only role"].ok and by["read-only role"].severity == "warn" and "init" in by["read-only role"].fix
    monkeypatch.setenv(config.ENV_READONLY_DSN, dsn)
    by = {c.name: c for c in db.doctor(dsn)}
    assert not by["read-only role"].ok and "same login" in by["read-only role"].detail


def test_doctor_is_happy_with_the_real_role_and_unhappy_with_one_that_can_write(dsn, monkeypatch):
    reader = db.init(dsn, reader=True).reader.dsn
    monkeypatch.setenv(config.ENV_READONLY_DSN, reader)
    assert {c.name: c for c in db.doctor(dsn)}["read-only role"].ok
    with psycopg.connect(dsn, autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'can_write'").fetchone():
            admin.execute("CREATE ROLE can_write LOGIN")
        admin.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO can_write")
        admin.execute("GRANT USAGE ON SCHEMA public TO can_write")
    monkeypatch.setenv(config.ENV_READONLY_DSN, make_conninfo(dsn, user="can_write"))
    c = {c.name: c for c in db.doctor(dsn)}["read-only role"]
    assert not c.ok and "can write" in c.detail


def test_doctor_warns_when_the_reader_can_create_in_public(dsn, monkeypatch):
    reader = db.init(dsn, reader=True).reader.dsn
    monkeypatch.setenv(config.ENV_READONLY_DSN, reader)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    assert {c.name: c for c in db.doctor(dsn)}["read-only role"].ok
    with psycopg.connect(dsn, autocommit=True) as admin:       # what every role had before Postgres 15
        admin.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")
    c = {c.name: c for c in db.doctor(dsn)}["read-only role"]
    assert not c.ok and c.severity == "warn" and "CREATE in schema public" in c.detail
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in c.fix
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    assert {c.name: c for c in db.doctor(dsn)}["read-only role"].ok


def test_doctor_does_not_leak_the_readonly_password(dsn, monkeypatch):
    monkeypatch.setenv(config.ENV_READONLY_DSN, "postgresql://reader:hunter2hunter2@127.0.0.1:1/none")
    c = {c.name: c for c in db.doctor(dsn)}["read-only role"]
    assert not c.ok and "hunter2hunter2" not in c.detail


def test_mcp_and_recall_connect_with_the_reader(blank_dsn, monkeypatch):
    monkeypatch.setenv(config.ENV_DSN, blank_dsn)
    reader = commands.init().reader.dsn
    from resonant_archive.search import open_readonly
    with open_readonly(runtime.readonly_dsn()) as c:
        assert c.execute("SELECT current_user").fetchone()[0].startswith("archive_reader")
        with pytest.raises(psycopg.Error):
            c.execute(INSERT)
    assert runtime.readonly_dsn() == reader
