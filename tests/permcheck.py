"""Is a file or folder private to its owner? Mode bits on POSIX; the access-control list on Windows (where the mode
bits Python reports are meaningless: a plain file says 0o666 whatever its ACL)."""
from __future__ import annotations

import subprocess
import sys

from resonant_archive import fsutil


def is_private(path, directory: bool = False) -> bool:
    if sys.platform != "win32":
        return (path.stat().st_mode & 0o777) == (0o700 if directory else 0o600)
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, creationflags=fsutil.NO_WINDOW).stdout
    # Python 3.13+ makes mkdir(mode=0o700) an ACL of the owner, SYSTEM and Administrators, so those may appear next to the
    # user. What must not appear: an inherited entry, or a group any other account belongs to.
    wide = ("Everyone", "BUILTIN" + chr(92) + "Users", "Authenticated Users", "INTERACTIVE", "NETWORK")
    return out.count(":(") >= 1 and "(I)" not in out and not any(w in out for w in wide)
