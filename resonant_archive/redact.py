"""Redaction: remove credentials from text before it is stored.

Two layers, both run over a whole string (before any truncation, so a key split by a cut cannot slip past):

1. Known values: secrets you point it at (``known_secrets`` files, see ``load_known``). Every occurrence is replaced,
   wherever it appears. Values are held in memory only and never printed.
2. Generic patterns for common credential shapes (API keys, tokens, private keys, connection strings, secret URL
   paths, ``KEY=value`` assignments).

The patterns mirror the chronicle mod's, so a row redacted at the source and again here is unchanged. Redaction is
pattern based and errs towards removing too much: a leaked key can only be rotated, an over-redacted word costs a
few characters. It is a safety net, not a guarantee: keep the archive private and never commit it to a public
repository.
"""

from __future__ import annotations

import collections
import json
import os
import re
from typing import Iterable

from . import textio

#: Bump when the patterns change in a way that should re-redact what is already imported: the import pipeline mixes
#: it into every unit's hash, so an unchanged input is read again once.
REDACTION_VERSION = 3   # 3: Windows shapes (cmd set/setx, PowerShell $env: and -Password, env listings, reg, net use)

#: Longer strings are cut here first (callers keep far less than this).
SCAN_MAX = 512 * 1024

# --- private keys: a linear scan, not a regex -------------------------------------------------------------------------
# A "BEGIN ... END" regex backtracks quadratically on many BEGIN lines with no END (500 KB took over a minute), so the
# markers are found one at a time: BEGIN, then the first END after it, then on. No END at all: the rest is redacted.
PRIVATE_KEY_MARK = "[redacted:private-key]"
_PK_BEGIN = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
_PK_END = re.compile(r"-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")


def scrub_private_keys(s: str) -> tuple[str, int]:
    """Replace every private-key block (PEM, OpenSSH, PGP) with a marker. Linear in the length of ``s``."""
    if "PRIVATE KEY" not in s:
        return s, 0
    out: list[str] = []
    pos = hits = 0
    while True:
        b = _PK_BEGIN.search(s, pos)
        if not b:
            break
        hits += 1
        out.append(s[pos:b.start()])
        out.append(PRIVATE_KEY_MARK)
        e = _PK_END.search(s, b.end())
        if not e:
            return "".join(out), hits  # no end in sight: all of it
        pos = e.end()
    out.append(s[pos:])
    return "".join(out), hits


# (kind, pattern, replacement). Replacements keep a key name before the value where there is one. A replacement is
# meant to leave text the same pattern passes over, so redacting already-redacted text should change nothing: the test
# corpus checks that for every shape listed, it is not proven for arbitrary input. The chronicle mod keeps a twin of
# this list in claude-code/chronicle/hooks/register.ts: change both together.
_FLAG_VALUE = r"""('[^']*'|"[^"]*"|[^\s"']+)"""
# A word that makes a name secret-looking, and a whole variable name containing one (bounded: no backtracking blow-up).
_WIN_WORD = r"(?:key|token|secret|passw(?:or)?d|pwd|pass|credential)"
_WIN_NAME = r"(?:[A-Za-z_][\w.-]{0,60}?)?" + _WIN_WORD + r"[\w.-]{0,60}"
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("api-key", re.compile(r"\bsk-(?:ant-|or-v1-|proj-)?[A-Za-z0-9_-]{20,}"), "[redacted:api-key]"),
    ("api-key", re.compile(r"\bxai-[A-Za-z0-9_-]{16,}"), "[redacted:api-key]"),
    ("api-key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), "[redacted:api-key]"),
    ("api-key", re.compile(r"\bwhsec_[A-Za-z0-9]{16,}"), "[redacted:api-key]"),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), "[redacted:npm-token]"),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "[redacted:hf-token]"),
    ("gitlab-token", re.compile(r"\bgl(?:pat|ptt|rt|dt|oas|soat|ft|imt)-[A-Za-z0-9_-]{20,}"), "[redacted:gitlab-token]"),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), "[redacted:sendgrid-key]"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[redacted:github-token]"),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), "[redacted:github-token]"),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}"), "[redacted:aws-key]"),
    ("aws-secret",
     re.compile(r"(aws[ _-]?secret[ _-]?(?:access[ _-]?)?key[\"']?[ \t]*[:=][ \t]*[\"']?)(?!\[redacted)[A-Za-z0-9/+=]{16,}", re.I),
     r"\1[redacted:secret]"),
    ("azure-key",
     re.compile(r"(\b(?:AccountKey|SharedAccessKey|Pwd)[ \t]*=[ \t]*)(?!\[redacted)[^;\s\"']{8,}", re.I),
     r"\1[redacted:secret]"),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "[redacted:google-key]"),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted:slack-token]"),
    ("webhook",
     re.compile(r"https?://hooks\.slack\.com/(?:services|workflows|triggers|actions)/[A-Za-z0-9/_-]{8,}"),
     "[redacted:webhook]"),
    ("webhook",
     re.compile(r"https?://(?:[a-z0-9-]+\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[A-Za-z0-9._-]{20,}"),
     "[redacted:webhook]"),
    ("discord-token", re.compile(r"\b[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}"),
     "[redacted:discord-token]"),
    ("twilio", re.compile(r"\b(?:AC|SK)[0-9a-f]{32}\b"), "[redacted:twilio]"),
    ("jwt", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"), "[redacted:jwt]"),
    ("telegram-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"), "[redacted:telegram-token]"),
    # Any scheme://user:password@host. The password may hold '@' (the last '@' before the host ends it) but not '/'.
    ("url-password",
     re.compile(r"\b([a-z][a-z0-9+.-]{1,20}://[^:/\s@\"'<>]*:)[^\s/\"'<>]+@", re.I),
     r"\1[redacted:password]@"),
    ("cli-credentials",
     re.compile(r"(?<![\w-])((?:-u|--user)(?:[ \t]+|=)[\"']?)(?!\[redacted)[^\s:\"']{1,200}:[^\s\"']+"),
     r"\1[redacted:credentials]"),
    ("cli-password",
     re.compile(r"(?<![\w-])(--(?!no-)(?:[a-z0-9]+-)*(?:password|passwd|pass|passphrase|pwd|token|secret|api-key|apikey)"
                r"(?:=|[ \t]+)[\"']?)(?!-|\[redacted)[^\s\"']{4,}", re.I),
     r"\1[redacted:secret]"),
    ("cli-password",
     re.compile(r"(\bsshpass[ \t]+-p[ \t]*)(?!\[redacted)" + _FLAG_VALUE),
     r"\1[redacted:password]"),
    ("cli-password",
     re.compile(r"(\bmysql(?:dump|admin|pump|sh)?\b[^\n]{0,200}?[ \t]-p)(?![\s\[])" + _FLAG_VALUE),
     r"\1[redacted:password]"),
    ("cli-password",
     re.compile(r"(\bredis-cli\b[^\n]{0,200}?[ \t]-a[ \t]+)(?!\[redacted)" + _FLAG_VALUE),
     r"\1[redacted:password]"),
    ("bearer", re.compile(r"\b(bearer)(\s+)[A-Za-z0-9._~+/=-]{16,}", re.I), r"\1\2[redacted:token]"),
    ("basic", re.compile(r"\b(basic)(\s+)(?=[A-Za-z0-9+/=]*\d)[A-Za-z0-9+/]{16,}={0,2}", re.I), r"\1\2[redacted:token]"),
    ("authorization",
     re.compile(r"(\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:(?:token|basic|bearer|digest|apikey|api-key|key)[ \t]+)?)"
                r"(?!\[redacted|(?:token|basic|bearer|digest)[ \t])[^\s\"',;}]{8,}", re.I),
     r"\1[redacted:secret]"),
    ("secret-path", re.compile(r"/mcp/[A-Za-z0-9_-]{16,}"), "/mcp/[redacted:secret-path]"),
    ("secret-path", re.compile(r"\bmcp_[0-9a-f]{8,}"), "[redacted:secret-path]"),
    ("url-secret", re.compile(r"([?&](?:key|token|api_key|apikey|access_token|sig|secret)=)[^&\s\"'<>]+", re.I),
     r"\1[redacted]"),
    ("env-assignment",
     re.compile(r"^([ \t]*(?:export\s+)?[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS)[A-Z0-9_]*\s*=\s*).+$", re.M),
     r"\1[redacted:secret]"),
    # --- Windows: cmd, PowerShell, wmic, reg, cmdkey -------------------------------------------------------------------
    # cmd:  set API_KEY=value   set "API_KEY=value"   setx API_KEY value   (any value length: the name says it is secret)
    ("win-set", re.compile(r'(?<![\w-])(@?set[ \t]+"' + _WIN_NAME + r'=)(?!\[redacted)[^"\r\n]*', re.I), r"\1[redacted:secret]"),
    ("win-set", re.compile(r"(?<![\w-])(@?set[ \t]+" + _WIN_NAME + r"=)(?!\[redacted)[^\s&|]+", re.I), r"\1[redacted:secret]"),
    ("win-set", re.compile(r'(?<![\w-])(setx(?:\.exe)?[ \t]+"?' + _WIN_NAME + r'"?[ \t]+)(?![\[/])' + _FLAG_VALUE, re.I),
     r"\1[redacted:secret]"),
    # PowerShell:  $env:API_KEY = "value"   [Environment]::SetEnvironmentVariable("API_KEY", "value", "User")
    ("win-env",
     re.compile(r"((?<![\w-])\$\{?env:" + _WIN_NAME + r"\}?[ \t]*=[ \t]*)(?!\[redacted|[$(<{%@]|[A-Za-z]+-[A-Za-z])" + _FLAG_VALUE,
                re.I), r"\1[redacted:secret]"),
    ("win-env",
     re.compile(r"(SetEnvironmentVariable\([ \t]*[\"']" + _WIN_NAME + r"[\"'][ \t]*,[ \t]*)(?!\[redacted)(\"[^\"]*\"|'[^']*')",
                re.I), r"\1[redacted:secret]"),
    # PowerShell and Windows tools take -Password value, -Token:value, /pass:value, /RP value (schtasks), -pw value (plink)
    ("win-flag",
     re.compile(r"(?<![\w-])(-(?:password|passwd|pass|pwd|pw|passphrase|token|secret|apikey|accesstoken|clientsecret|secretkey)"
                r"(?::|[ \t]+))(?!\[redacted|-|[$(@])" + r"(\"[^\"]*\"|'[^']*'|[^\s\"']{3,})", re.I),
     r"\1[redacted:secret]"),
    ("win-flag", re.compile(r"(?<![\w/.-])(/(?:password|passwd|pass|pwd|token|secret|apikey)[:=])(?!\[redacted)" + _FLAG_VALUE, re.I),
     r"\1[redacted:secret]"),
    ("win-flag", re.compile(r"(?<![\w/.-])(/RP[ \t]+)(?!\[redacted|/|\*)" + _FLAG_VALUE),
     r"\1[redacted:password]"),
    ("win-flag", re.compile(r"(\bnet(?:\.exe)?[ \t]+use[ \t]+(?:\S+[ \t]+)?\\\\\S+[ \t]+)(?![/*]|\[redacted)" + _FLAG_VALUE, re.I),
     r"\1[redacted:password]"),
    ("win-flag", re.compile(r"(\bnet(?:\.exe)?[ \t]+user[ \t]+\S+[ \t]+)(?![/*]|\[redacted)" + _FLAG_VALUE, re.I),
     r"\1[redacted:password]"),
    ("win-flag", re.compile(r"(\bsqlcmd(?:\.exe)?\b[^\n]{0,200}?[ \t]-P[ \t]*)(?![\[\s])" + _FLAG_VALUE, re.I),
     r"\1[redacted:password]"),
    ("win-flag", re.compile(r"(\b[Pp]s[Ee]xec(?:64)?(?:\.exe)?\b[^\n]{0,200}?[ \t]-p[ \t]+)(?!\[redacted)" + _FLAG_VALUE),
     r"\1[redacted:password]"),
    ("win-flag",
     re.compile(r"(\bConvertTo-SecureString[ \t]+(?:-String[ \t]+)?)(?!\[redacted)(\"[^\"]*\"|'[^']*')(?=[^\n]{0,200}-AsPlainText)", re.I),
     r"\1[redacted:password]"),
    # An environment listing: `gci env:` (Name, then Value, in columns), `gci env: | fl` (Name : ... / Value : ...) and
    # `wmic environment` (Name=... / VariableValue=...).
    ("win-env-listing",
     re.compile(r"^((?:[A-Z][A-Z0-9_]{0,60}?)?(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|PASS|CREDENTIALS?)[A-Z0-9_]{0,60}[ \t]{2,})(?![ \t]|\[redacted).+$",
                re.M), r"\1[redacted:secret]"),
    ("win-env-listing",
     re.compile(r"^([ \t]{0,16}Name[ \t]{0,16}[:=][ \t]{0,16}[A-Za-z0-9_]{0,60}?" + _WIN_WORD + r"[A-Za-z0-9_]{0,60}[ \t]*\r?\n"
                r"[ \t]*(?:Value|VariableValue)[ \t]*[:=][ \t]*)(?![ \t]|\[redacted).+$", re.I | re.M), r"\1[redacted:secret]"),
    # reg query output:  <name>    REG_SZ    <value>   and   reg add ... /v <name> /d <value>
    ("win-registry",
     # The value name runs to the first double space (one scan, no second lazy window), and a lookahead finds the secret word in it.
     re.compile(r"^([ \t]{1,16}(?![ \t])(?=[^\r\n]{0,120}?" + _WIN_WORD + r")(?:(?![ \t]{2})[^\r\n]){1,120}[ \t]{2,}REG_(?:SZ|EXPAND_SZ|MULTI_SZ|BINARY)[ \t]{2,})(?![ \t]|\[redacted).+$",
                re.I | re.M), r"\1[redacted:secret]"),
    ("win-registry",
     re.compile(r"(\breg(?:\.exe)?[ \t]+add\b(?:(?!/v[ \t])[^\r\n]){0,80}/v[ \t]+\"?[^\s\"]{0,40}?" + _WIN_WORD + r"[^\s\"]{0,40}\"?(?:(?!/d[ \t])[^\r\n]){0,40}/d[ \t]+)"
                r"(?!\[redacted)" + _FLAG_VALUE, re.I), r"\1[redacted:secret]"),
    # name [: or =] value, with the name anywhere in the word (DB_PASSWORD, "apiKey", secret access key). Values that
    # are placeholders (${VAR}, <your-key>, %s) or already redacted are left; short values (a type name) are left.
    ("assignment",
     re.compile(r"((?:secret[ _-]?access[ _-]?key|secret[ _-]?key|access[ _-]?key|api[ _-]?key|private[ _-]?key|"
                r"client[ _-]?secret|secret|password|passwd|passphrase|token|credentials?)[A-Za-z0-9_-]{0,30}[\"']?"
                r"[ \t]*[:=][ \t]*[\"']?)(?!\[redacted|[$<{%])[^\s\"',;}\]]{8,}", re.I),
     r"\1[redacted:secret]"),
    # A short value after an explicit password key (the rule above wants 8+): password=abcd, "pwd": "x1y2z3". Code that
    # only names a type or a call (password: string, password = input(...)) is left. `pwd` alone is the current
    # directory in an environment listing, so a path after it is left.
    ("short-password",
     re.compile(r"((?:password|passwd)[A-Za-z0-9_-]{0,30}[\"']?[ \t]*[:=][ \t]*[\"']?|pwd[\"']?[ \t]*[:=][ \t]*[\"']?(?![/~]))"
                r"(?!\[redacted|[$<{%])"
                r"(?!(?:none|null|nil|true|false|undefined|string|str|bool|boolean|int|integer|number|input|getpass|prompt|"
                r"hidden|text|field|value|optional|required|default|self|args|env|os|config|process|form|request|user|data|"
                r"dict|list|type)(?![\w]))"
                r"(?![A-Za-z_][\w.]*[(\[])[^\s\"',;}\]]{4,}", re.I),
     r"\1[redacted:secret]"),
    # ~/.pgpass lines: host:port:database:user:password
    ("pgpass",
     re.compile(r"^([^\s:#]+:(?:\d{1,5}|\*):[^\s:]+:[^\s:]+:)\S+$", re.M),
     r"\1[redacted:password]"),
    # Prose: "the password is hunter2", "password for admin was X".
    ("prose-password",
     re.compile(r"\b((?:password|passwd|passphrase|passcode|secret|api[ _-]key|token)s?(?:[ \t]+for[ \t]+[\w.@-]{1,40})?"
                r"[ \t]+(?:is|was|=)[ \t]*:?[ \t]*[\"'`]?)(?!\[redacted)"
                r"(?!(?:the|an?|not|now|set|stored|required|wrong|correct|incorrect|invalid|valid|empty|missing|expired|"
                r"different|same|here|in|on|for|to|used|sent|ok|fine|right|changed|reset|updated|saved|"
                r"unchanged|supposed|needed|still|also|only|just|being|that|this|there)(?![\w-]))"
                r"[^\s\"'`,;]{6,}", re.I),
     r"\1[redacted:secret]"),
    # Last: a long token (40+ characters: shorter ones were mostly identifiers in code) that mixes upper case, lower case and digits and is not hex-only (so commit ids, UUIDs and
    # paths pass). Starts only at a token start, so the lookaheads run once per token: linear.
    ("high-entropy",
     re.compile(r"(?<![A-Za-z0-9_+-])(?=[A-Za-z0-9_+-]*[a-z])(?=[A-Za-z0-9_+-]*[A-Z])(?=[A-Za-z0-9_+-]*\d)"
                r"[A-Za-z0-9_+-]{40,}"),
     "[redacted:secret]"),
]

_PRIVATE_KEY_START = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")

#: Patterns alone, for callers that only need to know "does this look like a secret" (anchors).
SECRET_REGEXES: list[re.Pattern[str]] = [_PRIVATE_KEY_START] + [rx for _, rx, _ in PATTERNS]


def _plausible_known(v: str) -> bool:
    """Keep known values that are secret-shaped; drop paths, plain URLs and lowercase words/hosts."""
    if len(v) < 12 or v.startswith("/") or "://" in v:
        return False
    upper = sum(c.isupper() for c in v)
    digits = sum(c.isdigit() for c in v)
    return upper > 0 or digits >= 4


def load_known(paths: Iterable[str], json_configs: Iterable[str] = ()) -> list[str]:
    """Secret values read from the files (or folders) you name, longest first. Held in memory, never printed.

    From each text file: the value of every ``NAME=value`` / ``name: value`` line, and any long token-looking run.
    From each JSON config (an MCP config, for instance): ``/mcp/<secret>`` URL segments and values under keys that
    look like auth, token, key, secret or password. There are no default paths: you choose what is secret.
    """
    vals: set[str] = set()
    for p in paths:
        p = os.path.expanduser(str(p))
        if os.path.isdir(p):
            files = [os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs]
        else:
            files = [p]
        for f in files:
            try:
                text = textio.read_text(f)   # UTF-8, with or without a BOM, or UTF-16: what Windows tools write
            except OSError:
                continue
            for m in re.finditer(r"(?m)^\s*[A-Za-z0-9_]+\s*[=:]\s*[\"']?([^\s\"'#]{12,})", text):
                vals.add(m.group(1))
            for m in re.finditer(r"[A-Za-z0-9._~+/=-]{24,}", text):
                vals.add(m.group(0))
    for p in json_configs:
        try:
            with open(os.path.expanduser(str(p)), encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            continue
        vals.update(_json_secrets(cfg))
    return sorted((v for v in vals if _plausible_known(v)), key=len, reverse=True)


def _json_secrets(obj, key: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _json_secrets(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_secrets(v, key)
    elif isinstance(obj, str):
        for m in re.finditer(r"/mcp/([A-Za-z0-9_-]{16,})", obj):
            yield m.group(1)
        if re.search(r"(?i)(authorization|token|key|secret|password)", key):
            v = re.sub(r"(?i)^\s*bearer\s+", "", obj).strip()
            if len(v) >= 16:
                yield v


def redact(text: str, known: Iterable[str] = ()) -> str:
    """Redact one string. Pure; never raises on a string."""
    s = "" if text is None else str(text)
    if not s:
        return s
    if len(s) > SCAN_MAX:
        s = s[:SCAN_MAX]
    for v in known:
        if v in s:
            s = s.replace(v, "[redacted:known]")
    s, _ = scrub_private_keys(s)
    for _, rx, to in PATTERNS:
        s = rx.sub(to, s)
    return s


class Redactor:
    """Callable that redacts and counts what it changed (counts only, never text)."""

    def __init__(self, known: Iterable[str] = ()):
        self.known = sorted(set(known), key=len, reverse=True)
        self.counts: collections.Counter[str] = collections.Counter()

    def __call__(self, text: str | None, counts: collections.Counter | None = None, *, ident: bool = False) -> str:
        """Redact ``text`` and count what changed. ``ident=True`` is for identifiers: the long-token rule is skipped."""
        if not text:
            return text or ""
        c = self.counts if counts is None else counts
        s = text if len(text) <= SCAN_MAX else text[:SCAN_MAX]
        for v in self.known:
            if v in s:
                c["known"] += s.count(v)
                s = s.replace(v, "[redacted:known]")
        s, n = scrub_private_keys(s)
        if n:
            c["private-key"] += n
        for kind, rx, to in PATTERNS:
            if ident and kind == "high-entropy":
                continue
            hits = 0

            def swap(m: re.Match[str], to: str = to) -> str:
                nonlocal hits
                new = m.expand(to)
                if new != m.group(0):  # text already redacted (by the mod, or on a re-import) is not counted again
                    hits += 1
                return new

            s = rx.sub(swap, s)
            if hits:
                c[kind] += hits
        return s
