# Privacy

resonant-archive holds your conversations. This page says where they go, and where they don't.

## Where your data lives

- **The mirror** (written by the chronicle mod): `~/.chronicle/archive` by default. Folders 0700, files 0600.
- **The database**: Postgres. With the bundled compose file, a named Docker volume on your machine.
- **The database address** (it contains a password): `~/.config/resonant-archive/config.toml`, mode 0600, or the
  `RESONANT_ARCHIVE_DSN` environment variable (Windows: `%LOCALAPPDATA%/resonant-archive/config/config.toml`, readable by your account alone through an ACL, since Windows has no mode bits). Never a file inside the repository, and keep it off the command line
  (`--dsn` is visible to other users through `ps`; the docs use the variable). There are two addresses: the owner's
  `dsn`, and `readonly_dsn` for a login that can only read, which `init` creates for the MCP server and `recall`.
- **The `.env` that `init --docker` writes** (the database password): in `~/.local/share/resonant-archive/docker` unless
  you choose `--project-dir`, mode 0600, and git-ignored. A placeholder or short password in an existing `.env` is
  replaced with a random one; a good one is kept and its mode is tightened to 0600.
- **The model**: `models/nomic-embed-text-v1.5` inside the data folder (`~/.local/share/resonant-archive` on Linux,
  `~/Library/Application Support/resonant-archive` on macOS, `%LOCALAPPDATA%/resonant-archive` on Windows (the `.env` above goes in its `docker` folder there); `RESONANT_ARCHIVE_DATA_DIR` or
  `RESONANT_EMBEDDER_MODEL_DIR` override it).

## What leaves your machine

Nothing, with one exception.

- **No telemetry. No accounts. No API keys.** Embeddings are computed locally with ONNX Runtime.
- **One download:** `resonant-archive models fetch` downloads the embedding model from Hugging Face (about 550 MB), at
  a pinned revision, and checks every file against a pinned SHA-256 sum. A file that doesn't match is deleted and the
  command fails. To avoid even that, put the files in a folder yourself and use `--model-dir`. All four files are
  checked again before the model is loaded, by the embed server and by any command that loads it in-process; a folder
  that verified before and has not changed (same size and modification time, recorded in `.verified.json`) is not
  hashed again. That marker is a speed-up, not protection from someone who can already write to the model folder.
- The embed server and the MCP server make no outbound connections.

## What listens on the network

- **Postgres (bundled):** `127.0.0.1:54329` only. Not reachable from other machines.
- **The embed server** (`resonant-archive serve`, port 8899): refuses to start on anything but a loopback address,
  refuses a request whose `Host` header is not a loopback name (so a web page cannot reach it through DNS rebinding),
  accepts only `Content-Type: application/json` (so a cross-site form post cannot), and drops a client that stalls
  for more than 30 seconds.
- **The MCP server:** stdio. It opens no port.

If you run your own Postgres on a network, securing it is on you. `init` creates a read-only login (`archive_reader`:
SELECT on the archive's tables and nothing else) and saves it as `readonly_dsn`; the MCP server and `recall` connect
with it, so a hostile query cannot write even by turning the session flag off. They also open the session read-only
with a 10 second statement timeout, and pass every value as a bound parameter. `doctor` warns when `readonly_dsn` is
unset or is the owner's login. If your owner role cannot create roles, [SCHEMA.md](SCHEMA.md) has the SQL.

## Redaction

Credentials are removed before text is stored. This happens twice: the mod redacts when it writes the mirror, and the
importers redact again (the patterns are the same, so a row redacted at the source is unchanged). It covers:

- API keys and tokens for common services (OpenAI-style, Anthropic, Stripe, GitHub, GitLab, npm, Hugging Face,
  SendGrid, Twilio, Slack, Discord, Telegram, Google, AWS access keys and `aws_secret_access_key` values, Azure
  `AccountKey=`), JWTs
- private key blocks (PEM, OpenSSH, PGP), found by a linear scan, so a file full of `BEGIN` lines cannot stall an import
- passwords in any `scheme://user:password@host` address (a password may contain `@`), in `curl -u`, `sshpass -p`,
  `mysql -pPASSWORD`, `redis-cli -a`, and `--password` / `--token` style flags
- `Authorization:` values (`Bearer`, `Basic`, `Token`, ...), Slack and Discord webhook URLs, secret URL paths and query
  parameters
- assignments: `KEY=value` for names ending in KEY, TOKEN, SECRET or PASSWORD, and `name: value` / `name = value`
  where the name contains secret, password, passwd, passphrase, token, api key, access key or credential (any case)
- prose such as "the password is hunter2"
- `--db-pass=` and any `--*-password` / `--*-token` / `--*-secret` flag, `~/.pgpass` lines (`host:port:db:user:password`), and a value of 4 or more characters after an explicit `password`, `passwd` or `pwd` key (code that only names a type or a call is left)
- any other long token (40 characters or more) that mixes upper case, lower case and digits and is not hex-only; commit
  ids and UUIDs are hex, so they pass

Redaction covers every stored string, not only the message text: titles, labels, file names, model names, tags and
ids (an id that contains a secret becomes a stable hash of itself). It runs on the whole string before anything is
cut to a length limit, so a key a cut would split is already gone.

Two layers sit on top of the patterns. First, a tool call that reads a secret-bearing source (a `.env` or `.env.*`,
`.envrc`, `.npmrc`, `.netrc`, `.git-credentials`, `.docker/config.json`, `.kube/config`, `secrets.*`, `*.pem`, `*.p12`,
`terraform.tfstate*`, `id_*` keys, `credentials.json`, `.mcp.json`, `/proc/*/environ`, anything under a `private/`
folder; `env`, `printenv`, `ps`, `pm2 env`, `docker compose config`, `systemctl show`, `ssh-add -L`; a `Grep` or `Glob`
whose pattern or path names a password, token, key or secret file) has its result withheld entirely, and its *input*
is cut to the tool name and the file or program (never `content`, `old_string`, `new_string`, an `echo` argument or a
heredoc body). **This is best effort**: it recognises common shapes by name. A secret reached through a file or command
it does not know, a renamed file, a script, or a command assembled at run time gets through to the pattern redaction
only. The chronicle mod does that when it writes the mirror; the `claude_code` importer does the
same when it reads a Claude Code transcript directly. Other importers read notes and exported chats, which have no tool
results. Second, you can add your own: `import --known-secrets FILE_OR_FOLDER` redacts every value found there wherever
it appears, and `--json-config FILE` does the same for the string values in a JSON config. Values are held in memory
only and never printed.

**Redaction is a safety net, not a guarantee.** It is pattern based and errs toward removing too much: a leaked key can
only be rotated, an over-redacted word costs a few characters. A secret with no recognisable shape (a short password
with no label, a key in a language or encoding the patterns do not know, one split across two messages) gets through.
Don't rely on it to make sensitive conversations safe to share.

Commands print counts, never message text. Message text is never written to a log.

## What an import will read

An importer reads regular files inside the folder you point it at. It skips (and counts, in the report) a symlink, a
path that resolves outside that folder, a file that is not a regular file, a file over 256 MB, and a line over 16 MB in
a JSON-lines file. A symlink in an archive folder could otherwise name any file you can read.

## Keep it private

The mirror and the archive hold the full record of what you and your AI said. They are meant for you.

- Don't commit `~/.chronicle/archive` or a database dump to a public repository. A private repository is a choice you
  make with eyes open: redaction may have missed something.
- Don't expose Postgres or the embed server to the internet.
- On shared machines, check the permissions on the folders above.

## Deleting

To remove everything: delete the mirror folder, run `docker compose down -v` in the folder `init --docker` used
(`~/.local/share/resonant-archive/docker` unless you chose `--project-dir`; it removes the volume), and
delete `~/.config/resonant-archive` (Windows: `%LOCALAPPDATA%/resonant-archive`) and the data folder above. Nothing is held anywhere else.
