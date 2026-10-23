# Changelog

All notable changes to `resonant-archive` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The chronicle mod runs on Windows.** The five places that shelled out to `sh`, `cat` and ripgrep now have a Windows path: the mirror is appended through `powershell.exe -EncodedCommand` (a byte copy of stdin, no shell quoting), the zone marker is written with `$.fs`, and the archive is listed, searched and read through `$.fs` (fixed strings, case-insensitive, the same semantics as the ripgrep call; capped at 4 MiB like rg's stdout was). Linux and macOS are unchanged. The ripgrep probe is skipped on Windows. The test harness treats a POSIX path and its Windows spelling as one file, takes an `env` option, and `tests/windows.test.ts` covers the append, the zone marker and recall on a Windows host.

## [2.0.1] - 2026-10-17

### Fixed

- **The quotes around a `sh -c` body are trimmed in linear time.** The secret-source check trimmed them with a regex ending in `$`, which was quadratic on a long run of quotes or spaces inside the body: 13 s at 50,000 characters in the Python importer, 40 s at 200,000 in the chronicle mod. A tool call can carry any text, so one hostile command could stall an import or the mod. Both now use a plain string trim with the same result; a 500 KB body takes well under a second.
- **The chronicle mod reads a prompt typed mid-turn in linear time.** Taking the engine's wrapper off it used two regexes that were quadratic on a long run of spaces or newlines (2 s and 4 s at 50,000). They are replaced by string scans with the same result.

## [2.0.0] - 2026-10-16

A rewrite. The story: compaction made more bearable, with an archive as the safety net.

### Added

- **The chronicle mod** (`claude-code/chronicle/`), a Claude Code mod:
  - split compaction: the older part is summarised by your AI in its own voice (`compaction.voice`, `compaction.steer`), keeping exact identifiers, decisions and open threads; the recent stretch stays word for word
  - `/squish [focus]`, `/squish status`, `/squish auto on|off`
  - a context meter in your prompts, flagging at 75% and 90%
  - a redacted, append-only mirror of every row on disk, and a `recall` tool over it
  - optional background memories (off by default; they spend model calls)
- **Postgres + pgvector archive**: exact word search (tsvector, pg_trgm), vector search, exact-string anchors, rank-fused.
- **Local embeddings**: `nomic-embed-text-v1.5`, fp32 ONNX, pinned revision and SHA-256 sums, loopback-only server, no egress after the download.
- **Importers**: `claude_code`, `chronicle_mirror`, `nexus_markdown`, `markdown_folder`, and a plugin entry-point group (`resonant_archive.importers`). Incremental by default.
- **Redaction** of credentials before anything is stored, plus `--known-secrets` and `--json-config`.
- **MCP tools**: `search`, `around`, `overview`, `associate`, `stats`, and `drill` (only when chronicle memories exist).
- **CLI**: `init` (`--docker`, `--up`, `--dsn`), `doctor` (`--fix`), `models fetch`, `import`, `importers`, `embed`, `recall`, `migrate-v01`.
- Docs: `COMPACTION`, `PRIVACY`, `EMBEDDER`, `DESIGN`, `MIGRATING-FROM-0.1`, `SCHEMA`, `MIRROR-FORMAT`, `RECALL-CONTRACT`.

### Changed (breaking)

- **Needs Postgres 15+** with `vector` and `pg_trgm`. `resonant-archive init --docker --up` brings one up.
- Chroma and MiniLM are gone. Vectors are 768-dimension nomic embeddings; v0.1 indexes can't be converted, so re-import and re-embed (see `docs/MIGRATING-FROM-0.1.md`).
- `resonant-archive serve` now runs the local embed server, not a search API. The MCP server needs no daemon.
- Python 3.11 or newer (the 3.12 cap is gone; no torch).
- Namespaces are the `source` label. `--namespace` still works as an alias of `--source`.
- `archive_search` and `archive_stats` remain as aliases for one release, then they go.
- Linux and macOS. Windows is not supported yet.
- Brand and links: Nekyia Labs.

### Removed

- The FastAPI daemon, the Chroma store, `sentence-transformers`/torch, the double-click launcher scripts, and the v0.1 guides (`START_HERE`, `SETUP_GUIDE`, `USAGE_GUIDE`, `QUICK_REFERENCE`, `TROUBLESHOOTING`, `CLOUD_IMPORT`). Their content now lives in the README and `docs/`.

### Known gaps

- No direct importers for raw ChatGPT and claude.ai `conversations.json` exports yet. Use the Nexus AI Chat Importer and `nexus_markdown`.
- The mod depends on Claude Code hooks, an early-access feature.
- Embedding timings on a CPU have not been measured and published.
- Licence: Apache-2.0.

### Security

- **Redaction covers far more shapes**: Stripe, npm, Hugging Face, GitLab, SendGrid, AWS session keys and secret-key assignments, Azure `AccountKey`, `Authorization: Token|Basic|Bearer`, `curl -u`, `sshpass -p`, `mysql -p`, any `scheme://user:password@host`, Slack and Discord webhooks, Discord bot tokens, Twilio, PGP key blocks, `name: value` assignments in any case, "password is X" prose, and long mixed-case tokens. The chronicle mod's copy of the patterns is kept equivalent.
- **Private-key redaction is linear** (a 500 KB input of `BEGIN` lines took over a minute; it is now instant).
- **`init` creates a SELECT-only `archive_reader` login** and saves `readonly_dsn`; the MCP server and `recall` use it; `doctor` warns when it is unset or is the owner's login.
- `init --docker` keeps `.env` in the data folder (not the current directory), replaces a placeholder or short password, and tightens the mode of a kept file to 0600.
- The embedder verifies all four model files before loading, with a cached marker keyed on modification time and size.
- Importers redact every stored string (not only the text), redact before cutting, withhold the results of secret-bearing tool calls, skip symlinks, files outside the root and oversize files and lines, and survive deeply nested JSON.
- The embed server refuses non-loopback `Host` headers and non-JSON POSTs, and times out stalled clients.
- `import --rebuild` removes only rows written by the same importer under the same label (migration 0002 adds `importer`).
- **`import --rebuild` is narrower still** (migration 0003 adds `unit_key`): it removes only rows that belong to a unit it re-read in that run and that the re-read no longer produces. Units it did not read (a project filter, a skipped symlink, an oversize or purged file) are never touched; `--rebuild` with `--project` is refused; any skipped file, failed unit or lost line stops the removal. It prints the count and removes only with `--yes` (or an answer at the prompt); `--dry-run` only reports.
- **A transcript cannot overwrite another source's row by reusing its id**: an upsert updates a row only when the stored `source` and `importer` match; a clash is skipped and counted.
- **A tool call that reads a secret-bearing source keeps only the tool name and the file (or program) in its stored input**, never `content`, `old_string`, `new_string`, an `echo ... >> .env` value or a heredoc body (Python importer and the chronicle mod). The list of secret files and commands is wider (`.env*`, `.npmrc`, `.netrc`, `.git-credentials`, `.docker/config.json`, `.kube/config`, `secrets.*`, key stores, `terraform.tfstate*`, `/proc/*/environ`, `id_*`; `exec env`, `VAR=x env`, `bash -c env`, `docker compose config`, `systemctl show`, `ssh-add -L`, ...), and a `Grep`/`Glob` whose pattern or path names a secret has its result withheld. Best effort, said so in the docs.
- Redaction: `--db-pass=` and any `--*-password` flag, `~/.pgpass` lines, short values (4+ characters) after an explicit `password` / `passwd` / `pwd` key; the long-token rule now starts at 40 characters (fewer false positives in code). `redact_dsn` hides `sslpassword=`.
- The private-strip lint exits 2 when the terms file yields no rules or holds an empty rule, and prints only `path:line` when `CI=true`.
- `doctor` warns when the read-only role can `CREATE` in schema `public`; [docs/SCHEMA.md](docs/SCHEMA.md) gives the `REVOKE` and explains that `CONNECT` is cluster-wide.
- CI: every job has a time limit, the wheel check lists all migrations, `.gitleaks.toml` allowlists the synthetic fixtures by file name.
- CI actions, the compose image and the Claude Code npm release are pinned. The private-strip lint no longer carries a denylist: private terms are read from `PRIVATE_STRIP_TERMS_FILE`.

## [0.1.0] - 2026-04-09

Initial public release. Modernized from an earlier internal tool (`vault-archive-product`) that was built, shelved during a cloud migration, and revived for this release.

### Added

- **Python 3.11/3.12 package** with `pyproject.toml`, `setuptools` backend, and a `resonant-archive` console script entry point
- **`resonant-archive import`** — chunks and embeds a directory of markdown or text files into a local semantic archive, tagged with a user-supplied namespace
- **`resonant-archive search`** — command-line semantic search with optional namespace filtering and rich table output
- **`resonant-archive stats`** — per-namespace chunk counts and archive location
- **`resonant-archive serve`** — FastAPI daemon that keeps the embedding model warm for fast MCP queries
- **`resonant-archive mcp`** — stdio MCP server (for Claude Desktop) forwarding tool calls to the daemon over HTTP
- **Frontmatter-aware chunker** with two strategies:
  - `paragraph` (default) — header-aware greedy packing with tail merging
  - `window` — fixed-size character windows with overlap and no-gap guarantees
- **Tiered metadata model** — every chunk carries stable `chunk_id`, `source_file`, `chunk_index`, and `char_offset`; plus optional `title`, `timestamp`, `tags`, and Nexus-style `conversation_id` / `provider` when extractable from frontmatter
- **Namespace tagging** — every chunk is stored with a namespace metadata field; results surface the namespace so querying AIs know where a hit came from
- **MCP tools** — `archive_search(query, namespace?, n_results?)` and `archive_stats()`, both exposed via stdio MCP server
- **Skill file** (`skills/resonant-archive.md`) — drops into any MCP-compatible AI's context to enable natural-language operation
- **Comprehensive docs** — `README`, `START_HERE`, `SETUP_GUIDE`, `USAGE_GUIDE`, `QUICK_REFERENCE`, `TROUBLESHOOTING`, `CLOUD_IMPORT`
- **Cross-platform launcher scripts** — `START_WINDOWS.bat` and `START_MAC_LINUX.sh` for double-click daemon startup
- **30 unit tests** covering the chunker (19) and store (11), including semantic similarity verification and namespace isolation

### Technical choices

- **Embedding model:** `sentence-transformers/all-MiniLM-L6-v2` (384-dim, ~90 MB, fast). Alternatives documented in README.
- **Vector store:** `chromadb.PersistentClient` — embedded, file-based, single process, no separate server
- **MCP framework:** Official Python `mcp` SDK
- **CLI framework:** `typer` with `rich` for progress bars and tables
- **Frontmatter parser:** `python-frontmatter` (handles YAML frontmatter with graceful fallback)

### Licensed under

[Apache License 2.0](LICENSE). Earlier releases (0.1.x) were under the Codependent AI Source-Available License; 2.0.0 and later are Apache-2.0.

[Unreleased]: https://github.com/nekyialabs/resonant-archive/compare/v2.0.0...HEAD
[2.0.1]: https://github.com/nekyialabs/resonant-archive/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/nekyialabs/resonant-archive/compare/v0.1.0...v2.0.0
[0.1.0]: https://github.com/nekyialabs/resonant-archive/releases/tag/v0.1.0
