"""The command line, end to end against a real Postgres. No model is available in tests, so every search runs on the
embedder-down path (full text + anchors), which is a behaviour the product promises."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from resonant_archive import cli, commands, config

from . import synth
from .permcheck import is_private

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_embedder(monkeypatch, tmp_path):
    """Nothing listens on the embed port and no model exists: the vector signal must be skipped, not crash."""
    monkeypatch.setenv("RESONANT_EMBEDDER_MODEL_DIR", str(tmp_path / "no-model"))
    monkeypatch.setenv("RESONANT_EMBEDDER_PORT", "9")


@pytest.fixture
def archive(dsn, tmp_path, monkeypatch):
    """A database with a claude_code session and a chronicle mirror imported through the CLI."""
    monkeypatch.setenv("RESONANT_ARCHIVE_DSN", dsn)
    cc = tmp_path / "cc"
    synth.claude_code_session(cc)
    mirror = synth.mirror_tree(tmp_path / "mirror")
    for target in (cc, mirror):
        res = runner.invoke(cli.app, ["import", str(target)])
        assert res.exit_code == 0, res.output
    return dsn


def test_importers_lists_the_builtins():
    res = runner.invoke(cli.app, ["importers"])
    assert res.exit_code == 0
    for name in ("claude_code", "chronicle_mirror", "nexus_markdown", "markdown_folder"):
        assert name in res.output


def test_init_import_stats_doctor(blank_dsn, tmp_path, monkeypatch):
    monkeypatch.setenv("RESONANT_ARCHIVE_DSN", blank_dsn)
    res = runner.invoke(cli.app, ["init"])
    assert res.exit_code == 0 and "schema version 0003" in res.output
    assert runner.invoke(cli.app, ["init"]).output.count("already current") == 1  # idempotent
    synth.claude_code_session(tmp_path / "cc")
    res = runner.invoke(cli.app, ["import", str(tmp_path / "cc")])
    assert res.exit_code == 0 and "8 new" in res.output
    again = runner.invoke(cli.app, ["import", str(tmp_path / "cc")])
    assert "1 unchanged" in again.output and "0 new" in again.output
    rebuilt = runner.invoke(cli.app, ["import", str(tmp_path / "cc"), "--rebuild", "--yes"])
    assert rebuilt.exit_code == 0
    stats = runner.invoke(cli.app, ["stats"])
    assert "claude-code" in stats.output and "await embedding" in stats.output
    as_json = json.loads(runner.invoke(cli.app, ["stats", "--json"]).output)
    assert as_json["embedding"]["pending"] == 5 and as_json["memories"] == 0
    doc = runner.invoke(cli.app, ["doctor"])
    assert "[ok  ] migrations" in doc.output
    assert "embedder server" in doc.output and "embedding model" in doc.output  # embedder checks are wired in
    assert "[warn] embedding model" in doc.output and "models fetch" in doc.output
    assert "[FAIL]" not in doc.output and doc.exit_code == 1     # the database is fine; the missing model is reported


def test_init_saves_dsn_and_reports_missing_database(blank_dsn, monkeypatch):
    monkeypatch.delenv("RESONANT_ARCHIVE_DSN", raising=False)
    res = runner.invoke(cli.app, ["init", "--dsn", blank_dsn])
    assert res.exit_code == 0 and "saved the database address" in res.output
    assert config.resolve_dsn() == blank_dsn
    assert is_private(config.config_file())
    monkeypatch.delenv("RESONANT_ARCHIVE_DSN", raising=False)
    shutil.rmtree(config.config_dir())
    res = runner.invoke(cli.app, ["stats"])
    assert res.exit_code == 1 and "no database configured" in res.output


def test_init_docker_writes_env_and_prints_steps(tmp_path, monkeypatch):
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    res = runner.invoke(cli.app, ["init", "--docker", "--project-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    env = tmp_path / ".env"
    assert is_private(env)
    password = dict(l.split("=", 1) for l in env.read_text().splitlines())["POSTGRES_PASSWORD"]
    assert len(password) >= 24 and password not in res.output  # the password is never printed
    assert "docker compose up -d --wait" in res.output and "resonant-archive init" in res.output
    assert password in config.resolve_dsn()                      # but the DSN is saved for the next commands
    # running it again keeps the same password
    runner.invoke(cli.app, ["init", "--docker", "--project-dir", str(tmp_path)])
    assert dict(l.split("=", 1) for l in env.read_text().splitlines())["POSTGRES_PASSWORD"] == password


def test_init_docker_falls_back_to_the_bundled_compose_file(tmp_path):
    """A pip install has no clone: with no compose file in --project-dir the packaged copy is written there."""
    res = runner.invoke(cli.app, ["init", "--docker", "--project-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "docker-compose.yml").read_text() == (ROOT / "docker-compose.yml").read_text()
    assert (tmp_path / "docker" / "init-extensions.sql").read_text() == (ROOT / "docker" / "init-extensions.sql").read_text()
    assert (tmp_path / ".env").exists()


def test_init_docker_keeps_an_existing_compose_file(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("# mine\n")
    res = runner.invoke(cli.app, ["init", "--docker", "--project-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "docker-compose.yml").read_text() == "# mine\n"
    assert not (tmp_path / "docker").exists()


def test_bundled_docker_files_match_the_repo_copies():
    """The package data are copies of the files at the repo root; they must not drift apart."""
    from resonant_archive import commands
    bundled = commands.bundled_compose_files()
    assert bundled["docker-compose.yml"] == (ROOT / "docker-compose.yml").read_text()
    assert bundled["docker/init-extensions.sql"] == (ROOT / "docker" / "init-extensions.sql").read_text()


def test_version_is_2_0_1():
    import resonant_archive
    assert resonant_archive.__version__ == "2.0.1"


def test_migrate_v01_reads_nothing(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "chroma.sqlite3").write_bytes(b"not a database")
    res = runner.invoke(cli.app, ["migrate-v01", "--from", str(old)])
    assert res.exit_code == 0 and "was not read" in res.output and "--importer markdown_folder" in res.output
    assert (old / "chroma.sqlite3").read_bytes() == b"not a database"


def _recall(capsys, *argv):
    code = cli.run_recall_cli(list(argv))
    return code, capsys.readouterr().out


def test_recall_text_and_json_with_embedder_down(archive, capsys):
    code, out = _recall(capsys, "search", "widget", "--mode", "fts")
    assert code == 0 and "widget" in out
    code, out = _recall(capsys, "search", "garden", "--json")
    body = json.loads(out)
    assert code == 0 and body["ok"] is True and body["action"] == "search"
    assert body["data"]["vectors_skipped"] is True            # embedder down: said so, still answered
    assert "garden" in " ".join(h["text"] for h in body["data"]["hits"])


def test_recall_errors_follow_the_contract(archive, capsys, monkeypatch):
    code, out = _recall(capsys, "search", "x", "--source", "nope", "--json")
    assert code == 2 and json.loads(out) == {"ok": False, "error": "source: nope not one of chronicle, claude-code",
                                             "kind": "usage"}
    code, out = _recall(capsys, "search", "x", "--source", "nope")
    assert code == 2 and out.startswith("recall failed:")
    monkeypatch.setenv("RESONANT_ARCHIVE_DSN", "host=/nonexistent port=1 dbname=x")
    code, out = _recall(capsys, "search", "x", "--json")
    assert code == 1 and json.loads(out)["kind"] == "db"
    monkeypatch.delenv("RESONANT_ARCHIVE_DSN")
    code, out = _recall(capsys, "search", "x")
    assert code == 1 and "no database configured" in out


def test_recall_goes_through_main_with_dashes_and_terminator(archive, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["resonant-archive", "recall", "search", "--json", "--", "-garden"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["data"]["query"] == "-garden"
    monkeypatch.setattr("sys.argv", ["resonant-archive", "search", "garden"])   # shorthand for `recall search`
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and "garden" in capsys.readouterr().out


def test_recall_is_read_only(archive, capsys):
    """The recall connection refuses writes even though the DSN is the owner's."""
    from resonant_archive.search import open_readonly

    conn = open_readonly(archive)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("DELETE FROM messages")
    conn.close()
    assert psycopg.connect(archive).execute("SELECT count(*) FROM messages").fetchone()[0] == 16


def test_recall_drill_only_with_memories(archive, capsys, dsn):
    code, out = _recall(capsys, "overview", "--from", "2026-03-01", "--to", "2026-04-30", "--json")
    assert json.loads(out)["data"]["memories"]
    code, out = _recall(capsys, "drill", "0")
    assert code == 0 and "garden" in out
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DELETE FROM memories")
    code, out = _recall(capsys, "drill", "0")
    assert code == 2                                          # not an action at all without memories


# ---------------------------------------------------------------- init --docker: where .env goes, and which passwords it keeps

def test_init_docker_defaults_to_the_data_folder_not_the_current_directory(tmp_path, monkeypatch):
    here = tmp_path / "repo"
    here.mkdir()
    monkeypatch.chdir(here)
    res = runner.invoke(cli.app, ["init", "--docker"])
    assert res.exit_code == 0, res.output
    assert list(here.iterdir()) == []                                     # nothing written next to the user's files
    target = config.data_dir() / "docker"
    assert is_private(target / ".env")
    assert (target / "docker-compose.yml").exists()
    assert is_private(target, directory=True)


@pytest.mark.parametrize("old", ["change-me-to-a-long-random-string", "short", "", "Password123456789"])
def test_a_placeholder_or_short_password_in_an_existing_env_is_replaced(tmp_path, old):
    (tmp_path / ".env").write_text(f"POSTGRES_PASSWORD={old}\nRESONANT_ARCHIVE_PORT=60000\nMY_OWN=keep\n")
    res = runner.invoke(cli.app, ["init", "--docker", "--project-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    values = dict(l.split("=", 1) for l in (tmp_path / ".env").read_text().splitlines())
    assert values["POSTGRES_PASSWORD"] != old and len(values["POSTGRES_PASSWORD"]) >= 24
    assert values["RESONANT_ARCHIVE_PORT"] == "60000" and values["MY_OWN"] == "keep"
    assert "placeholder or too short" in res.output and "docker compose down -v" in res.output
    assert values["POSTGRES_PASSWORD"] not in res.output


def test_a_good_password_is_kept_and_a_loose_mode_is_tightened(tmp_path):
    env = tmp_path / ".env"
    good = "Zq8vN2pXk4TjR7mWc1LsHd9B"
    env.write_text(f"POSTGRES_PASSWORD={good}\nRESONANT_ARCHIVE_PORT=54329\n")
    env.chmod(0o644)
    assert not is_private(env) or sys.platform == "win32"   # Windows: the file carries its folder's ACL, not 0644
    _, pw = commands.write_env_file(tmp_path)
    assert pw == good and is_private(env)


def test_weak_password_rules():
    for bad in (None, "", "short", "change-me-to-a-long-random-string", "my-example-password-here"):
        assert commands.weak_password(bad)
    assert not commands.weak_password("Zq8vN2pXk4TjR7mWc1LsHd9B")
    assert not commands.weak_password(__import__("secrets").token_urlsafe(24))


# ---------------------------------------------------------------- LOW-6: DSNs and argv

def test_a_password_on_the_command_line_gets_a_note(blank_dsn):
    pw = "postgresql://u:hunter2hunter2@127.0.0.1:1/none"
    res = runner.invoke(cli.app, ["stats", "--dsn", pw])
    assert "visible to other users" in res.output and "hunter2hunter2" not in res.output
    res = runner.invoke(cli.app, ["doctor", "--dsn", pw])
    assert "visible to other users" in res.output and "hunter2hunter2" not in res.output
    quiet = runner.invoke(cli.app, ["stats", "--dsn", "postgresql://127.0.0.1:1/none"])
    assert "visible to other users" not in quiet.output


@pytest.mark.parametrize("dsn,secret", [
    ("postgresql://u:pw@host/db", "pw"),
    ("postgresql://u:p@ss@word@host:5432/db", "p@ss"),
    ("postgresql://u:p@ss@word@host:5432/db?sslmode=require", "word"),
    ("postgresql://u:a:b:c@host/db", "a:b"),
    ("host=h user=u password=hunter2hunter2 dbname=d", "hunter2"),
    ("host=h user=u password='two words' dbname=d", "two words"),
    ("postgresql://:onlypass@host/db", "onlypass"),
    ("host=h user=u sslpassword=keyphrase99 dbname=d", "keyphrase99"),
    ("host=h sslpassword='key phrase' dbname=d", "key phrase"),
    ("postgresql://u@host/db?sslmode=require&sslpassword=keyphrase99", "keyphrase99"),
    ("postgresql://u:pw@host/db?sslpassword=keyphrase99&sslmode=require", "keyphrase99"),
])
def test_redact_dsn_hides_the_password(dsn, secret):
    out = config.redact_dsn(dsn)
    assert secret not in out and "***" in out and "host" in out


def test_redact_dsn_keeps_the_rest_of_a_query_after_a_hidden_sslpassword():
    assert config.redact_dsn("postgresql://u@h/d?sslpassword=zzz&sslmode=require") == \
        "postgresql://u@h/d?sslpassword=***&sslmode=require"


def test_redact_dsn_keeps_what_has_no_password():
    for dsn in ("postgresql://host:5432/db", "postgresql://u@host/db", "host=h dbname=d", "postgresql://u@host/db?x=a@b"):
        assert config.redact_dsn(dsn) == dsn


def test_import_rebuild_asks_for_yes_and_refuses_a_project_filter(blank_dsn, tmp_path, monkeypatch):
    monkeypatch.setenv("RESONANT_ARCHIVE_DSN", blank_dsn)
    runner.invoke(cli.app, ["init"])
    session = synth.claude_code_session(tmp_path / "cc", "-a", "sa")
    assert runner.invoke(cli.app, ["import", str(tmp_path / "cc")]).exit_code == 0
    lines = session.read_text().splitlines()
    session.write_text("".join(l + "\n" for l in lines if '"uuid": "a3"' not in l and "broken" not in l))   # a reply is gone

    refused = runner.invoke(cli.app, ["import", str(tmp_path / "cc"), "--rebuild", "--project", "-a"])
    assert refused.exit_code == 1 and "--rebuild cannot be combined with --project" in refused.output

    dry = runner.invoke(cli.app, ["import", str(tmp_path / "cc"), "--rebuild", "--dry-run"])
    assert dry.exit_code == 0 and "would remove 1 message row(s)" in dry.output

    asked = runner.invoke(cli.app, ["import", str(tmp_path / "cc"), "--rebuild"])       # not a terminal, no --yes
    assert asked.exit_code == 0 and "NOT removed" in asked.output and "--yes" in asked.output
    assert "8 removed" not in asked.output and "removed (rebuild)" not in asked.output

    done = runner.invoke(cli.app, ["import", str(tmp_path / "cc"), "--rebuild", "--yes"])
    assert done.exit_code == 0 and "1 removed (rebuild)" in done.output and "removing 1 message row(s)" in done.output
