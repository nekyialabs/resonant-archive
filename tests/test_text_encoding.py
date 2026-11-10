"""Files as Windows tools write them: a UTF-8 BOM (Notepad, PowerShell 5.1 `Set-Content -Encoding utf8`), UTF-16
(PowerShell 5.1 `>` and `Out-File`), CRLF line endings. None of these may change what is read."""
from __future__ import annotations

import json

import pytest

from resonant_archive import chunker, commands, redact, textio
from resonant_archive.importers import chronicle_mirror, claude_code, nexus_markdown

BOM8 = b"\xef\xbb\xbf"

MD = "---\ntitle: Window Notes\ncreated: 2026-11-10\n---\nFirst paragraph.\n\nSecond paragraph é€.\n"

ENCODINGS = {
    "utf-8": lambda s: s.encode("utf-8"),
    "utf-8 + BOM": lambda s: BOM8 + s.encode("utf-8"),
    "utf-8, CRLF": lambda s: s.replace("\n", "\r\n").encode("utf-8"),
    "utf-8 + BOM, CRLF": lambda s: BOM8 + s.replace("\n", "\r\n").encode("utf-8"),
    "utf-16 LE + BOM, CRLF": lambda s: b"\xff\xfe" + s.replace("\n", "\r\n").encode("utf-16-le"),
    "utf-16 BE + BOM": lambda s: b"\xfe\xff" + s.encode("utf-16-be"),
}


@pytest.mark.parametrize("how", sorted(ENCODINGS))
def test_markdown_front_matter_and_body_survive_every_encoding(tmp_path, how):
    f = tmp_path / "note.md"
    f.write_bytes(ENCODINGS[how](MD))
    parsed = chunker.parse_file(f)
    assert parsed.metadata["title"] == "Window Notes"             # the --- block was seen as front matter
    assert parsed.body.startswith("First paragraph.") and "Second paragraph é€." in parsed.body
    assert "\r" not in parsed.body and "﻿" not in parsed.body


@pytest.mark.parametrize("how", sorted(ENCODINGS))
def test_textio_gives_the_same_text(tmp_path, how):
    f = tmp_path / "t.txt"
    f.write_bytes(ENCODINGS[how]("a\nb é\n"))
    assert textio.read_text(f) == "a\nb é\n"


def test_invalid_bytes_become_replacement_characters_not_an_exception(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes(b"ok \xff\xfe\xfd end")
    assert textio.read_text(f).startswith("ok ") and textio.read_text(f).endswith(" end")


@pytest.mark.parametrize("how", ["utf-8 + BOM, CRLF", "utf-16 LE + BOM, CRLF"])
def test_known_secrets_are_read_from_an_env_file_written_by_powershell(tmp_path, how):
    secret = "Zk9mQ2xLp7RtVw3YbN5c"
    f = tmp_path / ".env"
    f.write_bytes(ENCODINGS[how](f"API_TOKEN={secret}\nOTHER=short\n"))
    assert secret in redact.load_known([str(f)])


@pytest.mark.parametrize("how", ["utf-8 + BOM, CRLF", "utf-16 LE + BOM, CRLF"])
def test_an_env_file_with_a_bom_keeps_its_first_key(tmp_path, how):
    f = tmp_path / ".env"
    pw = "Zq8vN2pXk4TjR7mWc1LsHd9B"
    f.write_bytes(ENCODINGS[how](f"POSTGRES_PASSWORD={pw}\nRESONANT_ARCHIVE_PORT=54329\n"))
    assert commands.env_file_state(tmp_path) == "ok"             # the password was found under its own name
    assert commands.write_env_file(tmp_path)[1] == pw            # and an existing good password is kept


def _transcript_line(i):
    return json.dumps({"type": "user" if i % 2 == 0 else "assistant", "sessionId": "s1", "uuid": f"u{i}",
                       "timestamp": "2026-11-10T10:00:00Z", "message": {"role": "user", "content": "hi"}})


@pytest.mark.parametrize("how", ["utf-8 + BOM", "utf-8, CRLF", "utf-8 + BOM, CRLF"])
def test_claude_code_transcripts_with_a_bom_or_crlf_are_recognised_and_read(tmp_path, how):
    proj = tmp_path / "C--Users-someone-project"                    # Claude Code's encoded folder name on Windows
    proj.mkdir()
    f = proj / "s1.jsonl"
    f.write_bytes(ENCODINGS[how]("\n".join(_transcript_line(i) for i in range(3)) + "\n"))
    assert claude_code._looks_like_transcript(f)
    assert claude_code.ClaudeCode().detect(tmp_path)


def test_chronicle_mirror_with_a_bom_is_recognised(tmp_path):
    day = tmp_path / "2026-11-10"
    day.mkdir()
    row = json.dumps({"t": 1762761600000, "sid": "s1", "role": "user", "text": "hi"})
    (day / "s1.jsonl").write_bytes(BOM8 + (row + "\r\n").encode("utf-8"))
    assert chronicle_mirror.ChronicleMirror().detect(tmp_path)


def test_nexus_markdown_reads_crlf_and_a_bom(tmp_path):
    f = tmp_path / "chat.md"
    f.write_bytes(BOM8 + b"line one\r\nline two\r\n")
    assert nexus_markdown._read(f) == ["line one", "line two", ""]


def _as_windows_wrote_it(root, how):
    """Rewrite every .jsonl under ``root`` with a BOM and/or CRLF endings, in place."""
    for f in root.rglob("*.jsonl"):
        text = f.read_bytes().decode("utf-8")
        f.write_bytes(ENCODINGS[how](text))


@pytest.mark.parametrize("how", ["utf-8 + BOM", "utf-8, CRLF", "utf-8 + BOM, CRLF"])
def test_importers_give_the_same_rows_whatever_the_line_endings_or_bom(tmp_path, how):
    """The same session, as Windows might have written it (BOM, CRLF), imports to exactly the same rows."""
    import collections

    from resonant_archive import importers
    from resonant_archive.importers.base import Options, Record

    from . import synth

    def rows(importer, path):
        out = list(importers.get(importer).records(path, Options(), collections.Counter()))
        return sorted((r.id, r.text) for r in out if isinstance(r, Record))

    cc_plain = synth.claude_code_session(tmp_path / "plain" / "cc", project="C--Users-someone-demo")
    mirror_plain = synth.mirror_tree(tmp_path / "plain" / "mirror")
    cc_win = synth.claude_code_session(tmp_path / "win" / "cc", project="C--Users-someone-demo")
    mirror_win = synth.mirror_tree(tmp_path / "win" / "mirror")
    _as_windows_wrote_it(tmp_path / "win", how)

    assert rows("claude_code", cc_win.parent.parent) == rows("claude_code", cc_plain.parent.parent) != []
    assert rows("chronicle_mirror", mirror_win) == rows("chronicle_mirror", mirror_plain) != []
