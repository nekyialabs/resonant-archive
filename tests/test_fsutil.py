"""The helpers that make "private file", "atomic replace" and "no console window" mean the same on every platform."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from resonant_archive import config, fsutil

from .permcheck import is_private


def test_write_private_text_is_private_utf8_with_lf_endings(tmp_path):
    p = tmp_path / "secret.env"
    fsutil.write_private_text(p, "A=1\nB=é€\n")
    assert is_private(p)
    assert p.read_bytes() == "A=1\nB=é€\n".encode("utf-8")      # no \r\n on Windows, no cp1252
    assert not (tmp_path / "secret.env.tmp").exists()
    fsutil.write_private_text(p, "A=2\n")                       # replaces in place
    assert p.read_text(encoding="utf-8") == "A=2\n" and is_private(p)


def test_make_private_on_a_folder(tmp_path):
    d = tmp_path / "docker"
    d.mkdir()
    assert fsutil.make_private(d, directory=True)
    assert is_private(d, directory=True)
    (d / "f.txt").write_text("x")                               # the owner can still use it
    assert (d / "f.txt").read_text() == "x"


def test_make_private_on_a_missing_path_returns_false_and_does_not_raise(tmp_path):
    assert fsutil.make_private(tmp_path / "nope") is False


def test_the_config_file_is_written_private_and_with_lf(tmp_path):
    config.write_dsn("postgresql://u:pw@h/d")
    cfg = config.config_file()
    assert is_private(cfg) and b"\r" not in cfg.read_bytes()
    assert config.resolve_dsn() == "postgresql://u:pw@h/d"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename over a file another process holds open")
def test_replace_waits_for_a_destination_that_is_briefly_open(tmp_path):
    import threading
    import time

    dst = tmp_path / "dst.txt"
    src = tmp_path / "src.txt"
    dst.write_text("old")
    src.write_text("new")
    fd = os.open(dst, os.O_RDONLY)                              # open without FILE_SHARE_DELETE: a rename over it fails
    threading.Timer(0.15, lambda: os.close(fd)).start()
    t0 = time.time()
    fsutil.replace(src, dst)
    assert dst.read_text() == "new" and time.time() - t0 < 5


def test_replace_raises_when_the_problem_is_not_transient(tmp_path):
    with pytest.raises(FileNotFoundError):
        fsutil.replace(tmp_path / "missing", tmp_path / "dst")


def test_no_window_flag_is_zero_off_windows():
    assert (fsutil.NO_WINDOW != 0) == (sys.platform == "win32")
    out = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True, creationflags=fsutil.NO_WINDOW)
    assert out.stdout.strip() == "1"
