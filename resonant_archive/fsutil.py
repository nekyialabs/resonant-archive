"""File helpers that behave the same on POSIX and Windows: owner-only files, atomic replace, console-less subprocesses.

POSIX permission bits do not exist on Windows (``os.chmod`` only toggles the read-only flag, ``mkdir(mode=)`` is
ignored), so "mode 0600" would silently mean "whatever the folder allows". On Windows the same promise is kept with
an access-control list: ``icacls`` drops the inherited entries and grants the current user alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = sys.platform == "win32"

#: ``creationflags`` for a subprocess that must not open a console window (0 off Windows).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if WINDOWS else 0

_sid: str | None = None


def _user_sid() -> str | None:
    """The current user's SID (``S-1-5-21-...``), from ``whoami /user``, or None when it cannot be read."""
    global _sid
    if _sid is None:
        try:
            out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15,
                                 creationflags=NO_WINDOW).stdout
            parts = [p.strip().strip('"') for p in out.strip().split(",")]
            _sid = parts[1] if len(parts) == 2 and parts[1].startswith("S-1-") else ""
        except (OSError, subprocess.SubprocessError):
            _sid = ""
    return _sid or None


def make_private(path: str | os.PathLike, *, directory: bool = False) -> bool:
    """Make ``path`` readable and writable by its owner alone. Returns True when that was applied.

    POSIX: mode 0600 (0700 for a directory). Windows: inheritance removed and only the current user granted full
    control. Best effort: on a filesystem with no ACLs (a FAT drive) it returns False instead of raising.
    """
    if not WINDOWS:
        try:
            os.chmod(path, 0o700 if directory else 0o600)
            return True
        except OSError:
            return False
    sid = _user_sid()
    if sid is None:
        return False
    grant = f"*{sid}:(OI)(CI)F" if directory else f"*{sid}:F"
    try:
        done = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                              capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def replace(src: str | os.PathLike, dst: str | os.PathLike, *, attempts: int = 8) -> None:
    """``os.replace``, retried on Windows for a moment when something else (a virus scanner, ``docker compose``
    reading the file) has the destination open and Windows refuses the rename with PermissionError."""
    for n in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not WINDOWS or n == attempts - 1:
                raise
            time.sleep(0.05 * (n + 1))


def write_private_text(path: str | os.PathLike, text: str) -> None:
    """Write ``text`` (UTF-8, LF line endings on every platform) to ``path`` through a temporary file, owner-only
    from before the first byte is written, then rename it into place. The caller creates the folder."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        make_private(tmp)  # Windows ignores the mode above: restrict the empty file before the secret goes in
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    replace(tmp, path)
    make_private(path)
