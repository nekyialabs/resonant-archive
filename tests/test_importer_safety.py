"""What the importers will and will not read, and what the pipeline stores: LOW-3 and LOW-4 of the security pass."""
from __future__ import annotations

import collections
import json
import os
import re
import sys
from pathlib import Path

import psycopg
import pytest

from resonant_archive import commands, importers, pipeline
from resonant_archive.importers import safety
from resonant_archive.importers.base import Importer, Options, Record, Unit
from resonant_archive.redact import Redactor
from resonant_archive.secret_sources import INPUT_WITHHELD, WITHHELD, secret_bearing, secret_call_summary

from . import synth
from .symhelp import symlink_or_skip
from .secret_cases import ORDINARY_CALLS, SECRET_CALLS

BASE = {"sessionId": "s1", "entrypoint": "cli"}
STRIPE = "sk_live_" + "a1B2" * 6


def q(dsn, sql, *args):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args or None).fetchall()


def user(uuid, content, t="2026-03-01T10:00:00.000Z"):
    return {**BASE, "type": "user", "uuid": uuid, "timestamp": t, "message": {"role": "user", "content": content}}


def assistant(uuid, blocks, t="2026-03-01T10:00:01.000Z"):
    return {**BASE, "type": "assistant", "uuid": uuid, "timestamp": t,
            "message": {"role": "assistant", "model": "m", "content": blocks}}


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, content):
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


# ---------------------------------------------------------------- LOW-3: redact before cutting

def test_a_secret_near_the_cut_is_redacted_before_the_cut(dsn, tmp_path):
    secret = "sk-ant-" + "Zq8" * 10
    cmd = "echo " + "x" * 188 + " " + secret       # the old cut at 200 characters left "sk-ant-Zq8Z" behind
    synth.jl(tmp_path / "p" / "s1.jsonl", [user("u1", "hi"), assistant("a1", [tool_use("t1", "Bash", {"command": cmd})])])
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn)
    assert rep.ok
    (text,) = q(dsn, "SELECT text FROM messages WHERE speaker = 'tool'")[0]
    assert "sk-ant" not in text and "Zq8" not in text and text.startswith("Bash: ")
    assert len(text) <= len("Bash: ") + 200


def test_tool_input_lines_are_redacted_by_structure_before_json_escapes_them(dsn, tmp_path):
    cmd = "cd /srv\nexport DB_TOKEN=abcdefghijklmnopqrstuvwxyz\nrun"
    synth.jl(tmp_path / "p" / "s1.jsonl", [user("u1", "hi"), assistant("a1", [tool_use("t1", "Bash", {"command": cmd})])])
    commands.import_path("claude_code", tmp_path, dsn=dsn)
    (text,) = q(dsn, "SELECT text FROM messages WHERE speaker = 'tool'")[0]
    assert "abcdefghijklmnop" not in text


# ---------------------------------------------------------------- LOW-3: secret-bearing sources are withheld

@pytest.mark.parametrize("name,inp", SECRET_CALLS, ids=lambda v: str(v)[:60])
def test_secret_bearing_calls(name, inp):
    assert secret_bearing(name, inp)


@pytest.mark.parametrize("name,inp", ORDINARY_CALLS, ids=lambda v: str(v)[:60])
def test_ordinary_calls_are_not_secret_bearing(name, inp):
    assert not secret_bearing(name, inp)


def test_secret_cases_match_the_mod():
    """The case lists in the chronicle mod's test are generated from tests/secret_cases.py: they must be equal."""
    ts = Path(__file__).resolve().parents[1] / "claude-code" / "chronicle" / "tests" / "redact.test.ts"
    if not ts.exists():
        pytest.skip("the chronicle mod is not in this checkout")
    block = re.search(r"// SECRET-CASES-BEGIN[^\n]*\n(.*?)// SECRET-CASES-END", ts.read_text(encoding="utf-8"), re.S).group(1)
    got = {}
    for m in re.finditer(r"const (\w+): \[string, unknown\]\[\] = \[\n(.*?)\n\]", block, re.S):
        got[m.group(1)] = [tuple(json.loads("[" + line.strip().removeprefix("[").removesuffix("],").removesuffix("]") + "]"))
                           for line in m.group(2).splitlines()]
    assert got["SECRET_CALLS"] == [tuple(c) for c in SECRET_CALLS]
    assert got["ORDINARY_CALLS"] == [tuple(c) for c in ORDINARY_CALLS]


HOSTILE = {   # shapes that made an earlier version of a rule quadratic; each must be scanned in well under a second
    "quotes": ("'" * 200_000 + "env"), "service account": "/" + "service-account-" * 15_000 + "!",
    "tfstate": "terraform.tfstate" * 15_000 + "!", "begin": "BEGIN " * 50_000, "env dots": ".env" * 60_000 + "!",
    "sudo flags": "sudo " + "-a " * 80_000, "assignments": "a=b " * 60_000 + "env", "proc": "/proc/x" * 40_000,
    # Windows rules (2.0.2)
    "get-process": "get-process x " * 40_000, "gps": "gps " * 120_000, "reg query": "reg query " * 50_000,
    "cmd flags": "cmd " + "/a " * 80_000, "powershell flags": "powershell " + "-a " * 80_000,
    "gci": "gci " * 120_000, "percent": "%A_KEY" * 70_000, "env colon": "$env:" * 90_000, "wmic": "wmic process " * 30_000,
    "encoded": "powershell -enc " + "A" * 400_000, "many encoded": "powershell -enc " + "AAAAAAAAAAAAAAAAAAAA " * 20_000,
    "netsh": "netsh wlan show profile " * 20_000, "hklm": "HKLM:\\" * 70_000, "cmd.exe": "cmd.exe " * 60_000,
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_secret_source_rules_are_linear_on_hostile_input(name):
    import time

    v = HOSTILE[name]
    t0 = time.time()
    secret_bearing("Bash", {"command": v})
    secret_bearing("Read", {"file_path": v})
    secret_bearing("Grep", {"pattern": v})
    secret_call_summary("Bash", {"command": v})
    assert time.time() - t0 < 3


# 2.0.1: the quotes around a `sh -c` body were trimmed with a regex ending in `$`, quadratic on a long run of quotes or
# spaces inside the body (13 s at 50,000). Each 500 KB shape must now be scanned in under a second.
N = 500_000
HOSTILE_SHELL_C = {
    "double quotes": 'bash -c "x' + '"' * N + 'y"', "single quotes": "sh -c 'x" + "'" * N + "y'",
    "mixed quotes": "bash -c \"x" + "\"'" * (N // 2) + "y\"", "spaces": 'bash -c "x' + " " * N + 'y"',
    "quotes and spaces": "zsh -c 'x" + "' \t" * (N // 3) + "y'", "unicode spaces": 'bash -c "x' + "\u3000\xa0" * (N // 2) + 'y"',
    "sudo": 'sudo -E bash -c "x' + '"' * N + 'y"', "nested": "bash -c \"sh -c 'x" + "'" * N + "y'\"",
    "flags": 'bash -l -e -c "x' + '"' * N + 'y"', "no closing": 'bash -c "x' + '"' * N + "y",
}


@pytest.mark.parametrize("name", sorted(HOSTILE_SHELL_C))
def test_a_shell_c_body_is_trimmed_in_linear_time(name):
    import time

    v = HOSTILE_SHELL_C[name]
    t0 = time.time()
    assert not secret_bearing("Bash", {"command": v})
    secret_call_summary("Bash", {"command": v})
    assert time.time() - t0 < 1


def test_a_hostile_shell_c_body_is_still_read():
    assert secret_bearing("Bash", {"command": 'bash -c "' + '"' * N + "env" + '"' * N + '"'})
    assert secret_bearing("Bash", {"command": "sh -c '" + "' " * (N // 2) + "printenv'"})


def test_the_shell_c_trim_gives_what_the_old_regex_gave():
    import random
    import sys

    from resonant_archive.secret_sources import _QUOTES_AND_SPACE, _trim_quotes

    def old(s):
        return re.sub(r"^[\"'\s]+|[\"'\s]+$", "", s)

    assert set(_QUOTES_AND_SPACE) == {'"', "'"} | {c for c in map(chr, range(sys.maxunicode + 1)) if re.match(r"\s", c)}
    normal = ["env", "'env'", '"cd x && env"', "  'ls -la'  ", "\"'printenv'\"", "'", '""', "", " \t\n", "'a' 'b'",
              "\u3000env\xa0", "x'y\"z", "'echo \"hi there\"'", "'\t'cmd'\n'"]
    rng = random.Random(201)
    normal += ["".join(rng.choice("\"' \t\nab;\u3000\x85") for _ in range(rng.randrange(12))) for _ in range(3000)]
    for s in normal:
        assert _trim_quotes(s) == old(s), repr(s)


# ---------------------------------------------------------------- Ward round 2, MED-B: a secret-bearing call keeps no content

VALUE = "hunter2hunter2zq"


def test_summary_of_a_secret_call_keeps_the_file_or_the_program_only():
    assert secret_call_summary("Write", {"file_path": "/app/.env", "content": f"DB_NOTE={VALUE}"}) == "/app/.env"
    assert secret_call_summary("Edit", {"file_path": "/app/.env", "old_string": VALUE, "new_string": VALUE}) == "/app/.env"
    assert secret_call_summary("Bash", {"command": f"echo plain{VALUE} >> .env"}) == "echo [arguments withheld] .env"
    assert secret_call_summary("Bash", {"command": "env"}) == "env"
    out = secret_call_summary("Bash", {"command": f"cat > .env <<'EOF'\nTOKEN_X={VALUE}\nEOF"})
    assert VALUE not in out and out.startswith("cat")
    assert secret_call_summary("Grep", {"pattern": f"password={VALUE}", "path": "/srv"}) == "/srv"
    assert secret_call_summary("Grep", {"pattern": f"password={VALUE}"}) == INPUT_WITHHELD
    assert VALUE not in secret_call_summary("Bash", {"command": f"API_TOKEN={VALUE} printenv"})


@pytest.mark.parametrize("name,inp", [
    ("Write", {"file_path": "/app/.env", "content": f"DB_NOTE={VALUE}\nX=1"}),
    ("Bash", {"command": f"echo plain{VALUE} >> .env"}),
    ("Edit", {"file_path": "/app/.env", "old_string": f"A=old{VALUE}", "new_string": f"A=new{VALUE}"}),
    ("Bash", {"command": f"cat > .env <<'EOF'\nTOKEN_X={VALUE}\nEOF"}),
])
def test_the_stored_input_of_a_secret_call_has_no_content(dsn, tmp_path, name, inp):
    synth.jl(tmp_path / "p" / "s1.jsonl", [user("u1", "hi"), assistant("a1", [tool_use("t1", name, inp)]),
                                          user("u2", [tool_result("t1", f"echoed {VALUE}")], t="2026-03-01T10:00:02.000Z")])
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn)
    assert rep.ok and rep.importer_stats["inputs_withheld"] == 1 and rep.importer_stats["results_withheld"] == 1
    rows = [r[0] for r in q(dsn, "SELECT text FROM messages WHERE speaker = 'tool' ORDER BY text")]
    assert len(rows) == 2 and not any(VALUE in r for r in rows) and any(r.startswith(name + ": ") for r in rows)
    assert not q(dsn, "SELECT 1 FROM messages WHERE text LIKE %s", f"%{VALUE}%")


def test_results_of_secret_calls_are_withheld_and_others_kept(dsn, tmp_path):
    synth.jl(tmp_path / "p" / "s1.jsonl", [
        user("u1", "show me"),
        assistant("a1", [tool_use("t1", "Read", {"file_path": "/app/.env"}),
                         tool_use("t2", "Bash", {"command": "env"}),
                         tool_use("t3", "Read", {"file_path": "/app/main.py"})]),
        user("u2", [tool_result("t1", "PLAIN_VALUE_NOBODY_CAN_PATTERN=zebra-giraffe-2"),
                    tool_result("t2", "HOME=/home/x\nSOMETHING=quietly-secret"),
                    tool_result("t3", "print('hello')")], t="2026-03-01T10:00:02.000Z"),
    ])
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn)
    assert rep.importer_stats["results_withheld"] == 2
    rows = [r[0] for r in q(dsn, "SELECT text FROM messages WHERE speaker = 'tool' AND text LIKE '[result%'")]
    assert sorted(rows) == sorted([f"[result:Read] {WITHHELD}", f"[result:Bash] {WITHHELD}", "[result:Read] print('hello')"])
    assert not q(dsn, "SELECT 1 FROM messages WHERE text LIKE '%zebra-giraffe%' OR text LIKE '%quietly-secret%'")


# ---------------------------------------------------------------- LOW-3: every stored string is redacted

def test_every_stored_column_is_redacted(dsn, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / f"{STRIPE}.md").write_text(f"---\ntitle: the password is hunter2hunter2\ntags: [{STRIPE}]\n---\n" + "body text here. " * 20)
    rep = commands.import_path("markdown_folder", notes, dsn=dsn, source=f"label-{STRIPE}")
    assert rep.ok and rep.kept > 0
    dump = q(dsn, "SELECT row_to_json(m)::text FROM messages m UNION ALL SELECT row_to_json(c)::text FROM conversations c "
                  "UNION ALL SELECT row_to_json(s)::text FROM sources s UNION ALL SELECT token FROM anchors")
    blob = "\n".join(r[0] for r in dump)
    assert "a1B2a1B2" not in blob and "hunter2hunter2" not in blob
    ids = q(dsn, "SELECT id, conv_id FROM messages")
    assert all(i.startswith("md:") or i.startswith("redacted-") for i, _ in ids)
    assert all(c.startswith("redacted-") for _, c in ids)


def test_a_redacted_id_is_stable_and_distinct():
    r = Redactor()
    a = pipeline._scrub_id(r, f"notes/{STRIPE}.md")
    b = pipeline._scrub_id(r, f"notes/{STRIPE}.md")
    c = pipeline._scrub_id(r, "notes/sk_live_" + "z9Y8" * 6 + ".md")
    assert a == b and a != c and a.startswith("redacted-") and STRIPE not in a
    assert pipeline._scrub_id(r, "claude-code:s1:u1:toolu_01AbCdEfGhIjKlMnOpQrStUvWxYz") == \
        "claude-code:s1:u1:toolu_01AbCdEfGhIjKlMnOpQrStUvWxYz"      # a long mixed-case id is not a secret
    assert pipeline._scrub_id(r, None) is None


def test_cut_after_redaction_in_clean():
    rec = Record(id="x", source="s", conv_id="c", ts=synth_ts(), speaker="tool", text="head: " + "y" * 10 + " " + "sk-" + "Q9" * 20,
                 cut=len("head: ") + 14)
    rep = pipeline.ImportReport()
    (out,), _ = pipeline.clean([rec], Redactor(), rep)
    assert "sk-" not in out.text and out.text.startswith("head: yyyyyyyyyy ")


def synth_ts():
    import datetime as dt
    return dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- LOW-4: what may be read

def test_symlinked_files_are_not_followed(dsn, tmp_path):
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir()
    outside.write_text("SECRET-IN-A-FILE-OUTSIDE " * 40)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "real.md").write_text("an ordinary note " * 30)
    symlink_or_skip(outside, notes / "link.md")
    rep = commands.import_path("markdown_folder", notes, dsn=dsn)
    assert rep.ok and rep.importer_stats["skipped symlink"] == 1
    assert not q(dsn, "SELECT 1 FROM messages WHERE text LIKE '%SECRET-IN-A-FILE%'")
    assert q(dsn, "SELECT count(DISTINCT conv_id) FROM messages")[0][0] == 1


def test_a_symlinked_project_folder_pointing_outside_is_skipped(dsn, tmp_path):
    real = tmp_path / "elsewhere" / "proj"
    synth.jl(real / "s1.jsonl", [user("u1", "OUTSIDE-ROOT-CONTENT hello there")])
    root = tmp_path / "projects"
    root.mkdir()
    symlink_or_skip(real, root / "proj", directory=True)
    rep = commands.import_path("claude_code", root, dsn=dsn) if importers.detect(root) else None
    found = list(importers.get("claude_code").units(root, Options()))
    assert found == []
    assert rep is None or rep.kept == 0


def test_symlinked_jsonl_is_skipped_by_every_jsonl_importer(tmp_path):
    outside = synth.jl(tmp_path / "out" / "x.jsonl", [user("u1", "hello")])
    d = tmp_path / "cc" / "proj"
    d.mkdir(parents=True)
    symlink_or_skip(outside, d / "s1.jsonl")
    imp = importers.get("claude_code")
    assert list(imp.units(tmp_path / "cc", Options())) == [] and imp.skipped["symlink"] == 1
    assert importers.detect(tmp_path / "cc") == []
    day = tmp_path / "mirror" / "2026-04-01"
    day.mkdir(parents=True)
    symlink_or_skip(outside, day / "sid.jsonl")
    imp = importers.get("chronicle_mirror")
    assert list(imp.units(tmp_path / "mirror", Options())) == [] and imp.skipped["symlink"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are a Windows thing")
def test_a_directory_junction_pointing_outside_is_not_read(tmp_path):
    """A junction is not a symlink to Python (is_symlink() is False and os.walk descends into it, and `mklink /J` needs
    no privilege), so only the resolve-inside-the-root check stands between an archive folder and a folder elsewhere."""
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET-IN-A-FOLDER-OUTSIDE " * 40)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "real.md").write_text("an ordinary note " * 30)
    _winapi.CreateJunction(str(outside), str(notes / "linked"))
    imp = importers.get("markdown_folder")
    keys = [u.key for u in imp.units(notes, Options())]
    assert keys == ["real.md"] and imp.skipped["outside root"] == 1


def test_oversized_file_is_skipped_not_truncated(dsn, tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "MAX_FILE_BYTES", 2000)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "small.md").write_text("small note " * 20)
    (notes / "big.md").write_text("big note " * 1000)
    rep = commands.import_path("markdown_folder", notes, dsn=dsn)
    assert rep.importer_stats["skipped too large"] == 1 and rep.units_total == 1
    assert q(dsn, "SELECT count(DISTINCT conv_id) FROM messages")[0][0] == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Windows has no FIFOs")
def test_fifo_is_not_read(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    os.mkfifo(notes / "pipe.md")
    assert safety.problem(notes / "pipe.md", notes) == "not a regular file"
    assert list(importers.get("markdown_folder").units(notes, Options())) == []


def test_long_lines_are_skipped_and_counted(dsn, tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "MAX_LINE_CHARS", 500)
    synth.jl(tmp_path / "p" / "s1.jsonl", [user("u1", "first"), user("u2", "x" * 5000, "2026-03-01T10:00:01.000Z"),
                                           user("u3", "third", "2026-03-01T10:00:02.000Z")])
    rep = commands.import_path("claude_code", tmp_path, dsn=dsn)
    assert rep.importer_stats["long_lines"] == 1
    assert sorted(r[0] for r in q(dsn, "SELECT text FROM messages")) == ["first", "third"]


def test_iter_lines_edges():
    import io
    st = collections.Counter()
    got = list(safety.iter_lines(io.StringIO("ab\n" + "x" * 30 + "\ncd\n" + "e" * 10 + "\nlast"), st, limit=10))
    assert got == [(0, "ab\n"), (2, "cd\n"), (3, "eeeeeeeeee\n"), (4, "last")] and st["long_lines"] == 1
    assert list(safety.iter_lines(io.StringIO("x" * 25), st, limit=10)) == [] and st["long_lines"] == 2


def test_deeply_nested_json_does_not_crash_detect_or_import(dsn, tmp_path):
    nest = "[" * 200_000 + "]" * 200_000
    d = tmp_path / "mirror" / "2026-04-01"
    d.mkdir(parents=True)
    (d / "sid.jsonl").write_text(nest + "\n")
    assert importers.detect(tmp_path / "mirror") == []                 # detect() absorbs it
    assert importers.get("chronicle_mirror").detect(tmp_path / "mirror") is False
    cc = tmp_path / "cc" / "proj"
    cc.mkdir(parents=True)
    (cc / "s1.jsonl").write_text(nest + "\n" + '{"type":"user","sessionId":"s1","uuid":"u","timestamp":"2026-03-01T10:00:00Z",'
                                 '"message":{"role":"user","content":"after the nest"}}\n')
    rep = commands.import_path("claude_code", tmp_path / "cc", dsn=dsn)
    assert rep.importer_stats["bad_lines"] == 1 and rep.kept == 1


def test_pipeline_rechecks_a_unit_a_plugin_listed(dsn, tmp_path):
    target = tmp_path / "t.md"
    target.write_text("x")
    link = tmp_path / "root" / "l.md"
    link.parent.mkdir()
    symlink_or_skip(target, link)

    class Sloppy(Importer):
        name, default_source = "sloppy", "sloppy"

        def units(self, path, opts):
            yield Unit(key="l.md", files=[link], root=link.parent)

        def unit_records(self, unit, opts, stats):  # pragma: no cover - must not be reached
            raise AssertionError("read a symlink")

    rep = pipeline.run_import(Sloppy(), link.parent, dsn=dsn)
    assert rep.units_skipped == 1 and rep.importer_stats["skipped symlink"] == 1 and rep.ok
