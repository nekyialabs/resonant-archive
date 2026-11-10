"""Output must survive a pipe whatever the platform's default encoding is (cp1252 on Windows)."""
from __future__ import annotations

import os
import subprocess
import sys

from resonant_archive import fsutil


def test_a_path_with_characters_outside_the_ansi_code_page_prints_through_a_pipe(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}   # no PYTHONIOENCODING / UTF8 mode
    name = "no-such-日本-\U0001f600"
    out = subprocess.run([sys.executable, "-m", "resonant_archive.cli", "migrate-v01", "--from", str(tmp_path / name)],
                         capture_output=True, env=env, creationflags=fsutil.NO_WINDOW)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[-400:]
    assert name.encode("utf-8") in out.stdout
