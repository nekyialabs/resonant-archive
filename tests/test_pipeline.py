"""Import pipeline against a real Postgres."""

from __future__ import annotations

import datetime as dt
import stat

import psycopg
import pytest

from resonant_archive import commands, config, db, importers, pipeline
from resonant_archive.importers.base import Importer, Options, Record, Unit

from . import synth
from .permcheck import is_private
from .symhelp import symlink_or_skip


def q(dsn, sql, *args):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args or None).fetchall()


def test_claude_code_import_end_to_end(dsn, tmp_path):
    synth.claude_code_session(tmp_path)
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn)
    assert rep.ok and rep.units_imported == 1 and rep.inserted == rep.kept > 0
    rows = q(dsn, "SELECT speaker, hidden, embed_status, seq FROM messages ORDER BY conv_id, seq")
    assert [r[3] for r in rows] == list(range(len(rows)))                 # seq is 0..n-1
    assert {r[2] for r in rows if r[1]} == {"skipped"} and {r[2] for r in rows if not r[1]} == {"pending"}
    assert q(dsn, "SELECT title, n_messages, n_visible FROM conversations")[0][0] == "Fixing the widget"
    # redacted before storage; never in messages, anchors or the report
    assert q(dsn, "SELECT count(*) FROM messages WHERE text LIKE %s", f"%{synth.FAKE_SK}%")[0][0] == 0
    assert q(dsn, "SELECT count(*) FROM messages WHERE text LIKE '%[redacted:api-key]%'")[0][0] == 1
    assert synth.FAKE_SK not in rep.summary()
    toks = {(t, k) for t, k in q(dsn, "SELECT token, kind FROM anchors")}
    assert ("/srv/app/widget.py", "path") in toks and ("abc1234", "sha") in toks
    assert not any(t.startswith("sk-") for t, _ in toks)


def test_second_import_is_a_no_op(dsn, tmp_path):
    synth.claude_code_session(tmp_path)
    commands.import_path("claude_code", tmp_path, dsn=dsn)
    before = q(dsn, "SELECT count(*), max(imported_at) FROM messages")
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn)
    assert rep.units_skipped == 1 and rep.units_imported == 0 and rep.inserted == 0
    assert q(dsn, "SELECT count(*), max(imported_at) FROM messages") == before


def test_forced_reimport_changes_nothing(dsn, tmp_path):
    synth.claude_code_session(tmp_path)
    first = commands.import_path("claude_code", tmp_path, dsn=dsn)
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn, force=True)
    assert rep.inserted == 0 and rep.updated == 0 and rep.unchanged == first.kept
    assert q(dsn, "SELECT count(*) FROM messages")[0][0] == first.kept


def test_grown_file_adds_only_new_rows(dsn, tmp_path):
    f = synth.claude_code_session(tmp_path)
    first = commands.import_path("claude_code", tmp_path, dsn=dsn)
    with open(f, "a") as fh:
        fh.write('{"sessionId":"sess-0001","type":"user","uuid":"u9","timestamp":"2026-03-01T11:00:00.000Z",'
                 '"message":{"role":"user","content":"one more thing"}}\n')
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn)
    assert rep.inserted == 1 and rep.unchanged == first.kept and rep.updated == 0
    assert q(dsn, "SELECT count(DISTINCT seq) = count(*) FROM messages")[0][0] is True


def test_embedding_survives_reimport_unless_text_changes(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("First note about tulips. " * 5)
    commands.import_path("markdown_folder", notes, dsn=dsn)
    vec = "[" + ",".join(["0.5"] * 768) + "]"
    with psycopg.connect(dsn) as c:
        c.execute("UPDATE messages SET embedding = %s::vector, embed_status = 'done', embedded_hash = 'h1', embedded_at = now()", (vec,))
        c.commit()
    commands.import_path("markdown_folder", notes, dsn=dsn, force=True)
    assert q(dsn, "SELECT embed_status, embedded_hash, embedding IS NOT NULL FROM messages") == [("done", "h1", True)]
    (notes / "a.md").write_text("A different note about beans. " * 5)
    rep = commands.import_path("markdown_folder", notes, dsn=dsn)
    assert rep.updated == 1
    assert q(dsn, "SELECT embed_status, embedded_hash, embedding IS NOT NULL FROM messages") == [("pending", None, False)]


def test_mirror_import_with_memories_and_session_spanning_days(dsn, tmp_path):
    synth.mirror_tree(tmp_path)
    rep = commands.import_path("chronicle_mirror", tmp_path, dsn=dsn)
    assert rep.ok and rep.memories == 1
    assert q(dsn, "SELECT count(*) FROM conversations")[0][0] == 1
    mem = q(dsn, "SELECT sid, ordinal, level, t_from < t_to, tsv @@ to_tsquery('simple','garden') FROM memories")
    assert mem == [("mirror-sess-1", 0, 1, True, True)]
    assert q(dsn, "SELECT count(*) FROM messages WHERE id LIKE '%:m1'")[0][0] == 1       # retried flush collapsed
    again = commands.import_path("chronicle_mirror", tmp_path, dsn=dsn)
    assert again.units_skipped == 1
    assert q(dsn, "SELECT count(*) FROM memories")[0][0] == 1


def test_nexus_import_both_flavours(dsn, tmp_path):
    synth.nexus_gpt(tmp_path)
    synth.nexus_claude(tmp_path)
    rep = commands.import_path("nexus_markdown", tmp_path, dsn=dsn, timezone="UTC")
    assert rep.ok and rep.units_imported == 2
    assert {r[0] for r in q(dsn, "SELECT DISTINCT source FROM messages")} == {"chatgpt", "claude-ai"}
    assert q(dsn, "SELECT count(*) FROM conversations")[0][0] == 2


def test_changed_options_reimport(dsn, tmp_path):
    synth.nexus_gpt(tmp_path)
    a = commands.import_path("nexus_markdown", tmp_path, dsn=dsn, timezone="UTC")
    b = commands.import_path("nexus_markdown", tmp_path, dsn=dsn, timezone="Europe/Berlin")
    assert a.inserted == 4 and b.units_imported == 1 and b.updated == 4        # same ids, new times


def test_rebuild_removes_rows_a_re_read_file_no_longer_produces(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("Alpha note text. " * 10)
    (notes / "b.md").write_text("# One\n\n" + "First section text. " * 40 + "\n\n# Two\n\n" + "Second section text. " * 40)
    commands.import_path("markdown_folder", notes, dsn=dsn)
    before = q(dsn, "SELECT count(*) FROM messages WHERE conv_id = 'b.md'")[0][0]
    (notes / "b.md").write_text("# One\n\n" + "First section text. " * 40)
    plain = commands.import_path("markdown_folder", notes, dsn=dsn)
    assert plain.removed == 0 and q(dsn, "SELECT count(*) FROM messages WHERE conv_id = 'b.md'")[0][0] == before  # kept by default
    ask = commands.import_path("markdown_folder", notes, dsn=dsn, rebuild=True)       # no --yes: says so, removes nothing
    assert ask.removed == 0 and any("NOT removed" in n and "--yes" in n for n in ask.notes)
    assert q(dsn, "SELECT count(*) FROM messages WHERE conv_id = 'b.md'")[0][0] == before
    dry = commands.import_path("markdown_folder", notes, dsn=dsn, rebuild=True, dry_run=True)
    assert dry.removed == 0 and any("would remove" in n for n in dry.notes)
    rep = commands.import_path("markdown_folder", notes, dsn=dsn, rebuild=True, yes=True)
    now = q(dsn, "SELECT count(*) FROM messages WHERE conv_id = 'b.md'")[0][0]
    assert rep.removed == before - now >= 1 and q(dsn, "SELECT count(*) FROM messages WHERE conv_id = 'a.md'")[0][0] > 0


def test_rebuild_never_touches_a_file_that_was_purged_from_the_folder(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("Alpha note text. " * 10)
    (notes / "b.md").write_text("Beta note text. " * 10)
    commands.import_path("markdown_folder", notes, dsn=dsn)
    before = q(dsn, "SELECT count(*) FROM messages")[0][0]
    (notes / "b.md").unlink()
    rep = commands.import_path("markdown_folder", notes, dsn=dsn, rebuild=True, yes=True)
    assert rep.removed == 0 and q(dsn, "SELECT count(*) FROM messages")[0][0] == before
    assert any("not in this input" in n for n in rep.notes)


def test_rebuild_removes_nothing_when_a_unit_fails_or_input_is_empty(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("Alpha note text. " * 10)
    commands.import_path("markdown_folder", notes, dsn=dsn)
    empty = tmp_path / "empty"
    empty.mkdir()
    rep = commands.import_path("markdown_folder", empty, dsn=dsn, rebuild=True)
    assert rep.removed == 0 and rep.notes and q(dsn, "SELECT count(*) FROM messages")[0][0] > 0


class Flaky(Importer):
    name = "flaky"
    default_source = "flaky"

    def detect(self, path):
        return True

    def units(self, path, opts):
        for k in ("good", "bad", "good2"):
            p = path / f"{k}.txt"
            p.write_text(k)
            yield Unit(key=k, files=[p])

    def unit_records(self, unit, opts, stats):
        if unit.key == "bad":
            raise RuntimeError("cannot parse this unit")
        yield Record(id=f"flaky:{unit.key}", source="flaky", conv_id=unit.key,
                     ts=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), speaker="user", text=f"hello {unit.key}")


def test_one_bad_unit_does_not_stop_the_others_and_is_reported(dsn, tmp_path):
    rep = pipeline.run_import(Flaky(), tmp_path, dsn=dsn)
    assert not rep.ok and rep.units_failed == 1 and rep.units_imported == 2
    assert "unit bad" in rep.errors[0] and "RuntimeError" in rep.errors[0]
    assert q(dsn, "SELECT count(*) FROM messages")[0][0] == 2
    assert q(dsn, "SELECT count(*) FROM sources")[0][0] == 2          # the failed unit is retried next time


class Sloppy(Importer):
    name = "sloppy"
    default_source = "sloppy"

    def detect(self, path):
        return True

    def units(self, path, opts):
        yield Unit(key="u", files=[path])

    def unit_records(self, unit, opts, stats):
        t = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        yield Record(id="s:1", source="sloppy", conv_id="c", ts=t, speaker="user", text="x" * 25000)
        yield Record(id="s:2", source="sloppy", conv_id="c", ts=t, speaker="tool", text="y" * 3000, hidden=True)
        yield Record(id="s:3", source="sloppy", conv_id="c", ts=t, speaker="user", text="   ")
        yield Record(id="s:4", source="sloppy", conv_id="c", ts=t, speaker="user", text="", tags=["image"])
        yield Record(id="s:5", source="sloppy", conv_id="c", ts=t, speaker="user", text="same words")
        yield Record(id="s:6", source="sloppy", conv_id="c", ts=t, speaker="user", text="same words")
        yield Record(id="s:5", source="sloppy", conv_id="c", ts=t, speaker="user", text="id repeat")
        yield Record(id="s:7", source="sloppy", conv_id="c", ts=t, speaker="user",
                     text="nul\x00byte; ' OR 1=1; DROP TABLE messages; --")


def test_pipeline_cleaning_rules(dsn, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    rep = pipeline.run_import(Sloppy(), f, dsn=dsn)
    assert rep.truncated == 2 and rep.dropped_empty == 1 and rep.dropped_duplicate == 2 and rep.kept == 5
    lens = dict(q(dsn, "SELECT id, length(text) FROM messages"))
    assert lens["s:1"] < 20100 and "truncated" in q(dsn, "SELECT text FROM messages WHERE id='s:1'")[0][0]
    assert lens["s:2"] < 2100
    assert q(dsn, "SELECT text FROM messages WHERE id='s:4'")[0][0] == ""             # media-only row kept
    hostile = q(dsn, "SELECT text FROM messages WHERE id='s:7'")[0][0]
    assert "\x00" not in hostile and "DROP TABLE" in hostile                          # stored as data, not run
    assert q(dsn, "SELECT count(*) FROM messages")[0][0] == 5


def test_known_secrets_are_redacted(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("The staging login is Zebra7Quartz9Lemon and that is that. " * 3)
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("STAGING_LOGIN=Zebra7Quartz9Lemon\n")
    rep = commands.import_path("markdown_folder", notes, dsn=dsn, known_secrets=[str(secrets_file)])
    assert rep.redactions["known"] == 3
    assert q(dsn, "SELECT count(*) FROM messages WHERE text LIKE '%Zebra7%'")[0][0] == 0


def test_missing_path_is_reported(dsn, tmp_path):
    rep = commands.import_path("markdown_folder", tmp_path / "nope", dsn=dsn)
    assert not rep.ok and "does not exist" in rep.errors[0]


def test_auto_detect_and_unknown_importer(dsn, tmp_path):
    synth.claude_code_session(tmp_path)
    assert commands.import_path("auto", tmp_path, dsn=dsn).importer == "claude_code"
    with pytest.raises(config.ConfigError):
        commands.import_path("auto", tmp_path / "nothing-here", dsn=dsn)
    with pytest.raises(KeyError):
        commands.import_path("no_such_importer", tmp_path, dsn=dsn)


def test_stats(dsn, tmp_path):
    synth.claude_code_session(tmp_path)
    commands.import_path("claude_code", tmp_path, dsn=dsn)
    s = commands.stats(dsn)
    assert s["sources"][0]["source"] == "claude-code" and s["conversations"] == 1
    assert s["embedding"]["pending"] > 0 and s["speakers"]["tool"] == 2


def test_import_works_through_a_readonly_doctor_free_flow(dsn):
    assert not [c for c in db.failed(commands.doctor(dsn)) if c.name != "read-only role"]


# ---------------------------------------------------------------- command helpers that need no database

def test_write_env_file_is_private_and_stable(tmp_path):
    env, pw = commands.write_env_file(tmp_path)
    assert is_private(env) and len(pw) >= 24
    again, pw2 = commands.write_env_file(tmp_path)
    assert pw2 == pw                                                   # an existing password is kept
    _, pw3 = commands.write_env_file(tmp_path, overwrite=True)
    assert pw3 != pw


def test_init_docker_needs_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)
    with pytest.raises(config.ConfigError, match="docker was not found"):
        commands.init_docker(tmp_path)
    assert (tmp_path / "docker-compose.yml").exists()   # the bundled copy was written when the folder had none


def test_list_importers():
    names = [n for n, _, _ in commands.list_importers()]
    assert names == sorted(names) and "chronicle_mirror" in names


# ---------------------------------------------------------------- --rebuild deletes only the same importer's rows (LOW-6)

class Other(Importer):
    name, default_source = "other", "shared"

    def units(self, path, opts):
        yield Unit(key="one", files=[path / "o.dat"], root=path)

    def unit_records(self, unit, opts, stats):
        yield Record(id="other:1", source=self.label(opts), conv_id="oc", ts=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                     speaker="user", text="a row that belongs to the other importer")


def test_rebuild_never_touches_another_importers_rows_under_the_same_label(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("Alpha note text. " * 10)
    (notes / "b.md").write_text("Beta note text. " * 10)
    (notes / "o.dat").write_text("x")
    commands.import_path("markdown_folder", notes, dsn=dsn, source="shared")
    pipeline.run_import(Other(), notes, dsn=dsn)
    assert q(dsn, "SELECT importer, count(*) FROM messages GROUP BY importer ORDER BY importer") == \
        [("markdown_folder", 2), ("other", 1)]
    (notes / "b.md").write_text("# One\n\n" + "Beta one. " * 40 + "\n\n# Two\n\n" + "Beta two. " * 40)
    commands.import_path("markdown_folder", notes, dsn=dsn, source="shared")
    (notes / "b.md").write_text("# One\n\n" + "Beta one. " * 40)
    rep = commands.import_path("markdown_folder", notes, dsn=dsn, source="shared", rebuild=True, yes=True)
    assert rep.removed >= 1
    assert q(dsn, "SELECT id FROM messages WHERE importer = 'other'") == [("other:1",)]


def test_rebuild_leaves_rows_without_an_importer_alone(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("Alpha note text. " * 10)
    commands.import_path("markdown_folder", notes, dsn=dsn, source="shared")
    with psycopg.connect(dsn) as c:   # a row imported before migration 0002 has no importer
        c.execute("INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq) "
                  "VALUES ('legacy', 'shared', 'old', now(), 'user', 'legacy row', 0)")
    commands.import_path("markdown_folder", notes, dsn=dsn, source="shared", rebuild=True)
    assert q(dsn, "SELECT count(*) FROM messages WHERE id = 'legacy'")[0][0] == 1
    commands.import_path("markdown_folder", notes, dsn=dsn, source="shared", rebuild=True, force=True)
    assert q(dsn, "SELECT count(*) FROM messages WHERE id = 'legacy'")[0][0] == 1


def test_memories_carry_the_importer_too(dsn, tmp_path):
    synth.mirror_tree(tmp_path)
    commands.import_path("chronicle_mirror", tmp_path, dsn=dsn)
    assert q(dsn, "SELECT DISTINCT importer FROM memories") == [("chronicle_mirror",)]


# ---------------------------------------------------------------- Ward round 2, MED-A: a rebuild only judges what it read

def _two_projects(tmp_path):
    root = tmp_path / "proj"
    a = synth.claude_code_session(root, "-a", "sa")
    synth.claude_code_session(root, "-b", "sb")
    return root, a


def test_rebuild_with_a_project_filter_is_refused(dsn, tmp_path):
    root, _ = _two_projects(tmp_path)
    commands.import_path("claude_code", root, dsn=dsn)
    before = q(dsn, "SELECT count(*) FROM messages")[0][0]
    with pytest.raises(config.ConfigError, match="--project"):
        commands.import_path("claude_code", root, dsn=dsn, rebuild=True, yes=True, projects=["-a"])
    rep = pipeline.run_import("claude_code", root, dsn=dsn, rebuild=True, yes=True, options=Options(projects=["-a"]))
    assert not rep.ok and rep.removed == 0
    assert q(dsn, "SELECT count(*) FROM messages")[0][0] == before == 16


def test_rebuild_with_a_symlinked_file_keeps_everything(dsn, tmp_path):
    root, a = _two_projects(tmp_path)
    commands.import_path("claude_code", root, dsn=dsn)
    assert q(dsn, "SELECT count(*) FROM messages")[0][0] == 16
    real = tmp_path / "elsewhere.jsonl"
    a.rename(real)
    symlink_or_skip(real, a)
    rep = commands.import_path("claude_code", root, dsn=dsn, rebuild=True, yes=True)
    assert rep.removed == 0 and q(dsn, "SELECT count(*) FROM messages")[0][0] == 16
    assert any("not read" in n and "nothing was removed" in n for n in rep.notes)


def test_rebuild_keeps_a_unit_that_lost_lines(dsn, tmp_path):
    root, a = _two_projects(tmp_path)
    commands.import_path("claude_code", root, dsn=dsn)
    lines = a.read_text().splitlines()
    a.write_text("\n".join(l for l in lines if '"uuid": "a3"' not in l) + "\n{broken\n")   # one reply gone, one line bad
    rep = commands.import_path("claude_code", root, dsn=dsn, rebuild=True, yes=True)
    assert rep.removed == 0 and q(dsn, "SELECT count(*) FROM messages")[0][0] == 16


def test_rebuild_removes_a_reply_a_clean_re_read_no_longer_has(dsn, tmp_path):
    root, a = _two_projects(tmp_path)
    commands.import_path("claude_code", root, dsn=dsn)
    a.write_text("".join(l + "\n" for l in a.read_text().splitlines() if '"uuid": "a3"' not in l and "broken" not in l))
    rep = commands.import_path("claude_code", root, dsn=dsn, rebuild=True, yes=True)
    assert rep.removed == 1 and q(dsn, "SELECT count(*) FROM messages")[0][0] == 15
    assert q(dsn, "SELECT count(*) FROM messages WHERE conv_id = 'sb'")[0][0] == 8      # the other session was not touched


# ---------------------------------------------------------------- Ward round 2, LOW-E: an id belongs to its source and importer

def _hostile_line(uuid):
    return {"sessionId": "evil", "entrypoint": "cli", "type": "user", "uuid": uuid, "timestamp": "2026-03-01T10:00:00.000Z",
            "message": {"role": "user", "content": "overwritten by a hostile transcript"}}


def test_a_transcript_cannot_overwrite_another_sources_row_by_reusing_its_id(dsn, tmp_path):
    mine = tmp_path / "mine"
    synth.claude_code_session(mine, "-a", "sa")
    commands.import_path("claude_code", mine, dsn=dsn)
    (victim_id,) = q(dsn, "SELECT id FROM messages WHERE text LIKE 'Please look%'")[0]
    original = q(dsn, "SELECT text, conv_id, source, importer FROM messages WHERE id = %s", victim_id)
    # the id the first import gave the row is "claude-code:<session>:<uuid>"; the hostile file claims the same one
    _, sid, uuid = victim_id.split(":", 2)
    bad = tmp_path / "bad"
    line = _hostile_line(uuid)
    line["sessionId"] = sid
    synth.jl(bad / "p" / "evil.jsonl", [line])
    # same importer, other label: the row is not theirs to update
    rep = commands.import_path("claude_code", bad, dsn=dsn, source="somewhere-else")
    assert rep.ok and rep.collisions == 1 and rep.updated == 0 and "skipped" in rep.summary()
    assert q(dsn, "SELECT text, conv_id, source, importer FROM messages WHERE id = %s", victim_id) == original
    # same label, other importer
    class Squatter(Importer):
        name, default_source = "squatter", "claude-code"

        def units(self, path, opts):
            yield Unit(key="x", files=[path / "x.dat"], root=path)

        def unit_records(self, unit, opts, stats):
            yield Record(id=victim_id, source="claude-code", conv_id="c", ts=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                         speaker="user", text="squatting on someone else's id")

    (tmp_path / "sq").mkdir()
    (tmp_path / "sq" / "x.dat").write_text("x")
    rep2 = pipeline.run_import(Squatter(), tmp_path / "sq", dsn=dsn)
    assert rep2.collisions == 1
    assert q(dsn, "SELECT text, conv_id, source, importer FROM messages WHERE id = %s", victim_id) == original
    assert q(dsn, "SELECT count(*) FROM anchors WHERE msg_id = %s", victim_id)[0][0] >= 1     # nor were its anchors dropped


# ---------------------------------------------------------------- Ward round 3, MED-A: clashes count as produced; one import at a time

def test_rebuild_with_id_clashes_removes_nothing(dsn, tmp_path):
    root = tmp_path / "proj"
    for i in range(3):
        synth.claude_code_session(root, f"-p{i}", f"s{i}")
    commands.import_path("claude_code", root, dsn=dsn)
    before = q(dsn, "SELECT id FROM messages ORDER BY id")
    assert len(before) == 24
    synth.claude_code_session(root, "-new", "snew")
    rep = commands.import_path("claude_code", root, dsn=dsn, rebuild=True, yes=True, source="other")
    assert rep.collisions > 0 and rep.removed == 0
    assert any("nothing was removed" in n and "already belongs" in n for n in rep.notes), rep.notes
    after = q(dsn, "SELECT id FROM messages ORDER BY id")
    assert set(before) <= set(after) and len(after) > len(before)


def test_a_second_import_while_the_lock_is_held_is_refused(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("Alpha note text. " * 10)
    with psycopg.connect(dsn, autocommit=True) as other:
        assert other.execute("SELECT pg_try_advisory_lock(hashtext('import:markdown_folder'))").fetchone()[0]
        rep = commands.import_path("markdown_folder", notes, dsn=dsn)
        assert not rep.ok and "already running" in rep.errors[0]
        assert q(dsn, "SELECT count(*) FROM messages")[0][0] == 0
        # a different importer is not blocked
        assert pipeline.run_import(Other(), notes, dsn=dsn).ok
    rep = commands.import_path("markdown_folder", notes, dsn=dsn)        # the lock is gone with its holder
    assert rep.ok and rep.inserted == 1
    assert commands.import_path("markdown_folder", notes, dsn=dsn, force=True).ok   # and the run released its own


class Racer(Importer):
    """One unit; on request, a second process commits a row of the same unit while the run is reading it."""
    name, default_source = "racer", "race"
    late = False

    def units(self, path, opts):
        yield Unit(key="one", files=[path / "o.dat"], root=path)

    def unit_records(self, unit, opts, stats):
        if self.late:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("INSERT INTO messages (id, source, conv_id, ts, speaker, text, seq, importer, unit_key) VALUES "
                          "('racer:late', 'race', 'rc', now(), 'user', 'arrived mid-run', 9, 'racer', 'one')")
        yield Record(id="racer:1", source=self.label(opts), conv_id="rc", ts=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                     speaker="user", text="the row the file produces")


def test_rebuild_never_removes_a_row_imported_after_it_started(dsn, tmp_path):
    (tmp_path / "o.dat").write_text("x")
    racer = Racer()
    racer.dsn = dsn
    assert pipeline.run_import(racer, tmp_path, dsn=dsn).ok
    racer.late = True
    rep = pipeline.run_import(racer, tmp_path, dsn=dsn, rebuild=True, yes=True, force=True)
    assert rep.ok and rep.removed == 0
    assert q(dsn, "SELECT id FROM messages ORDER BY id") == [("racer:1",), ("racer:late",)]
    # the same row, once the run that missed it is over, is an ordinary stale row for the next rebuild
    racer.late = False
    rep = pipeline.run_import(racer, tmp_path, dsn=dsn, rebuild=True, yes=True, force=True)
    assert rep.removed == 1 and q(dsn, "SELECT id FROM messages") == [("racer:1",)]
