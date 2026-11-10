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


# ---- a failed restriction must be loud, never silent ----

@pytest.fixture
def restriction_fails(monkeypatch):
    """icacls (Windows) or chmod (elsewhere) fails."""
    if sys.platform == "win32":
        real = subprocess.run

        def run(args, *a, **kw):
            if args and args[0] == "icacls":
                return subprocess.CompletedProcess(args, 5, "", "Access is denied.")
            return real(args, *a, **kw)

        monkeypatch.setattr(fsutil.subprocess, "run", run)
    else:
        class FailingChmod:
            """fsutil's view of os, with chmod refused. Patching os.chmod itself would break the test's own setup
            (shutil.copy calls it), so only the module under test sees the failure."""

            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def chmod(*a, **kw):
                raise PermissionError("no")

        monkeypatch.setattr(fsutil, "os", FailingChmod())
    fsutil.take_unprotected()
    yield
    fsutil.take_unprotected()


def test_make_private_says_false_when_the_restriction_fails(tmp_path, restriction_fails):
    f = tmp_path / "x"
    f.write_text("x")
    assert fsutil.make_private(f) is False


def test_write_private_text_warns_on_stderr_and_reports_the_file(tmp_path, restriction_fails, capsys):
    p = tmp_path / "secret.env"
    assert fsutil.write_private_text(p, "A=1\n") is False
    assert p.read_text() == "A=1\n"                                     # the file is written, but nobody is told it is safe
    err = capsys.readouterr().err
    assert str(p) in err and "readable by other users" in err
    assert fsutil.take_unprotected() == [str(p)]


def test_init_docker_says_in_its_result_that_the_env_file_is_not_private(tmp_path, restriction_fails):
    import shutil as sh
    from pathlib import Path

    from typer.testing import CliRunner

    from resonant_archive import cli

    sh.copyfile(Path(__file__).resolve().parent.parent / "docker-compose.yml", tmp_path / "docker-compose.yml")
    res = CliRunner().invoke(cli.app, ["init", "--docker", "--project-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert ".env" in res.output and "could NOT be restricted" in res.output
    assert "readable by other users" in res.output                     # the stderr warning (CliRunner mixes the streams)


def test_a_failed_or_empty_whoami_is_a_failed_restriction(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "WINDOWS", True)
    monkeypatch.setattr(fsutil, "_sid", None)
    f = tmp_path / "x"
    f.write_text("x")
    for out in (subprocess.CompletedProcess([], 1, "", "boom"), subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, '"h\\u","not-a-sid"\n', "")):
        monkeypatch.setattr(fsutil.subprocess, "run", lambda *a, _o=out, **k: _o)
        monkeypatch.setattr(fsutil, "_sid", None)
        assert fsutil.make_private(f) is False
