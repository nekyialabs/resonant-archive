"""resonant-archive chunker — frontmatter-aware text chunking.

Walks a directory of text files, parses frontmatter (markdown only),
and splits bodies into chunks with tiered metadata.

Metadata tiers:
  - Always present: chunk_id, source_file, text, chunk_index, char_offset
  - Present if extractable: title, timestamp, timestamp_source, tags
  - Present for conversation data (Nexus-style frontmatter only):
    conversation_id, provider

Copyright 2025-2026 Nekyia Labs
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import frontmatter

from . import textio

# Default chunking parameters sized for all-MiniLM-L6-v2 (512 token limit).
# 1500 chars leaves comfortable headroom under the tokenizer cap.
DEFAULT_MAX_CHARS = 1500
DEFAULT_MIN_CHARS = 200
DEFAULT_OVERLAP_CHARS = 150
DEFAULT_EXTENSIONS: tuple[str, ...] = (".md", ".markdown", ".mdx", ".txt")

_HEADER_RE = re.compile(r"^\s*#{1,6}\s")
_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")
_MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdx"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """A single chunk of text with tiered metadata."""

    chunk_id: str
    source_file: str
    text: str
    chunk_index: int
    char_offset: int
    # Optional metadata
    title: str | None = None
    timestamp: str | None = None
    timestamp_source: str | None = None  # "frontmatter" or "filesystem"
    tags: list[str] | None = None
    conversation_id: str | None = None
    provider: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a dict with None fields omitted (for JSONL serialization)."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ParsedFile:
    """Result of parsing a single text file."""

    source_file: str
    body: str
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Splitting strategies
# ---------------------------------------------------------------------------


def split_body(
    body: str,
    *,
    strategy: str = "paragraph",
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[tuple[str, int]]:
    """Split a body of text into (text, char_offset) pairs.

    Line endings are normalized to LF before processing.
    Empty or whitespace-only bodies return an empty list.
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized or not normalized.strip():
        return []
    if strategy == "window":
        return _split_window(normalized, max_chars, overlap_chars)
    return _split_paragraph(normalized, max_chars, min_chars, overlap_chars)


def _split_paragraph(
    body: str, max_chars: int, min_chars: int, overlap_chars: int
) -> list[tuple[str, int]]:
    """Paragraph strategy: header-aware greedy packing, overflow to windows."""
    paragraphs = _find_paragraphs(body)
    if not paragraphs:
        return []

    chunks: list[tuple[str, int]] = []
    current_text: str | None = None
    current_offset: int = 0

    for para_text, para_offset in paragraphs:
        # Oversized paragraph: flush current, split this one into windows.
        if len(para_text) > max_chars:
            if current_text is not None:
                chunks.append((current_text, current_offset))
                current_text = None
            for window_text, window_offset in _split_window(
                para_text, max_chars, overlap_chars
            ):
                chunks.append((window_text, para_offset + window_offset))
            continue

        # Header breaks any running pack.
        if _HEADER_RE.match(para_text):
            if current_text is not None:
                chunks.append((current_text, current_offset))
            current_text = para_text
            current_offset = para_offset
            continue

        if current_text is None:
            current_text = para_text
            current_offset = para_offset
            continue

        # Never merge a fresh paragraph into a header-led chunk.
        if _HEADER_RE.match(current_text):
            chunks.append((current_text, current_offset))
            current_text = para_text
            current_offset = para_offset
            continue

        candidate = current_text + "\n\n" + para_text
        if len(candidate) <= max_chars:
            current_text = candidate
        else:
            chunks.append((current_text, current_offset))
            current_text = para_text
            current_offset = para_offset

    if current_text is not None:
        chunks.append((current_text, current_offset))

    # Best-effort tail merge if the last chunk is below min_chars.
    if len(chunks) >= 2:
        last_text, _ = chunks[-1]
        prev_text, prev_offset = chunks[-2]
        if (
            len(last_text) < min_chars
            and not _HEADER_RE.match(last_text)
            and not _HEADER_RE.match(prev_text)
            and len(prev_text) + len(last_text) + 2 <= max_chars
        ):
            merged = prev_text + "\n\n" + last_text
            chunks[-2] = (merged, prev_offset)
            chunks.pop()

    return chunks


def _split_window(
    body: str, max_chars: int, overlap_chars: int
) -> list[tuple[str, int]]:
    """Fixed-size windows with overlap, preferring whitespace boundaries."""
    if len(body) <= max_chars:
        return [(body, 0)]

    chunks: list[tuple[str, int]] = []
    step = max(1, max_chars - overlap_chars)
    start = 0

    while start < len(body):
        end = min(start + max_chars, len(body))

        if end < len(body):
            min_end = start + step
            search_from = max(start + int(max_chars * 0.7), min_end)
            if search_from < end:
                segment = body[search_from:end]
                last_ws = max(segment.rfind(" "), segment.rfind("\n"))
                if last_ws > 0:
                    end = search_from + last_ws

        chunks.append((body[start:end], start))

        if end >= len(body):
            break
        next_start = end - overlap_chars
        start = next_start if next_start > start else start + 1

    return chunks


def _find_paragraphs(body: str) -> list[tuple[str, int]]:
    """Split body on blank lines and track offsets."""
    paragraphs: list[tuple[str, int]] = []
    last_end = 0
    for match in _PARAGRAPH_BREAK_RE.finditer(body):
        segment = body[last_end : match.start()]
        if segment.strip():
            paragraphs.append((segment, last_end))
        last_end = match.end()
    if last_end < len(body):
        segment = body[last_end:]
        if segment.strip():
            paragraphs.append((segment, last_end))
    return paragraphs


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


def parse_file(
    file_path: Path, relative_path: str | None = None
) -> ParsedFile:
    """Read and parse a single text file, extracting frontmatter if present."""
    content = textio.read_text(file_path)  # a BOM (Notepad) or UTF-16 (PowerShell) is decoded, CRLF read as LF

    body = content
    fm_data: dict[str, Any] = {}
    if file_path.suffix.lower() in _MARKDOWN_EXTENSIONS:
        try:
            post = frontmatter.loads(content)
            body = post.content
            fm_data = dict(post.metadata)
        except Exception:
            # Malformed frontmatter: treat whole file as body.
            body = content

    metadata = _extract_metadata(file_path, fm_data)
    source = relative_path if relative_path is not None else str(file_path)
    return ParsedFile(source_file=source, body=body, metadata=metadata)


def _extract_metadata(file_path: Path, fm: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}

    # Title: frontmatter > filename without extension
    title = fm.get("title")
    if isinstance(title, str) and title:
        meta["title"] = title
    else:
        meta["title"] = file_path.stem

    # Timestamp: frontmatter create_time/date/timestamp/created > mtime
    ts = _first_timestamp(fm, ("create_time", "date", "timestamp", "created"))
    if ts:
        meta["timestamp"] = ts
        meta["timestamp_source"] = "frontmatter"
    else:
        try:
            mtime = file_path.stat().st_mtime
            meta["timestamp"] = datetime.fromtimestamp(
                mtime, tz=timezone.utc
            ).isoformat()
            meta["timestamp_source"] = "filesystem"
        except OSError:
            pass

    # Tags
    tags = fm.get("tags")
    if isinstance(tags, list):
        filtered = [t for t in tags if isinstance(t, str)]
        if filtered:
            meta["tags"] = filtered
    elif isinstance(tags, str):
        meta["tags"] = [tags]

    # Conversation metadata (Nexus-style)
    if isinstance(fm.get("conversation_id"), str):
        meta["conversation_id"] = fm["conversation_id"]
    if isinstance(fm.get("provider"), str):
        meta["provider"] = fm["provider"]

    return meta


def _first_timestamp(
    obj: dict[str, Any], keys: Iterable[str]
) -> str | None:
    for k in keys:
        iso = _to_iso_string(obj.get(k))
        if iso:
            return iso
    return None


def _to_iso_string(raw: Any) -> str | None:
    """Coerce a frontmatter value into an ISO-8601 UTC timestamp string."""
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        return raw.isoformat()
    if isinstance(raw, date):
        # A bare YAML date (``date: 2026-02-01``) loads as datetime.date, not datetime.
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc).isoformat()
    if isinstance(raw, bool):
        # bool is a subclass of int — exclude it explicitly.
        return None
    if isinstance(raw, (int, float)) and raw > 0:
        ms = float(raw)
        if ms < 1e12:
            ms *= 1000
        try:
            return datetime.fromtimestamp(
                ms / 1000, tz=timezone.utc
            ).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw, str) and raw:
        # Try numeric epoch first.
        try:
            as_num = float(raw)
            if as_num > 0:
                ms = as_num if as_num >= 1e12 else as_num * 1000
                return datetime.fromtimestamp(
                    ms / 1000, tz=timezone.utc
                ).isoformat()
        except ValueError:
            pass
        # Try ISO-8601.
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------


def walk_text_files(
    directory: Path, extensions: Iterable[str]
) -> list[Path]:
    """Walk a directory recursively, returning matching text files.

    Hidden files and hidden directories (names starting with ``.``) are
    skipped. Results are sorted for reproducible ordering.
    """
    ext_set = {e.lower() for e in extensions}
    out: list[Path] = []
    for root, dirs, files in os.walk(directory):
        # Prune hidden directories in-place so os.walk doesn't descend.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in ext_set:
                out.append(Path(root) / name)
    return sorted(out)


def chunk_file(
    file_path: Path,
    relative_path: str,
    *,
    strategy: str = "paragraph",
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Chunk a single file. Returns an empty list if the body is empty."""
    parsed = parse_file(file_path, relative_path)
    raw_chunks = split_body(
        parsed.body,
        strategy=strategy,
        max_chars=max_chars,
        min_chars=min_chars,
        overlap_chars=overlap_chars,
    )
    return _raw_to_chunks(raw_chunks, parsed.source_file, parsed.metadata)


def chunk_directory(
    directory: Path,
    *,
    strategy: str = "paragraph",
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
) -> list[Chunk]:
    """Chunk every text file in a directory. Returns a flat list of chunks."""
    files = walk_text_files(directory, extensions)
    all_chunks: list[Chunk] = []
    for file_path in files:
        rel = file_path.relative_to(directory).as_posix()
        parsed = parse_file(file_path, rel)
        raw_chunks = split_body(
            parsed.body,
            strategy=strategy,
            max_chars=max_chars,
            min_chars=min_chars,
            overlap_chars=overlap_chars,
        )
        all_chunks.extend(
            _raw_to_chunks(raw_chunks, parsed.source_file, parsed.metadata)
        )
    return all_chunks


def write_chunks_jsonl(chunks: list[Chunk], output_path: Path) -> None:
    """Serialize chunks to a JSONL file, creating parent dirs if needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(c.to_dict(), ensure_ascii=False) for c in chunks]
    content = "\n".join(lines) + ("\n" if lines else "")
    output_path.write_text(content, encoding="utf-8")


def _raw_to_chunks(
    raw_chunks: list[tuple[str, int]],
    source_file: str,
    metadata: dict[str, Any],
) -> list[Chunk]:
    """Attach tiered metadata to (text, offset) pairs."""
    out: list[Chunk] = []
    for i, (text, offset) in enumerate(raw_chunks):
        chunk = Chunk(
            chunk_id=_make_chunk_id(source_file, i),
            source_file=source_file,
            text=text,
            chunk_index=i,
            char_offset=offset,
        )
        if "title" in metadata:
            chunk.title = metadata["title"]
        if "timestamp" in metadata:
            chunk.timestamp = metadata["timestamp"]
        if "timestamp_source" in metadata:
            chunk.timestamp_source = metadata["timestamp_source"]
        if "tags" in metadata:
            chunk.tags = metadata["tags"]
        if "conversation_id" in metadata:
            chunk.conversation_id = metadata["conversation_id"]
        if "provider" in metadata:
            chunk.provider = metadata["provider"]
        out.append(chunk)
    return out


def _make_chunk_id(source: str, chunk_index: int) -> str:
    """Return a stable 16-char hex chunk id derived from source + index."""
    payload = f"{source}:{chunk_index}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
