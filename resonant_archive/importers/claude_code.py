"""claude_code: Claude Code's own session transcripts (``~/.claude/projects/<project>/<session>.jsonl``).

What is kept, per session file:

- ``user`` / ``assistant`` lines that are not sidechains (sub-agent traffic is skipped).
- Assistant text blocks become visible ``agent`` rows. Summarised thinking blocks become visible ``agent`` rows tagged
  ``thinking`` with the text prefixed ``[thinking] `` so a hit can never be mistaken for something said. Empty
  (omitted) thinking is skipped.
- ``tool_use`` becomes a hidden ``tool`` row (``name: <first 200 characters of the input>``, the input redacted
  before it is cut); ``tool_result`` becomes a hidden ``tool`` row. A call that reads a secret-bearing source (a ``.env``,
  a private key, ``env``, ``ps``, a search for passwords: see ``secret_sources``) has its result withheld whole, and its
  input reduced to the tool name and the file (or the program): never ``content``, ``new_string``, ``old_string`` or a
  heredoc body. Best effort, not a guarantee.
- User text becomes a visible ``user`` row, except harness input (system reminders, task notifications, slash-command
  wrappers, meta lines, compaction summaries, plugin prompts), which is hidden with speaker ``system``.
  A message from another agent (origin kind ``peer``) is a visible ``peer`` row.
- ``conv_id`` is the session id; the title is the session's custom title, else its latest AI title; the platform
  follows the entrypoint (``cli``, or ``desktop`` for the desktop app).

Point it at a ``.jsonl`` file, a project folder, or the whole ``~/.claude/projects`` folder.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Iterator

from ..redact import redact
from ..secret_sources import WITHHELD, secret_bearing, secret_call_summary
from ..timeutil import parse_iso
from . import safety
from .base import Importer, Options, Record, Unit

SYSTEM_PREFIXES = ("<system-reminder", "<task-notification", "<command-", "<local-command", "[Request interrupted")
PLATFORM = {"cli": "cli", "claude-desktop": "desktop"}
TOOL_INPUT_CHARS = 200


def _text_of(content) -> tuple[str, int]:
    """(text, image_count) from a string or a list of blocks; tool_result, thinking and tool_use are excluded."""
    if isinstance(content, str):
        return content, 0
    texts, images = [], 0
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text") or "")
        elif b.get("type") == "image":
            images += 1
    return "\n\n".join(t for t in texts if t.strip()), images


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts)


def _redact_deep(v, depth: int = 0):
    """Every string inside a tool input, redacted: a multi-line command meets the line patterns before JSON escapes it."""
    if isinstance(v, str):
        return redact(v)
    if depth > 6 or v is None or not isinstance(v, (dict, list)):
        return v
    if isinstance(v, list):
        return [_redact_deep(x, depth + 1) for x in v]
    return {k: _redact_deep(x, depth + 1) for k, x in v.items()}


def _tool_input_text(inp) -> str:
    """The tool input as one redacted line of text (not yet shortened: ``Record.cut`` does that after redaction)."""
    try:
        return redact(inp if isinstance(inp, str) else json.dumps(_redact_deep(inp), ensure_ascii=False))
    except (RecursionError, TypeError, ValueError):
        return "[unreadable input]"


def _peer_name(origin: dict) -> str:
    words = (origin.get("name") or "peer").strip().split() or ["peer"]
    return re.sub(r"[^a-z0-9_-]", "", words[0].lower()) or "peer"


def classify_user(d: dict, text: str) -> tuple[str, bool, set[str], str]:
    """(speaker, hidden, tags, context) for a user turn's text."""
    origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
    kind = origin.get("kind")
    s = text.lstrip()
    if d.get("isCompactSummary"):
        return "system", True, {"summary"}, "system"
    if kind == "plugin":
        return "system", True, {"plugin"}, "system"
    if kind == "peer":
        return "peer", False, {"peer:" + _peer_name(origin)}, "peer"
    if kind == "task-notification":
        return "system", True, {"system"}, "system"
    if s.startswith(SYSTEM_PREFIXES):
        return "system", True, {"system"}, "system"
    if d.get("isMeta"):
        return "system", True, {"system"}, "system"
    return "user", False, set(), "default"


def _session_files(path: Path, projects: list[str] | None) -> list[Path]:
    if path.is_file():
        return [path]
    files = sorted(path.glob("*.jsonl"))
    if not files:  # a projects root: one level of project folders
        for sub in sorted(p for p in path.iterdir() if p.is_dir()):
            if projects is not None and sub.name not in projects:
                continue
            files.extend(sorted(sub.glob("*.jsonl")))
    return files


def _looks_like_transcript(path: Path) -> bool:
    if safety.problem(path):
        return False
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for n, line in safety.iter_lines(fh):
                if n >= 40:
                    break
                try:
                    d = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(d, dict) and d.get("type") in ("user", "assistant") and "sessionId" in d \
                        and isinstance(d.get("message"), dict):
                    return True
    except OSError:
        return False
    return False


class ClaudeCode(Importer):
    name = "claude_code"
    description = "Claude Code session transcripts (~/.claude/projects/<project>/*.jsonl)"
    default_source = "claude-code"
    version = 3   # 3: inputs of secret-bearing calls reduced to the tool and the file; wider secret-source rules

    def detect(self, path: Path) -> bool:
        path = Path(path)
        if path.is_file():
            return path.suffix == ".jsonl" and _looks_like_transcript(path)
        if path.is_dir():
            files = _session_files(path, None)
            return bool(files) and _looks_like_transcript(files[0])
        return False

    def units(self, path: Path, opts: Options) -> Iterator[Unit]:
        path = Path(path)
        root = path.parent if path.is_file() else path
        self.skipped = collections.Counter()
        for f in _session_files(path, opts.projects):
            if self.accept(f, root):
                yield Unit(key=f.relative_to(root).as_posix(), files=[f], root=root)

    def unit_records(self, unit: Unit, opts: Options, stats: collections.Counter) -> Iterator[Record]:
        label = self.label(opts)
        path = unit.files[0]
        src = "claude-code/" + path.name
        custom: dict = {}
        ai: dict = {}
        tool_names: dict[str, str] = {}
        secret_calls: set[str] = set()   # ids of tool calls whose result is withheld
        out: list[Record] = []
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for ln, line in safety.iter_lines(fh, stats):
                try:
                    d = json.loads(line)
                except (ValueError, RecursionError):
                    stats["bad_lines"] += 1
                    continue
                if not isinstance(d, dict):
                    stats["bad_lines"] += 1
                    continue
                t = d.get("type")
                if t == "custom-title" and d.get("customTitle"):
                    custom[d.get("sessionId")] = d["customTitle"]
                elif t == "ai-title" and d.get("aiTitle"):
                    ai[d.get("sessionId")] = d["aiTitle"]
                if t not in ("user", "assistant"):
                    continue
                if d.get("isSidechain"):
                    stats["sidechain_skipped"] += 1
                    continue
                sid = d.get("sessionId") or path.stem
                uuid = d.get("uuid") or f"{sid}:{ln}"
                try:
                    ts = parse_iso(d["timestamp"])
                except (KeyError, ValueError, TypeError, AttributeError):
                    stats["no_timestamp"] += 1
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                content = msg.get("content")
                platform = PLATFORM.get(d.get("entrypoint"), "cli")
                base = dict(source=label, conv_id=sid, ts=ts, platform=platform, src=src)

                def rid(*parts: str) -> str:
                    return ":".join(("claude-code", sid, uuid) + parts)

                if t == "assistant":
                    model = msg.get("model")
                    synthetic = model == "<synthetic>"
                    model = None if synthetic else model
                    blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
                    text = "\n\n".join(
                        b.get("text") or "" for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip())
                    thought = "\n\n".join(
                        b.get("thinking") or "" for b in blocks
                        if isinstance(b, dict) and b.get("type") == "thinking" and (b.get("thinking") or "").strip())
                    if thought and not synthetic:
                        out.append(Record(id=rid("thinking"), speaker="agent", text="[thinking] " + thought.strip(),
                                          model=model, tags={"thinking"}, order=ln * 4, **base))
                        stats["thinking"] += 1
                    if text:
                        if synthetic:
                            out.append(Record(id=rid(), speaker="system", text=text, hidden=True, context="system",
                                              tags={"synthetic"}, order=ln * 4, **base))
                        else:
                            out.append(Record(id=rid(), speaker="agent", text=text, model=model, order=ln * 4, **base))
                            stats["agent_messages"] += 1
                    for k, b in enumerate(blocks):
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            name = b.get("name") or "?"
                            tid = b.get("id") or f"{k}"
                            tool_names[tid] = name
                            inp = b.get("input")
                            secret = secret_bearing(name, inp)
                            if secret:
                                secret_calls.add(tid)
                                stats["inputs_withheld"] += 1
                            head = f"{name}: "
                            shown_input = secret_call_summary(name, inp) if secret else _tool_input_text(inp)
                            out.append(Record(id=rid(tid), speaker="tool", text=head + shown_input,
                                              cut=len(head) + TOOL_INPUT_CHARS,
                                              model=model, hidden=True, tags={"tool-call"}, context="system",
                                              order=ln * 4 + 1 + min(k, 2), **base))
                    continue

                # user line
                if isinstance(content, list):
                    for k, b in enumerate(content):
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            tid = b.get("tool_use_id") or f"{k}"
                            name = tool_names.get(tid, "tool")
                            tags = {"tool-result"} | ({"error"} if b.get("is_error") else set())
                            shown = WITHHELD if tid in secret_calls else _result_text(b.get("content"))
                            if tid in secret_calls:
                                stats["results_withheld"] += 1
                            out.append(Record(id=rid(tid), speaker="tool",
                                              text=f"[result:{name}] {shown}",
                                              hidden=True, tags=tags, context="system",
                                              order=ln * 4 + 1 + min(k, 2), **base))
                text, images = _text_of(content)
                if not text.strip() and not images:
                    continue
                speaker, hidden, tags, context = classify_user(d, text)
                if images:
                    tags = tags | {"image"}
                    if not text.strip():
                        text = "[image]" if images == 1 else f"[{images} images]"
                out.append(Record(id=rid(), speaker=speaker, text=text, hidden=hidden, tags=tags, context=context,
                                  order=ln * 4, **base))
        for r in out:
            r.conv_title = custom.get(r.conv_id) or ai.get(r.conv_id)
        stats["sessions"] += len({r.conv_id for r in out})
        yield from out
