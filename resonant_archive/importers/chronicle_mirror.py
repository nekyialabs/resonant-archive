"""chronicle_mirror: the chronicle mod's on-disk mirror (format: docs/MIRROR-FORMAT.md).

The mod writes every row of every Claude Code session, already redacted at the source, to::

    <root>/<day>/<session id>.jsonl            rows and event lines
    <root>/<day>/<session id>.memories.jsonl   chronicle L1 memories

A session that crosses midnight has files in more than one day folder. One session (all its files) is one unit.

Mapping:

- ``role: assistant`` rows -> visible ``agent`` rows. ``tools`` and ``results`` strings -> hidden ``tool`` rows.
- ``role: user`` rows from the composer or a bridge (door ``prompt``) -> visible ``user`` rows; a prompt typed while
  the assistant was working (``queued: prompt``) is tagged ``mid-turn``. Origin ``peer`` -> ``peer``.
  Everything else (commands, notes, meta, plugin or task input, the mod's own memory blocks) -> hidden ``system``.
- ``event: compaction`` lines carrying a summary -> a hidden ``system`` row tagged ``summary`` and ``compaction``.
- ``event: memory`` lines -> the ``memories`` table.
- Other event lines (session_fresh, memory_formed, mirror_dropped, ...) are bookkeeping and only counted.

Rows the mod wrote twice (a retried flush) carry the same uuid and collapse to one. A line cut short by a crash is
counted as ``bad_lines`` and skipped.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Iterator

from ..timeutil import from_epoch_ms
from . import safety
from .base import Importer, Memory, Options, Record, Unit

DAY = re.compile(r"^(\d{4}-\d{2}-\d{2}|undated)$")
MEMORIES_SUFFIX = ".memories.jsonl"
PROMPT_ORIGINS = {"composer", "bridge", "", None}


def _session_of(name: str) -> str:
    return name[: -len(MEMORIES_SUFFIX)] if name.endswith(MEMORIES_SUFFIX) else name[: -len(".jsonl")]


def _day_dirs(path: Path) -> list[Path]:
    if path.is_dir() and DAY.match(path.name):
        return [path]
    return sorted(p for p in path.iterdir() if p.is_dir() and DAY.match(p.name))


def _ms(v) -> "object | None":
    try:
        return from_epoch_ms(float(v)) if v not in (None, "", 0) else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class ChronicleMirror(Importer):
    name = "chronicle_mirror"
    description = "The chronicle mod's mirror folder (<root>/<day>/<session>.jsonl and .memories.jsonl)"
    default_source = "chronicle"

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if not path.is_dir():
            return False
        for d in _day_dirs(path):
            for f in sorted(d.glob("*.jsonl"))[:5]:
                if safety.problem(f, path):
                    continue
                try:
                    with open(f, encoding="utf-8-sig", errors="replace") as fh:
                        first = json.loads(fh.readline(safety.MAX_LINE_CHARS))
                except (OSError, ValueError, RecursionError):
                    continue
                if isinstance(first, dict) and "t" in first and ("sid" in first or "event" in first or "session" in first):
                    return True
        return False

    def units(self, path: Path, opts: Options) -> Iterator[Unit]:
        path = Path(path)
        root = path.parent if (path.is_dir() and DAY.match(path.name)) else path
        groups: dict[str, list[Path]] = collections.defaultdict(list)
        self.skipped = collections.Counter()
        for d in _day_dirs(path):
            for f in sorted(d.glob("*.jsonl")):
                if self.accept(f, root):
                    groups[_session_of(f.name)].append(f)
        for sid in sorted(groups):
            yield Unit(key=sid, files=groups[sid], label=sid, root=root)

    def unit_records(self, unit: Unit, opts: Options, stats: collections.Counter) -> Iterator[Record | Memory]:
        label = self.label(opts)
        seen: set[str] = set()
        for f in unit.files:
            src = f"{f.parent.name}/{f.name}"
            with open(f, encoding="utf-8-sig", errors="replace") as fh:
                for ln, line in safety.iter_lines(fh, stats):
                    if not line.strip():
                        continue
                    try:
                        o = json.loads(line)
                    except (ValueError, RecursionError):
                        stats["bad_lines"] += 1
                        continue
                    if not isinstance(o, dict):
                        stats["bad_lines"] += 1
                        continue
                    ts = _ms(o.get("t"))
                    if ts is None:
                        stats["no_timestamp"] += 1
                        continue
                    sid = str(o.get("sid") or o.get("session") or unit.key)
                    ev = o.get("event")
                    if ev is not None:
                        yield from self._event(o, ev, sid, ts, label, src, ln, seen, stats)
                    else:
                        yield from self._row(o, sid, ts, label, src, ln, seen, stats)

    # ---- events

    def _event(self, o, ev, sid, ts, label, src, ln, seen, stats):
        if ev == "memory":
            text = o.get("text")
            if not isinstance(text, str) or not text.strip():
                stats["memories_empty"] += 1
                return
            ordinal = o.get("ordinal") if isinstance(o.get("ordinal"), int) else None
            tail = o.get("source_hash") or o.get("to_uuid") or str(o.get("t"))
            mid = f"memory:{sid}:{ordinal if ordinal is not None else 'x'}:{tail}"
            if mid in seen:
                stats["duplicates_in_input"] += 1
                return
            seen.add(mid)
            yield Memory(
                id=mid, source=label, sid=sid, ts=ts, text=text, level=int(o.get("level") or 1), ordinal=ordinal,
                t_from=_ms(o.get("t_from")), t_to=_ms(o.get("t_to")), from_uuid=o.get("from_uuid"),
                to_uuid=o.get("to_uuid"), est_tokens=o.get("est_tokens") if isinstance(o.get("est_tokens"), int) else None,
                rows=o.get("rows") if isinstance(o.get("rows"), int) else None, source_hash=o.get("source_hash"))
            return
        if ev == "compaction":
            summary = o.get("summary")
            if isinstance(summary, str) and summary.strip():
                rid = f"chronicle:{sid}:compaction:{o.get('t')}"
                if rid in seen:
                    stats["duplicates_in_input"] += 1
                    return
                seen.add(rid)
                yield Record(id=rid, source=label, conv_id=sid, ts=ts, speaker="system", text=summary, hidden=True,
                             context="system", platform="cli", tags={"summary", "compaction"}, src=src, order=ln * 4)
                return
        stats["events_ignored"] += 1

    # ---- rows

    def _row(self, o, sid, ts, label, src, ln, seen, stats):
        uuid = o.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            stats["rows_without_uuid"] += 1
            return
        base = f"chronicle:{sid}:{uuid}"
        if base in seen:
            stats["duplicates_in_input"] += 1
            return
        seen.add(base)
        text = o.get("text") if isinstance(o.get("text"), str) else ""
        tools = [x for x in o.get("tools", []) if isinstance(x, str)] if isinstance(o.get("tools"), list) else []
        results = [x for x in o.get("results", []) if isinstance(x, str)] if isinstance(o.get("results"), list) else []
        role = o.get("role")
        door = o.get("door")
        origin = o.get("origin") if isinstance(o.get("origin"), str) else None
        queued = o.get("queued")
        chron = o.get("chronicle")
        common = dict(source=label, conv_id=sid, ts=ts, platform="cli", src=src)

        if text.strip():
            tags: set[str] = set()
            hidden, context = False, "default"
            if chron in ("memories", "ack", "fork"):
                speaker, hidden, context = "system", True, "system"
                tags |= {"chronicle", chron}
            elif queued:
                if queued == "prompt":
                    speaker = "user"
                    tags.add("mid-turn")
                else:
                    speaker, hidden, context = "system", True, "system"
                    tags.add("queued")
            elif role == "assistant":
                speaker = "agent"
            elif o.get("meta") or origin in ("plugin", "task-notification"):
                speaker, hidden, context = "system", True, "system"
                tags.add("meta" if o.get("meta") else origin)
            elif origin == "peer":
                speaker, context = "peer", "peer"
                tags.add("peer")
            elif door == "prompt" and origin in PROMPT_ORIGINS:
                speaker = "user"
            else:
                speaker, hidden, context = "system", True, "system"
                tags.add(f"door:{door}" if door else "system")
            yield Record(id=base, speaker=speaker, text=text, hidden=hidden, context=context, tags=tags,
                         order=ln * 4, **common)
        for k, t in enumerate(tools):
            yield Record(id=f"{base}:tool:{k}", speaker="tool", text=t, hidden=True, context="system",
                         tags={"tool-call"}, order=ln * 4 + 1, **common)
        for k, t in enumerate(results):
            yield Record(id=f"{base}:result:{k}", speaker="tool", text=f"[result] {t}", hidden=True, context="system",
                         tags={"tool-result"}, order=ln * 4 + 2, **common)
