"""nexus_markdown: conversations saved as markdown by the Obsidian "Nexus AI Chat Importer" plugin.

One conversation per file, ChatGPT or claude.ai flavour (detected per file).

ChatGPT flavour::

    ---  front matter: conversation_id, aliases (title), create_time, update_time ('MM/DD/YYYY at H:MM AM')  ---
    ### User, on MM/DD/YYYY at H:MM AM;
    #### ChatGPT, on MM/DD/YYYY at H:MM AM;
    <body, every line quoted: '> ' for the user, '>> ' for ChatGPT>      <!-- UID: id -->

claude.ai flavour::

    >[!nexus_user] **User** - DD/MM/YYYY at HH:MM:SS
    >[!nexus_agent] **Assistant** - DD/MM/YYYY at HH:MM:SS
    > body ...      nested '>>[!nexus_attachment] **name**' / '>>[!nexus_artifact] **name** vN'     <!-- UID: id -->

These timestamps are wall-clock time with no zone. They are read in ``--timezone`` (default: this machine's zone) and
stored as UTC. The claude.ai heading is day-first by default; pass ``date_order='mdy'`` if your files are month-first.
The layout above was written from files produced by one plugin version: if headings are not recognised the file is
counted under ``unknown_headings`` in the report rather than guessed at.

Duplicate files for one conversation (``name (1).md``, edited copies) are reduced to one: the latest ``update_time``,
ties go to the larger file.
"""

from __future__ import annotations

import collections
import datetime as dt
import re
from pathlib import Path
from typing import Iterator

from .. import textio
from ..timeutil import parse_iso, wall_to_utc, zone
from . import safety
from .base import Importer, Options, Record, Unit

GPT_HEAD = re.compile(r"^(#{3,4}) (User|ChatGPT), on (\d{2})/(\d{2})/(\d{4}) at (\d{1,2}):(\d{2}) (AM|PM);\s*$")
ANY_GPT_HEAD = re.compile(r"^#{3,4} [A-Za-z][A-Za-z ]*, on \S+ at ")
GPT_FM_TIME = re.compile(r"^(\d{2})/(\d{2})/(\d{4}) at (\d{1,2}):(\d{2}) (AM|PM)$")
CLAUDE_HEAD = re.compile(
    r"^>\[!nexus_(user|agent)\] \*\*(User|Assistant)\*\* - (\d{2})/(\d{2})/(\d{4}) at (\d{2}):(\d{2}):(\d{2})\s*$")
ANY_CLAUDE_HEAD = re.compile(r"^>\[!nexus_[a-z]+\]")
UID = re.compile(r"^<!-- UID: (\S+) -->\s*$")
NESTED_CALLOUT = re.compile(r"^>>\[!nexus_([a-z]+)\]-? \*\*(.*?)\*\*(.*)$")
NESTED_NOISE = re.compile(r"^>> ?(⚠️ Attachment not processed|🎨 \[\[Nexus_attachments/|This block is not supported)")
IMAGE_NAME = re.compile(r"(?i)(\.(png|jpe?g|gif|webp|heic|heif|bmp)$|^(photo|image)(\.\w+)?$|^IMG_|^Screenshot)")

# ChatGPT's memory tool shows up as assistant turns: the note it saved ("User wants ...") and then
# "Model set context updated." Neither is the assistant talking, so both become hidden tool rows.
_MEMORY_DONE = re.compile(r"^\s*Model set context updated\.?\s*$")
_MEMORY_NOTE = re.compile(r"^\s*User\b")

_USER_Q = re.compile(r"^> ?")
_GPT_Q = re.compile(r"^>> ?")


def _h24(h: str, ampm: str) -> int:
    h24 = int(h) % 12
    return h24 + 12 if ampm == "PM" else h24


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
        if v and '\\"' in v:
            v = v.replace('\\"', '"')
    return v


def front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """(fields, index of the first line after the closing '---')."""
    fm: dict[str, str] = {}
    if not lines or lines[0].strip() != "---":
        return fm, 0
    i = 1
    last = None
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if m:
            last = m.group(1)
            fm[last] = _unquote(m.group(2))
        elif last and re.match(r"^\s+-\s+", line) and not fm.get(last):
            fm[last] = _unquote(re.sub(r"^\s+-\s+", "", line))
        i += 1
    return fm, i + 1


def _clean_body(lines: list[str]) -> str:
    lines = list(lines)
    while lines and lines[-1].strip() in ("", "---"):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def flavour(lines: list[str]) -> str | None:
    for line in lines:
        if GPT_HEAD.match(line):
            return "chatgpt"
        if CLAUDE_HEAD.match(line):
            return "claude"
    return None


def _read(path: Path) -> list[str]:
    return textio.read_text(path).split("\n")


def _gpt_time(s: str, tz) -> dt.datetime | None:
    m = GPT_FM_TIME.match(s.strip())
    if not m:
        return None
    mo, d, y, h, mi, ap = m.groups()
    try:
        return wall_to_utc(dt.datetime(int(y), int(mo), int(d), _h24(h, ap), int(mi)), tz)
    except ValueError:
        return None


class NexusMarkdown(Importer):
    name = "nexus_markdown"
    description = "Obsidian Nexus AI Chat Importer markdown (ChatGPT and claude.ai), one conversation per file"
    default_source = ""  # per flavour: chatgpt / claude-ai

    FLAVOUR_SOURCE = {"chatgpt": "chatgpt", "claude": "claude-ai"}
    FLAVOUR_PLATFORM = {"chatgpt": "chatgpt", "claude": "claude.ai"}

    def label_for(self, fl: str, opts: Options) -> str:
        return opts.source or self.FLAVOUR_SOURCE[fl]

    def detect(self, path: Path) -> bool:
        path = Path(path)
        root = path.parent if path.is_file() else path
        files = [path] if path.is_file() else sorted(path.rglob("*.md"))[:25]
        for f in files:
            if f.suffix == ".md" and not safety.problem(f, root):
                try:
                    if flavour(_read(f)):
                        return True
                except OSError:
                    continue
        return False

    # ---- units: one per conversation, after de-duplicating copies

    def units(self, path: Path, opts: Options) -> Iterator[Unit]:
        path = Path(path)
        root = path.parent if path.is_file() else path
        tz = zone(opts.timezone)
        self.skipped = collections.Counter()
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*.md") if p.is_file() or p.is_symlink())
        files = [p for p in files if self.accept(p, root)]
        best: dict[str, tuple[tuple, Path]] = {}
        for p in files:
            lines = _read(p)
            fl = flavour(lines)
            if fl is None:
                continue
            fm, _ = front_matter(lines)
            rel = p.relative_to(root).as_posix()
            conv = fm.get("conversation_id") or "file:" + rel
            upd = _gpt_time(fm.get("update_time", ""), tz) if fl == "chatgpt" else _iso_or_none(fm.get("update_time", ""))
            key = (upd or dt.datetime.min.replace(tzinfo=dt.timezone.utc), p.stat().st_size)
            prev = best.get(conv)
            if prev is None or key > prev[0]:
                best[conv] = (key, p)
        for conv in sorted(best):
            p = best[conv][1]
            yield Unit(key=conv, files=[p], label=p.relative_to(root).as_posix(), root=root)

    def unit_records(self, unit: Unit, opts: Options, stats: collections.Counter) -> Iterator[Record]:
        path = unit.files[0]
        lines = _read(path)
        fl = flavour(lines)
        if fl is None:
            stats["unrecognised_files"] += 1
            return
        stats["files"] += 1
        tz = zone(opts.timezone)
        fm, start = front_matter(lines)
        conv = fm.get("conversation_id") or unit.key
        title = fm.get("aliases") or None
        label = self.label_for(fl, opts)
        platform = self.FLAVOUR_PLATFORM[fl]
        src = unit.label
        msgs = self._parse_gpt(lines[start:], tz, stats) if fl == "chatgpt" else self._parse_claude(lines[start:], tz, opts, stats)
        bodies = [_clean_body(m["body"]) for m in msgs]
        memory: set[int] = set()
        if fl == "chatgpt":
            for i, m in enumerate(msgs):
                if m["who"] != "user" and _MEMORY_DONE.match(bodies[i]):
                    memory.add(i)
                    j = i - 1
                    if j >= 0 and msgs[j]["who"] != "user" and _MEMORY_NOTE.match(bodies[j]):
                        memory.add(j)
        for i, m in enumerate(msgs):
            tags = set(m.get("tags", ()))
            hidden = False
            context = "default"
            if m["who"] == "user":
                speaker = "user"
            elif i in memory:
                speaker, hidden, context = "tool", True, "system"
                tags.add("chatgpt-memory")
                stats["chatgpt_memory_rows"] += 1
            else:
                speaker = "agent"
            mid = m["uid"] or f"n{i}"
            yield Record(id=f"nexus:{conv}:{mid}", source=label, conv_id=conv, ts=m["ts"], speaker=speaker,
                         text=bodies[i], conv_title=title, context=context, platform=platform, hidden=hidden,
                         tags=tags, src=src, order=i)
        stats["conversations"] += 1

    # ---- ChatGPT flavour

    def _parse_gpt(self, lines, tz, stats):
        msgs, cur = [], None
        for line in lines:
            m = GPT_HEAD.match(line)
            if m:
                if cur:
                    msgs.append(cur)
                _, who, mo, d, y, h, mi, ap = m.groups()
                try:
                    ts = wall_to_utc(dt.datetime(int(y), int(mo), int(d), _h24(h, ap), int(mi)), tz)
                except ValueError:
                    stats["bad_timestamps"] += 1
                    cur = None
                    continue
                cur = {"who": "user" if who == "User" else "agent", "ts": ts, "body": [], "uid": None}
                continue
            if ANY_GPT_HEAD.match(line):
                stats["unknown_headings"] += 1
            if cur is None:
                continue
            u = UID.match(line)
            if u:
                cur["uid"] = u.group(1)
                continue
            cur["body"].append((_USER_Q if cur["who"] == "user" else _GPT_Q).sub("", line, count=1))
        if cur:
            msgs.append(cur)
        return msgs

    # ---- claude.ai flavour

    def _parse_claude(self, lines, tz, opts, stats):
        msgs, cur = [], None
        for line in lines:
            m = CLAUDE_HEAD.match(line)
            if m:
                if cur:
                    msgs.append(cur)
                kind, _, a, b, y, h, mi, s = m.groups()
                day, mon = (int(a), int(b)) if opts.date_order != "mdy" else (int(b), int(a))
                try:
                    ts = wall_to_utc(dt.datetime(int(y), mon, day, int(h), int(mi), int(s)), tz)
                except ValueError:
                    stats["bad_timestamps"] += 1
                    cur = None
                    continue
                cur = {"who": kind, "ts": ts, "body": [], "uid": None, "tags": set(), "closed": False}
                continue
            if ANY_CLAUDE_HEAD.match(line):
                stats["unknown_headings"] += 1
            if cur is None or cur["closed"]:
                continue
            u = UID.match(line)
            if u:
                cur["uid"] = u.group(1)
                cur["closed"] = True
                continue
            if NESTED_NOISE.match(line):
                continue
            n = NESTED_CALLOUT.match(line)
            if n:
                kind, name, rest = n.groups()
                name = name.strip()
                cur["tags"].add(("image" if IMAGE_NAME.search(name) else "file") if kind == "attachment" else kind)
                cur["body"].append(f"[{kind}: {name}{rest.rstrip()}]")
                continue
            if line.startswith("> "):
                line = line[2:]
            elif line.startswith(">"):
                line = line[1:]
            cur["body"].append(line)
        if cur:
            msgs.append(cur)
        return msgs


def _iso_or_none(s: str) -> dt.datetime | None:
    try:
        return parse_iso(s) if s else None
    except ValueError:
        return None
