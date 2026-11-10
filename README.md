# resonant-archive

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-5eaba5.svg" alt="License: Apache-2.0" />
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776ab.svg?logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-compatible-6366f1.svg" alt="MCP" /></a>
  <a href="https://github.com/nekyialabs/resonant-archive/releases/latest"><img src="https://img.shields.io/github/v/release/nekyialabs/resonant-archive?color=5eaba5" alt="Release" /></a>
</p>

**Makes compaction more bearable.** Your AI writes its own compaction summary, in a voice you set. The recent stretch stays word for word. You choose when it happens. And nothing is truly lost, because every message is saved to your disk and your AI can search it.

Built for people who use Claude Code every day. Everything runs on your machine.

> **Early access.** The compaction mod uses Claude Code's hooks, which are an early-access feature and may change between releases. See [Caveats](#caveats).

## What is compaction, and why does it hurt?

A conversation with an AI lives in a window of limited size. When the window is nearly full, the tool compacts it: everything so far is replaced by one summary, and the conversation carries on from there.

That keeps you going. But the summary is written generically, in one big pass, without knowing what matters to you. Nobody else knows which bits were the bones. The details go: the file path, the id, the reason behind a decision, what you were about to do next. The last few exchanges, the ones you need most, get summarised too. And once it's done, it's done.

If you've ever watched your AI say "let me re-read that file" for the third time in a session, or come back after a compaction to a collaborator who has lost the thread, that is what this is.

|  | Plain compaction | With chronicle |
|---|---|---|
| Who writes the summary | A generic summariser | Your AI, in its own voice, in a voice you set |
| What it keeps | Whatever it guesses matters | Told to keep exact identifiers, decisions and open threads |
| The recent stretch | Summarised with the rest | Kept word for word (split compaction) |
| When it happens | When the window fills | When you choose (`/squish`), with a meter that warns at 75% and 90% |
| If something was dropped | Gone | Every row is mirrored to disk; `recall` brings back the original |

## The four promises

1. **Your AI writes its own summary**, in a voice you configure, and keeps exact identifiers, decisions and open threads.
2. **The recent stretch stays word for word.** You don't lose the last hour to a summary.
3. **You choose when.** `/squish` any time. A context meter in every prompt flags 75% and 90%.
4. **Nothing is truly lost.** Every row is mirrored to disk with secrets redacted, and `recall` finds the sharp version again, in the mirror or in the archive.

The compaction side is a Claude Code mod called **chronicle**, in [`claude-code/chronicle/`](claude-code/chronicle/). Its README has the install steps and every setting. The details of how compaction works are in [docs/COMPACTION.md](docs/COMPACTION.md).

## The archive: the safety net

The archive is the other half. It turns every conversation you've had (Claude Code sessions, the chronicle mirror, ChatGPT and claude.ai exports that went through Obsidian, folders of notes) into one searchable store in your own Postgres:

- **Exact words and strings.** Paths, ids, URLs, quoted phrases.
- **Meaning.** Local embeddings, so "that thing about the retry logic" finds it.
- **Both, fused.** Full text, vectors and exact anchors, rank-fused.

Your AI reaches it through MCP (`search`, `around`, `overview`, `associate`, `stats`). The chronicle mod reaches it through `resonant-archive recall`. You can also search from the command line.

You can use the mod without the archive (the mirror alone gives session recall), and the archive without the mod. Together they are one story: your AI keeps its own life, and can search it.

### Why Postgres?

Version 0.1 said "no database server". Version 2 trades that for exact word search, date filters, `around` and `overview`, and a real hybrid ranking. The cost is one container. `resonant-archive init --docker --up` does it for you, or bring your own Postgres 15+ with pgvector.

## Quick start

You need Python 3.11+, Docker (or your own Postgres), and for the mod, Claude Code on Linux, macOS or Windows (see [Platforms](#platforms)).

### 1. The mod (two minutes, no database)

Follow [`claude-code/chronicle/README.md`](claude-code/chronicle/README.md). With no config file at all it mirrors your sessions to `~/.chronicle/archive`, steers compaction, and turns on `/squish` and the meter.

### 2. The archive

```bash
git clone https://github.com/nekyialabs/resonant-archive
cd resonant-archive
pip install -e .                       # or: pipx install .

resonant-archive init --docker --up    # starts Postgres 17 + pgvector on 127.0.0.1:54329, creates the schema
resonant-archive models fetch          # the embedding model, about 550 MB, checked against pinned SHA-256 sums
resonant-archive doctor                # check everything
```

Then import something, add vectors, and ask:

```bash
resonant-archive import ~/.chronicle/archive             # the mod's mirror (the importer is detected)
resonant-archive import ~/.claude/projects               # your Claude Code history
resonant-archive embed                                   # meaning-based search; resumable, run it again any time
resonant-archive recall search "the retry bug in the uploader"
```

Search works on words and exact strings as soon as an import finishes. `embed` adds meaning. Until it catches up, the answer footer says how many rows are pending.

**On Windows** (PowerShell; Python 3.11+, and Docker Desktop running for the bundled database, or your own PostgreSQL, see [Platforms](#platforms)):

```powershell
git clone https://github.com/nekyialabs/resonant-archive
cd resonant-archive
py -m pip install -e .                 # or: pipx install .

resonant-archive init --docker --up    # Docker Desktop must be running
resonant-archive models fetch
resonant-archive doctor

resonant-archive import "$env:USERPROFILE\.chronicle\archive"    # the mod's mirror
resonant-archive import "$env:USERPROFILE\.claude\projects"      # your Claude Code history
resonant-archive embed
resonant-archive recall search "the retry bug in the uploader"
```

If `resonant-archive` is not found, the Scripts folder of your Python is not on `PATH` (`pip` prints it); `py -m resonant_archive.cli ...` runs the same program. A leading `~` in a path works too, though PowerShell and cmd do not expand it for you (the tool does).

### Bring your own Postgres

You need PostgreSQL 15+ with the `vector` and `pg_trgm` extensions. Create the extensions once as a superuser, then:

```bash
export RESONANT_ARCHIVE_DSN=postgresql://owner:password@host:5432/resonant_archive
resonant-archive init
```

`init` also saves the address to `~/.config/resonant-archive/config.toml` (mode 0600), so you only export it once. Prefer the environment variable or the config file to `--dsn`: anything on a command line is visible to other users on the machine (`ps`). If an extension is missing, `init` stops and prints the exact `CREATE EXTENSION` line. `doctor --fix` creates missing extensions and applies pending migrations.

`init` also creates a login that can only read (`archive_reader`) and saves its address as `readonly_dsn`; the MCP server and `recall` use it, and `doctor` warns when it is missing. If your owner role cannot create roles, [docs/SCHEMA.md](docs/SCHEMA.md) has the SQL to do it by hand.

### Put the mod and the archive together

In `chronicle.json`:

```json
"archive": { "command": ["resonant-archive", "recall"] }
```

Now `recall` in Claude Code searches the mirror and the archive. The contract between them is in [docs/RECALL-CONTRACT.md](docs/RECALL-CONTRACT.md). To keep the archive current, run `resonant-archive import ~/.chronicle/archive` whenever you like; unchanged sessions are skipped.

## Importers

`resonant-archive importers` lists them. `import` detects which one fits, or you name it with `--importer`.

| Importer | Reads | Default source label |
|---|---|---|
| `chronicle_mirror` | The mod's mirror folder, including compaction summaries and memories | `chronicle` |
| `claude_code` | Claude Code transcripts: a `.jsonl`, a project folder, or all of `~/.claude/projects` | `claude-code` |
| `nexus_markdown` | ChatGPT and claude.ai exports converted to Markdown by the [Nexus AI Chat Importer](https://github.com/Superkikim/nexus-ai-chat-importer) Obsidian plugin | set with `--source` |
| `markdown_folder` | Any folder of `.md` / `.txt` notes, chunked | `notes` |

Raw `conversations.json` exports from ChatGPT and claude.ai are **not** read directly yet. Convert them with Nexus first. Direct importers are planned.

Useful flags: `--source` (the label stored on each row; `--namespace` still works), `--project` (Claude Code: one project only), `--timezone`, `--rebuild` (re-read everything, then remove the rows of re-read files that the input no longer produces; asks first, or `--yes`; `--dry-run` only reports; not with `--project`), `--force`, and `--known-secrets` (files whose values are redacted wherever they appear). Imports are incremental, so running one again is cheap. Importers are plugins: your own can register under the `resonant_archive.importers` entry-point group, see [docs/DESIGN.md](docs/DESIGN.md).

## Connect your AI over MCP

`resonant-archive mcp` is a read-only stdio MCP server. It does not need a daemon running; if the embed server (`resonant-archive serve`) is not up, it loads the model in its own process, and if there is no model it falls back to words and exact strings and says so.

**Claude Code:**

```bash
claude mcp add resonant-archive -- resonant-archive mcp
```

**Claude Desktop**, in `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Linux: `~/.config/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "resonant-archive": {
      "command": "resonant-archive",
      "args": ["mcp"]
    }
  }
}
```

If `resonant-archive` is not on the path Claude Desktop sees, use the full path from `which resonant-archive` (Windows: `where.exe resonant-archive`, written in the JSON with doubled backslashes: `"C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\resonant-archive.exe"`).

| Tool | What it does |
|---|---|
| `search` | Words, exact strings and meaning, rank-fused. Filters: source, speaker, context, dates, mode. |
| `around` | One message and its neighbours in the conversation. |
| `overview` | What happened between two dates: counts, busiest conversations. |
| `associate` | Messages from other conversations close in meaning to one message. |
| `stats` | What the archive holds. |
| `drill` | The messages a chronicle memory was written from. Only appears when the archive holds chronicle memories. |

`archive_search` and `archive_stats` (the v0.1 names) still work as aliases for one release. Replies are plain text, capped near 6,000 characters, and say when they capped.

A skill file for your AI is in [`skills/resonant-archive.md`](skills/resonant-archive.md).

## Command reference

```
resonant-archive init [--docker [--up]] [--dsn URL] [--port N] [--project-dir DIR] [--no-extensions] [--no-save]
resonant-archive doctor [--fix]
resonant-archive models fetch [--model-dir DIR] [--force]
resonant-archive import PATH [--importer NAME] [--source LABEL] [--project P] [--timezone TZ]
                             [--known-secrets PATH] [--force] [--rebuild [--yes | --dry-run]]
resonant-archive embed [--limit N] [--dry-run] [--index-only] [--no-fallback]
resonant-archive serve [--port N] [--threads N] [--model-dir DIR]
resonant-archive recall search|around|overview|associate|drill ...  [--json]
resonant-archive stats [--json]
resonant-archive mcp
resonant-archive importers
resonant-archive migrate-v01
```

`resonant-archive COMMAND --help` has every flag. The database address comes from `RESONANT_ARCHIVE_DSN` (`RESONANT_ARCHIVE_READONLY_DSN` for the read-only role) or `~/.config/resonant-archive/config.toml`, never from a file in the repo.

## Privacy

- **Local only.** Your messages go to Postgres on your machine and nowhere else. No telemetry, no accounts, no API keys.
- **Loopback.** The bundled Postgres is published on `127.0.0.1` only. The embed server refuses to bind anything but loopback and makes no outbound connections.
- **No egress.** The only network use is the one-time model download from Hugging Face, checked against pinned hashes. After that it works offline.
- **Redaction.** Credentials are stripped before anything is stored, by the mod when it writes the mirror and again by the importers. It is pattern-based and errs toward removing too much: a leaked key can only be rotated, an over-redacted word costs a few characters. It is a safety net, not a guarantee.
- **Keep it private.** The mirror and the archive hold your conversations. Don't commit them to a public repository.

Details in [docs/PRIVACY.md](docs/PRIVACY.md).

## Upgrading from v0.1

v0.1 kept vectors in a Chroma folder with a different model, so they can't be carried over. Your source files can: import them again, then embed. The old folder is never read or deleted by this tool. Steps in [docs/MIGRATING-FROM-0.1.md](docs/MIGRATING-FROM-0.1.md), or run `resonant-archive migrate-v01`.

## Platforms

| | Linux, macOS | Windows 11 (10 should work, untested) |
|---|---|---|
| **The chronicle mod** | Yes. Needs `sh`, `mkdir`, `cat` and [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on the path. | Yes, with nothing extra installed: the mirror is appended through `powershell.exe -EncodedCommand`, and the archive is listed, searched and read through the engine's own file API. No `sh`, `rg` or `cat`. |
| **The CLI, importers, MCP server, embed server** | Yes. Python 3.11+. | Yes, natively, Python 3.11+ (PowerShell or cmd; no WSL needed). |
| **PostgreSQL + pgvector** | The bundled `docker-compose.yml` (`init --docker --up`), or your own PostgreSQL 15+. | Docker Desktop and `init --docker --up`, or your own native PostgreSQL 15+ with `vector` and `pg_trgm` (see below). |
| **`contrib/systemd`** | Yes (Linux, systemd). | Linux only. On Windows run `resonant-archive serve` in a terminal, or do nothing: the MCP server loads the model in its own process when no embed server is up. |

**Windows, the details**

- **Where things live.** Settings in `%LOCALAPPDATA%\resonant-archive\config\config.toml`; the model, and the `docker-compose.yml` and `.env` that `init --docker` writes, in `%LOCALAPPDATA%\resonant-archive`; the mod's mirror in `%USERPROFILE%\.chronicle\archive`; Claude Code's transcripts in `%USERPROFILE%\.claude\projects` (one folder per working directory, named with the drive and separators turned into dashes, such as `C--Users-you-project`). An absolute `XDG_CONFIG_HOME` / `XDG_DATA_HOME` still wins if you set one. A config or docker folder an earlier version put under `~\.config` or `~\.local\share` is still used until the new place has one.
- **Private files.** Windows has no file mode bits. The config file and the `.env` (both hold a database password) are made readable by your account alone with `icacls` (inheritance removed, your user granted full control), where Linux uses mode 0600. The mod's mirror is not locked down further: it sits in your profile folder, which is private to your account already.
- **Your own PostgreSQL.** Install PostgreSQL 15 or newer for Windows (`pg_trgm` comes with it). Most Windows installers do not include pgvector: build it as the [pgvector README](https://github.com/pgvector/pgvector#windows) describes, or use a distribution that bundles it, then `CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;` in the archive's database as a superuser and set `RESONANT_ARCHIVE_DSN` (`$env:RESONANT_ARCHIVE_DSN = "postgresql://..."`).
- **A database that is down** is reported after 10 seconds, not minutes (libpq on Windows waits a long time for a refused connection otherwise). Put `connect_timeout=N` in the DSN, or set `PGCONNECT_TIMEOUT`, to change that.
- **Time zone.** Windows has no `TZ` or `/etc/timezone`. Without `RESONANT_ARCHIVE_TZ` (or `TZ`) set to an IANA name such as `Europe/London`, `recall` works out "today" and date ranges in UTC. Importers that read a timestamp with no zone use the machine's own zone, as everywhere.
- **Files written by Windows tools.** A UTF-8 byte-order mark (Notepad), UTF-16 (PowerShell 5.1's `>` and `Out-File`) and CRLF line endings are read correctly in Markdown, JSON-lines and `--known-secrets` files.
- **Output encoding.** Output to a pipe or a file is UTF-8 whatever the console's code page is.
- **Long paths.** Python on Windows is limited to 260-character paths unless long paths are switched on (Windows setting `LongPathsEnabled`). The defaults are well under it; a deeply nested `--project-dir` or Markdown folder may not be.
- **Symlinks.** Importers skip symbolic links and directory junctions that lead outside the folder you named, as on Linux.
- **What has and has not been tested on Windows.** The Python test suite that needs no database runs on Windows 11 with Python 3.13, and in CI on `windows-latest` with Python 3.12. The MCP server's stdio handshake and tool list were run by hand on Windows. The tests that need PostgreSQL run on Linux CI only; no maintainer has run `init`, an import or a search against a real database on Windows, so those paths are expected to work (they use the same driver everywhere) but are not verified there. The mod's test suite runs on Windows and on Linux.

## Caveats

- **Windows needs the details above.** Everything runs there, but the database-backed paths are not covered by tests on Windows (see Platforms).
- **Hooks are early access.** The mod depends on Claude Code features that may change between releases. The mod README records the Claude Code version it was tested against.
- **Postgres is required** for the archive. That's a change from v0.1.
- **Memory formation is off by default.** The optional memories feature spends model calls in the background. See [docs/COMPACTION.md](docs/COMPACTION.md).
- **Direct ChatGPT and claude.ai export importers are not in yet.** Use Nexus + `nexus_markdown`.
- **First embedding is slow on a CPU** for a large archive. It is resumable and incremental, so let it run in the background. We haven't published timings yet and won't guess.

## Troubleshooting

- `resonant-archive doctor` first. It checks the database, the extensions, the model and the embed server, and prints the fix for what it finds.
- *"missing extensions"*: run the `CREATE EXTENSION` line it prints, as a superuser, in the archive's database.
- *Search finds words but not meaning*: rows are still pending. Run `resonant-archive embed`.
- *"no embedder"*: run `resonant-archive models fetch`.
- *Port 54329 is taken*: `resonant-archive init --docker --up --port 54330`.

## Contributing, security, conduct

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [CHANGELOG.md](CHANGELOG.md)

## License

[Apache License 2.0](LICENSE). Versions before 2.0.0 were released under the Codependent AI Source-Available License.

## Credits

The embedding model is [nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) (Apache-2.0), run through ONNX Runtime. Postgres and [pgvector](https://github.com/pgvector/pgvector) do the storing. The [Nexus AI Chat Importer](https://github.com/Superkikim/nexus-ai-chat-importer) by Superkikim is the recommended way to turn ChatGPT and claude.ai exports into Markdown.

Built by [Nekyia Labs](https://nekyialabs.com).
