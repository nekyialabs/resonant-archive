"""The private-strip lint: public defaults, the private terms file, word boundaries, the allowlist, exit codes.

The repository is public, so nothing here names a real person, host or path: the terms are invented."""
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
spec = importlib.util.spec_from_file_location("private_strip_lint", SCRIPTS / "private_strip_lint.py")
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)

NAME = "zorblax"           # an invented private name
HOST = "internal-wiki"     # an invented private host
PORT = "4242"
CONFIG = f'words = ["{NAME}", "{PORT}"]\nsubstrings = ["{HOST}"]\nexclude = ["skipme/*"]\n'


@pytest.fixture(autouse=True)
def _no_ambient_terms(monkeypatch):
    monkeypatch.delenv(lint.ENV_TERMS, raising=False)
    # Under CI=true the lint prints only file:line; the tests read the local format unless they set CI themselves.
    monkeypatch.delenv("CI", raising=False)


def _run(tmp_path, files, allow="", config=CONFIG, extra=()):
    root = tmp_path / "tree"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / "cfg.toml").write_text(config)
    (tmp_path / "allow.txt").write_text(allow)
    return lint.main(["--root", str(root), "--config", str(tmp_path / "cfg.toml"),
                      "--allowlist", str(tmp_path / "allow.txt"), *extra])


def test_clean_tree_passes(tmp_path, capsys):
    assert _run(tmp_path, {"a.md": "primary key, summary, zorblaxless-free\n"}) == 0
    assert "clean" in capsys.readouterr().out


def test_word_hits_respect_boundaries(tmp_path, capsys):
    rc = _run(tmp_path, {"a.md": f"thanks {NAME.title()}!\nprimary\n{NAME}_x\n"})
    out = capsys.readouterr().out
    assert rc == 1 and "a.md:1:" in out and "a.md:2:" not in out and "a.md:3:" not in out


def test_substring_and_port_hits(tmp_path, capsys):
    rc = _run(tmp_path, {"a.md": f"see https://x.{HOST}.example\nport {PORT}\nport {PORT}0\n"})
    out = capsys.readouterr().out
    assert rc == 1 and "a.md:1:" in out and "a.md:2:" in out and "a.md:3:" not in out


def test_exclude_globs_are_not_scanned(tmp_path):
    assert _run(tmp_path, {"skipme/a.md": NAME}) == 0


def test_allowlist_needs_path_term_and_line_to_match(tmp_path):
    files = {"docs/a.md": f"a {NAME} here\nanother {NAME} there\n"}
    assert _run(tmp_path, files, f"docs/*.md | {NAME} | another | false positive\n") == 1   # line 1 still hits
    assert _run(tmp_path, files, f"docs/*.md | {NAME} | * | whole file ok\n") == 0
    assert _run(tmp_path, files, f"other/*.md | {NAME} | * | wrong path\n") == 1


def test_the_shipped_config_and_allowlist_parse():
    rules, exclude = lint.load_config(SCRIPTS / "private_strip_lint.toml")
    assert rules and exclude
    lint.load_allowlist(SCRIPTS / "private_strip_allowlist.txt")


# ---------------------------------------------------------------- the public defaults carry no private names

def test_shipped_defaults_name_nothing_private():
    cfg = lint.tomllib.loads((SCRIPTS / "private_strip_lint.toml").read_text())
    assert cfg["words"] == [] and cfg["substrings"] == []                  # no denylist in the public file
    # the only paths the public rules name are the placeholders they let through
    assert {p["name"] for p in cfg["patterns"]} >= {"home path", "private key block", "api key", "url with a password"}


def test_the_repository_passes_its_own_public_defaults(capsys):
    assert lint.main([]) == 0
    assert "public defaults only" in capsys.readouterr().err                # and says that it did not check private terms


# ---------------------------------------------------------------- the shapes

DEFAULTS = SCRIPTS / "private_strip_lint.toml"


def _defaults(tmp_path, files):
    root = tmp_path / "tree"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return lint.main(["--root", str(root), "--config", str(DEFAULTS), "--allowlist", str(tmp_path / "none.txt")])


@pytest.mark.parametrize("line", [
    "cd /home/jdoe/projects/app/",
    "path = /Users/jdoe/Documents/x",
    r"C:\Users\jdoe\Documents\x",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "key AKIA" + "ABCDEFGHIJKLMNOP",
    "token ghp_" + "a1" * 18,
    "sk-ant-" + "Zq8" * 10,
    "hook https://hooks.slack.com/services/T00000000/B00000000/" + "X" * 24,
    "dsn postgres://app:S3cretValue99@db.internal.example/app",
    "Authorization: Bearer " + "abcdef0123456789" * 2,
])
def test_default_shapes_are_caught(tmp_path, line):
    assert _defaults(tmp_path, {"docs/a.md": line + "\n"}) == 1


@pytest.mark.parametrize("line", [
    "cd /home/user/projects/app/", "cd /home/you/x/", "see /Users/<name>/notes/", r"C:\Users\user\x\y",
    "postgresql://owner:password@host:5432/db", "postgres://user:pass@localhost/db", "redis://:${REDIS_PASSWORD}@h",
    "https://example.test/docs/mcp/overview", "an ordinary line about sk-short and ghp_short and AKIA123",
])
def test_placeholders_and_ordinary_text_pass(tmp_path, line):
    assert _defaults(tmp_path, {"docs/a.md": line + "\n"}) == 0


def test_shape_rules_skip_the_redaction_fixtures_but_private_terms_do_not(tmp_path, capsys):
    key = "-----BEGIN RSA PRIVATE KEY-----\n"
    terms = tmp_path / "terms.txt"
    terms.write_text(f"word:{NAME}\n")
    files = {"tests/redaction_cases.py": key + f"# {NAME}\n"}
    rc = _run(tmp_path, files, config=DEFAULTS.read_text(), extra=["--terms-file", str(terms)])
    out = capsys.readouterr().out
    assert rc == 1 and f"tests/redaction_cases.py:2: word:{NAME}" not in out and "redaction_cases.py:2" in out
    assert "PRIVATE KEY" not in out and "private key block" not in out


# ---------------------------------------------------------------- the private terms file

def test_private_terms_file_via_flag_and_env(tmp_path, monkeypatch, capsys):
    terms = tmp_path / "terms.txt"
    terms.write_text(f"# mine\nword:{NAME}\nsub:{HOST}\nre:\\b{PORT}\\d\\b\n/home/jdoe\n\n")
    files = {"a.md": f"hello {NAME}\nhost {HOST}.example\nport {PORT}1\ncd /home/jdoe/x\nfine line\n"}
    assert _run(tmp_path, files, config="") == 0                                   # no terms: only the defaults (none here)
    assert _run(tmp_path, files, config="", extra=["--terms-file", str(terms)]) == 1
    out = capsys.readouterr().out
    assert all(f"a.md:{n}:" in out for n in (1, 2, 3, 4)) and "a.md:5:" not in out
    monkeypatch.setenv(lint.ENV_TERMS, str(terms))
    assert _run(tmp_path, files, config="") == 1                                   # the env var does the same


def test_private_terms_toml_format(tmp_path):
    terms = tmp_path / "terms.toml"
    terms.write_text(f'words = ["{NAME}"]\n[[patterns]]\nname = "port"\nregex = "\\\\b{PORT}\\\\b"\n')
    assert _run(tmp_path, {"a.md": f"{NAME}\n"}, config="", extra=["--terms-file", str(terms)]) == 1
    assert _run(tmp_path, {"a.md": f"port {PORT}\n"}, config="", extra=["--terms-file", str(terms)]) == 1
    assert _run(tmp_path, {"a.md": "nothing\n"}, config="", extra=["--terms-file", str(terms)]) == 0


def test_an_unreadable_terms_file_is_an_error_not_a_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(lint.ENV_TERMS, str(tmp_path / "missing.txt"))
    assert _run(tmp_path, {"a.md": "x\n"}, config="") == 2
    assert "cannot read the private terms file" in capsys.readouterr().err


def test_a_terms_file_kept_inside_the_tree_does_not_flag_itself(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "my-terms.txt").write_text(f"word:{NAME}\n")
    (tmp_path / "cfg.toml").write_text("")
    rc = lint.main(["--root", str(root), "--config", str(tmp_path / "cfg.toml"), "--allowlist", str(tmp_path / "a.txt"),
                    "--terms-file", str(root / "my-terms.txt")])
    assert rc == 0


def test_bad_regex_in_terms_file_is_reported(tmp_path):
    terms = tmp_path / "t.txt"
    terms.write_text("re:(unclosed\n")
    with pytest.raises(lint.TermsError):
        lint.load_terms(terms)


# ---------------------------------------------------------------- Ward round 2, LOW-C: unusable terms are an error; CI prints no terms

@pytest.mark.parametrize("body", ["", "# only a comment\n\n", "word:\n", "sub:   \n", "re:\n", "re:a*\n", "re:(unclosed\n"])
def test_a_terms_file_with_no_rules_or_an_empty_rule_exits_2(tmp_path, capsys, body):
    terms = tmp_path / "terms.txt"
    terms.write_text(body)
    assert _run(tmp_path, {"a.md": "x\n"}, config="", extra=["--terms-file", str(terms)]) == 2
    assert "unusable private terms file" in capsys.readouterr().err


def test_an_empty_toml_terms_file_exits_2(tmp_path):
    for body in ("", 'words = [""]\n', 'exclude = ["x/*"]\n'):
        terms = tmp_path / "terms.toml"
        terms.write_text(body)
        assert _run(tmp_path, {"a.md": "x\n"}, config="", extra=["--terms-file", str(terms)]) == 2


def test_in_ci_a_hit_prints_only_path_and_line(tmp_path, monkeypatch, capsys):
    terms = tmp_path / "terms.txt"
    terms.write_text(f"word:{NAME}\n")
    files = {"a.md": f"fine\nsay {NAME} loudly\n"}
    monkeypatch.setenv("CI", "true")
    assert _run(tmp_path, files, config="", extra=["--terms-file", str(terms)]) == 1
    cap = capsys.readouterr()
    assert cap.out.splitlines()[0] == "a.md:2" and NAME not in cap.out + cap.err and "loudly" not in cap.out
    monkeypatch.delenv("CI")
    assert _run(tmp_path, files, config="", extra=["--terms-file", str(terms)]) == 1
    assert f"a.md:2: {NAME}: say {NAME} loudly" in capsys.readouterr().out         # outside CI the detail stays


def test_in_ci_an_unusable_terms_file_names_no_term(tmp_path, monkeypatch, capsys):
    terms = tmp_path / "terms.txt"
    terms.write_text(f"word:{NAME}\nre:({NAME}\n")
    monkeypatch.setenv("CI", "true")
    assert _run(tmp_path, {"a.md": "x\n"}, config="", extra=["--terms-file", str(terms)]) == 2
    cap = capsys.readouterr()
    assert NAME not in cap.out + cap.err and "terms.txt:2" in cap.err


# ---------------------------------------------------------------- the mod's lint.sh follows the same rules

def _mod_copy(tmp_path):
    dst = tmp_path / "mod"
    shutil.copytree(ROOT / "claude-code" / "chronicle", dst, ignore=shutil.ignore_patterns("node_modules", ".claude-plugin"))
    shutil.copytree(ROOT / "claude-code" / "chronicle" / ".claude-plugin", dst / ".claude-plugin",
                    ignore=shutil.ignore_patterns("types"))
    return dst


def _lint_sh(mod, env_terms=None):
    import os
    env = {k: v for k, v in os.environ.items() if k != lint.ENV_TERMS}
    if env_terms:
        env[lint.ENV_TERMS] = str(env_terms)
    return subprocess.run(["bash", str(mod / "scripts" / "lint.sh")], capture_output=True, text=True, env=env)


def test_mod_lint_standalone_uses_the_private_terms_file(tmp_path):
    mod = _mod_copy(tmp_path)                     # no repository around it: the built-in fallback runs
    clean = _lint_sh(mod)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "no PRIVATE_STRIP_TERMS_FILE" in clean.stderr
    terms = tmp_path / "terms.txt"
    terms.write_text(f"word:{NAME}\nsub:{HOST}\n")
    (mod / "README.md").write_text(f"a note about {NAME}\n")
    bad = _lint_sh(mod, terms)
    assert bad.returncode == 1 and f"README.md:1:" in bad.stdout
    (mod / "README.md").write_text(f"a note about nothing\n")
    (mod / "hooks" / "x.txt").write_text("cd /home/jdoe/app/\n")
    assert _lint_sh(mod).returncode == 1                                           # the home-path shape needs no file
    (mod / "hooks" / "x.txt").unlink()
    missing = _lint_sh(mod, tmp_path / "missing.txt")                              # unreadable terms file: an error
    assert missing.returncode == 2 and "cannot read PRIVATE_STRIP_TERMS_FILE" in missing.stdout
    for body in ("# nothing here\n", "word:\n", "re:a*\n"):                         # no rules, or an empty rule
        terms.write_text(body)
        res = _lint_sh(mod, terms)
        assert res.returncode == 2, (body, res.stdout, res.stderr)
    terms.write_text(f"word:{NAME}\n")
    (mod / "README.md").write_text(f"a note about {NAME}\n")
    import os
    ci = subprocess.run(["bash", str(mod / "scripts" / "lint.sh")], capture_output=True, text=True,
                        env={**os.environ, "CI": "true", lint.ENV_TERMS: str(terms)})
    assert ci.returncode == 1 and "README.md:1" in ci.stdout and NAME not in ci.stdout + ci.stderr


def test_mod_lint_in_the_repository_delegates_to_the_shared_script():
    text = (ROOT / "claude-code" / "chronicle" / "scripts" / "lint.sh").read_text()
    assert "private_strip_lint.py" in text and "--only claude-code/chronicle/" in text
    assert "PRIVATE_STRIP_TERMS_FILE" in text
