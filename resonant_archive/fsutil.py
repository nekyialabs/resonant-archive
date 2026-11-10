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
    """The current user's SID (``S-1-5-21-...``), from ``whoami /user``. None when ``whoami`` fails, prints nothing or
    prints something that is not a SID: the caller then treats the file as NOT restricted. Only a success is cached."""
    global _sid
    if _sid:
        return _sid
    try:
        done = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    parts = [p.strip().strip('"') for p in done.stdout.strip().split(",")]
    if len(parts) == 2 and parts[1].startswith("S-1-") and len(parts[1]) > 8:
        _sid = parts[1]
    return _sid


def make_private(path: str | os.PathLike, *, directory: bool = False) -> bool:
    """Try to make ``path`` unreadable to other accounts. Returns True only when that was applied, False when it was not
    (it never raises, and a False must not be ignored: see ``ensure_private``).

    POSIX: mode 0600 (0700 for a directory). Windows: inheritance removed and only the current user granted access, so
    no other ordinary account can read it (an administrator can still take ownership; that is Windows). False when
    ``whoami`` or ``icacls`` fails, or the filesystem has no ACLs or modes (a FAT drive).
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


_unprotected: list[str] = []


def ensure_private(path: str | os.PathLike, *, directory: bool = False) -> bool:
    """``make_private`` that cannot fail silently: when it fails, a warning naming the file goes to stderr at once, and the
    path is remembered for ``take_unprotected`` so the command's own result can say so as well."""
    if make_private(path, directory=directory):
        return True
    _unprotected.append(str(path))
    print(f"warning: could not restrict {path} to your account: it may be readable by other users of this machine. "
          "It holds a database password: tighten its permissions yourself, or move it.", file=sys.stderr)
    return False


def take_unprotected() -> list[str]:
    """The paths ``ensure_private`` failed on since the last call (and forget them)."""
    out = list(dict.fromkeys(_unprotected))
    _unprotected.clear()
    return out


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


def write_private_text(path: str | os.PathLike, text: str) -> bool:
    """Write ``text`` (UTF-8, LF line endings on every platform) to ``path`` through a temporary file, owner-only
    from before the first byte is written, then rename it into place. The caller creates the folder. Returns False (after
    a warning, see ``ensure_private``) when the file could not be restricted."""
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
    return ensure_private(path)
