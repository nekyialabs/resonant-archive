#!/usr/bin/env bash
# Run from anywhere: scripts/lint.sh
# 1. The shipped tree holds no private names, hosts, ports or paths.
#    This repository is public, so the private terms are NOT in it: keep them in a file of your own and point
#    PRIVATE_STRIP_TERMS_FILE at it (the format is described at the top of scripts/private_strip_lint.py in the
#    repository root). Without that file only the public checks run (home directories, key and password shapes), and
#    the lint says so. A set but unreadable file is an error, never a silent pass.
#    A terms file that cannot be read, yields no rules or holds an empty rule exits 2. With CI=true only path:line is
#    printed for a hit (never the term or the line), so a private term cannot reach a public log.
# 2. chronicle.example.json parses, and matches the copy in tests/config.test.ts.
set -u
cd "$(dirname "$0")/.."
fail=0

# A Python that actually runs. On Windows `python3` is often the Microsoft Store stub (it prints "Python was not
# found" and exits 49) and the standard installer only provides `python`, so each candidate is run before it is used.
PY=
for c in python3 python py; do
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then PY=$c; break; fi
done
if [ -z "$PY" ]; then echo "lint: no working python3 or python on PATH" >&2; exit 2; fi

shared="../../scripts/private_strip_lint.py"
if [ -f "$shared" ] && [ -f ../../scripts/private_strip_lint.toml ]; then
  # inside the repository: the same lint, the same rules, limited to this folder
  "$PY" "$shared" --root ../.. --only claude-code/chronicle/ || fail=$?
else
  # the mod on its own (no repository around it): the private terms file, plus the home-directory check
  "$PY" - <<'PY' || fail=$?
import os, re, subprocess, sys
terms = os.environ.get('PRIVATE_STRIP_TERMS_FILE')
rules = [('home path', re.compile(r'(?:/home|/Users)/(?!(?:user|users|you|me|name|username|someone|tester|example|\$|<|\{|\*|%|~)(?![A-Za-z0-9_]))[A-Za-z_][A-Za-z0-9_.-]*/', re.I))]
if terms:
    try:
        lines = open(os.path.expanduser(terms), encoding='utf-8').read().splitlines()
    except OSError as e:
        print('lint: cannot read PRIVATE_STRIP_TERMS_FILE' + ('' if os.environ.get('CI', '').lower() == 'true' else f' {terms}: {e}')); sys.exit(2)
    named = 0
    for n, raw in enumerate(lines, 1):
        t = raw.strip()
        if not t or t.startswith('#'): continue
        body = t[5:].strip() if t.startswith('word:') else t[3:].strip() if t.startswith('re:') else t[4:].strip() if t.startswith('sub:') else t
        if not body:
            print(f'lint: terms file line {n}: empty rule'); sys.exit(2)
        named += 1
        if t.startswith('word:'): rules.append((t, re.compile(r'(?<![A-Za-z0-9_])' + re.escape(body) + r'(?![A-Za-z0-9_])', re.I)))
        elif t.startswith('re:'):
            try: rx = re.compile(body, re.I)
            except re.error: print(f'lint: terms file line {n}: not a valid regular expression'); sys.exit(2)
            if rx.search(''): print(f'lint: terms file line {n}: matches the empty string, so every line'); sys.exit(2)
            rules.append((t, rx))
        else: rules.append((t, re.compile(re.escape(body), re.I)))
    if not named:
        print('lint: the private terms file yields no rules'); sys.exit(2)
else:
    print('lint: no PRIVATE_STRIP_TERMS_FILE; checked the public shapes only', file=sys.stderr)
skip = os.path.realpath(os.path.expanduser(terms)) if terms else None
try:
    names = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, check=True).stdout.decode().split('\0')
except Exception:
    names = [os.path.join(d, f) for d, _, fs in os.walk('.') for f in fs]
bad = 0
for n in (os.path.normpath(x).replace(os.sep, '/') for x in names if x):   # '/' on Windows too: the prefixes below
    if n.startswith(('.claude-plugin/types/', 'node_modules/', '.git/')) or n == 'scripts/lint.sh' or n.startswith('tests/'): continue
    if skip and os.path.realpath(n) == skip: continue
    try: text = open(n, encoding='utf-8', errors='replace').read()
    except OSError: continue
    for i, line in enumerate(text.splitlines(), 1):
        for name, rx in rules:
            if rx.search(line): print(f'{n}:{i}' if os.environ.get('CI', '').lower() == 'true' else f'{n}:{i}: {name}: {line.strip()[:140]}'); bad = 1
sys.exit(bad)
PY
fi

"$PY" - <<'PY' || { [ "$fail" -ne 0 ] || fail=1; }   # keep exit 2 (unusable terms file) over 1
import json, re, sys
ex = json.load(open('chronicle.example.json'))
t = open('tests/config.test.ts').read()
m = re.search(r'// EXAMPLE-BEGIN\nconst example = JSON\.parse\(`(.*?)`\)\n// EXAMPLE-END', t, re.S)
if not m or json.loads(m.group(1)) != ex:
    print('lint: chronicle.example.json differs from the copy in tests/config.test.ts'); sys.exit(1)
PY

if [ "$fail" -eq 0 ]; then echo "lint: clean"; fi
exit "$fail"
