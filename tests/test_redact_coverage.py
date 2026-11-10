"""Redaction coverage: every credential shape goes, ordinary text and anchor ids stay, redaction is linear."""
from __future__ import annotations

import time

import pytest

from resonant_archive import anchors
from resonant_archive.redact import Redactor, redact, scrub_private_keys

from .redaction_cases import SECRETS, SURVIVE


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_secret_shape_is_redacted(name):
    text, gone = SECRETS[name]
    out = redact(f"before {text} after")
    assert gone not in out, out
    assert "[redacted" in out
    assert out.startswith("before") and out.endswith("after") or "-----BEGIN" in text  # context kept (a key with no end eats the rest)
    # a second pass changes nothing
    assert redact(out) == out
    r = Redactor()
    once = r(text)
    first = dict(r.counts)
    assert r(once) == once and dict(r.counts) == first


@pytest.mark.parametrize("text", SURVIVE)
def test_ordinary_text_survives(text):
    assert redact(text) == text


def test_commit_ids_and_uuids_are_still_anchored():
    sha = "3f786850e387550fdab836ed7e6dc881de23001b"
    text = f"we pushed commit {sha} and the run 550e8400-e29b-41d4-a716-446655440000 passed"
    out = redact(text)
    assert out == text
    kinds = {(t, k) for t, k in anchors.extract(out)}
    assert (sha, "sha") in kinds


def test_private_key_scan_is_linear():
    big = "-----BEGIN PRIVATE KEY-----\n" * 20_000
    t = time.perf_counter()
    out = redact(big)
    assert time.perf_counter() - t < 1.0
    assert out == "[redacted:private-key]"
    pairs = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\nkeep me\n" * 5000
    t = time.perf_counter()
    out = redact(pairs)
    assert time.perf_counter() - t < 1.0
    assert out.count("[redacted:private-key]") == 5000 and "abc" not in out and out.count("keep me") == 5000


HOSTILE = {
    "newlines": "\n",
    "repeated --user=x": "--user=x",
    "unfinished cli-credentials": "-u " + "a" * 40,
    "blank env lines": " \t\n",
    "repeated unfinished env key": "export A_KEY=\n",
    # Windows rules (2.0.2): each of these is a prefix that the rule can start on, over and over, and never finish
    "repeated set": "set A_KEY=",
    "repeated setx": 'setx A_KEY "',
    "repeated $env": "$env:A_KEY =",
    "repeated -Password": "-Password ",
    "repeated net use": "net use ",
    "repeated net user": "net user a ",
    "repeated sqlcmd": "sqlcmd -x ",
    "repeated psexec": "psexec -x ",
    "repeated ConvertTo": 'ConvertTo-SecureString "',
    "repeated env table": "A_KEY  \n",
    "repeated env list": "Name : A_KEY\n",
    "repeated reg row": "    A_KEY  REG_SZ \n",
    "repeated reg add": "reg add /v a_key ",
    "long upper run": "A" * 1000 + "  ",
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_redaction_is_linear_on_hostile_input(name):
    frag = HOSTILE[name]
    text = (frag * (500_000 // len(frag) + 1))[:500_000]
    t = time.perf_counter()
    redact(text)
    assert time.perf_counter() - t < 1.0


def test_scrub_private_keys_cases():
    assert scrub_private_keys("nothing here") == ("nothing here", 0)
    s, n = scrub_private_keys("a -----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY----- b")
    assert (s, n) == ("a [redacted:private-key] b", 1)
    s, n = scrub_private_keys("-----BEGIN PRIVATE KEY-----\nx -----END PRIVATE KEY----- mid -----BEGIN PRIVATE KEY-----\ny")
    assert s == "[redacted:private-key] mid [redacted:private-key]" and n == 2


def test_other_inputs_stay_fast():
    for s in ["A" * 500_000, 'password="' * 50_000, " " * 500_000 + "x", "A_KEY=x\n" * 60_000, "mysql " * 80_000,
              "mysql -p" * 60_000, "http://a:" * 50_000, "authorization:" * 30_000, "secret " * 70_000]:
        t = time.perf_counter()
        redact(s)
        assert time.perf_counter() - t < 5.0
