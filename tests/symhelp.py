"""Symlinks in tests. Windows only lets an account create them with Developer Mode on or from an elevated shell, so a
test that needs one skips there instead of failing (CI runners are elevated and run them)."""
from __future__ import annotations

import os
import sys

import pytest


def symlink_or_skip(target, link, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        if sys.platform == "win32":
            pytest.skip(f"cannot create a symlink here ({exc.__class__.__name__}: Windows needs Developer Mode or an "
                        "elevated shell)")
        raise
