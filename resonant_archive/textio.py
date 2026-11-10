"""Reading text files the way Windows writes them.

Notepad and PowerShell (``Out-File``, ``>``, ``Set-Content -Encoding utf8`` on 5.1) put a byte-order mark at the start
of a file, and PowerShell 5.1 writes UTF-16 by default. Read as plain UTF-8 the mark becomes a U+FEFF character in front of the
first line: a Markdown file's ``---`` front matter is no longer front matter, a ``.env`` loses its first key, and a
UTF-16 file turns into NUL-separated noise. Line endings are handled by Python's universal newlines (CRLF reads as LF).
"""

from __future__ import annotations

import codecs
import os


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without a BOM) or UTF-16 with a BOM, as text. Bytes that are not valid become U+FFFD."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")      # the utf-16 codec reads the BOM to pick the byte order
    return data.decode("utf-8", errors="replace")


def read_text(path: str | os.PathLike) -> str:
    """The text of a file (see ``decode_text``), with CRLF and a lone CR read as LF."""
    with open(path, "rb") as fh:
        text = decode_text(fh.read())
    return text.replace("\r\n", "\n").replace("\r", "\n")
