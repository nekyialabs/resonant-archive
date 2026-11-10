# Database schema

The contract other code is written against. The SQL is `resonant_archive/migrations/0001_init.sql`; if this page and
the SQL disagree, the SQL is right and this page has a bug. Migrations are numbered files, applied in order by
`resonant-archive init` (and `doctor --fix`), each in its own transaction, recorded in `schema_migrations` with a
checksum. An applied migration is never edited: add a new numbered file.

Requires PostgreSQL 15 or newer with the `vector` (pgvector) and `pg_trgm` extensions.

## Tables

### `messages`: one row per message

| column | type | notes |
|---|---|---|
| `id` | text PK | Deterministic, chosen by the importer (`<prefix>:<stable parts>`). Same input gives the same id. |
| `source` | text | Free label: `chatgpt`, `claude-ai`, `claude-code`, `chronicle`, `notes`, ... Replaces v0.1 "namespace". List them with `SELECT source FROM conversations GROUP BY 1`. |
| `conv_id` | text | Conversation id, unique within `source`. |
| `conv_title` | text | May be null. |
| `ts` | timestamptz | UTC. |
| `speaker` | text | `user` `agent` `assistant` `peer` `system` `tool` (CHECK). Importers in this package emit `user` for the person and `agent` for the AI. `peer` is another agent in the conversation. `assistant` is accepted for importers whose input has no notion of the AI beyond "assistant". |
| `context` | text | Free tag, default `default`. This package uses `default`, `system`, `peer`, `notes`. |
| `text` | text | Redacted, at most 20,000 characters (2,000 for `tool`). |
| `platform` | text | `cli`, `desktop`, `chatgpt`, `claude.ai`, ... or null. |
| `model` | text | Model name if the source had one. |
| `hidden` | boolean | Tool calls and results, harness input, compaction summaries: stored, left out of default search. |
| `tags` | text[] | e.g. `thinking`, `tool-call`, `tool-result`, `summary`, `mid-turn`, `document`. |
| `src` | text | Where it came from inside the source (file name). |
| `seq` | integer | 0-based position in the conversation, ordered by `ts` then the importer's order. |
| `tsv` | tsvector | `GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED`. Never write it. |
| `embedding` | vector(768) | nomic-embed-text-v1.5. Null until embedded. Cosine distance: `embedding <=> $query`. |
| `embed_status` | text | `pending` `done` `skipped` `failed` (CHECK), default `pending`. |
| `embedded_hash` | text | sha1 of the text that was embedded. Written by the embedder. |
| `embedded_at` | timestamptz | Written by the embedder. |
| `imported_at` | timestamptz | Last time an import changed the row. |
| `importer` | text | Name of the importer that wrote the row (migration 0002). A re-import updates a row only when its `source` and `importer` match the stored row (an id that belongs to another source or importer is skipped and counted). Null on rows imported before 0002. |
| `unit_key` | text | The unit of the input (file, session, conversation) that wrote the row (migration 0003). `import --rebuild` removes a row only if its importer and `unit_key` belong to a unit that run re-read and the re-read no longer produces it. Null on rows imported before 0003; a rebuild never removes those (`import --force` fills the column in). |

Every string column is redacted before it is stored, not only `text`: titles, labels, file names, model names, tags. An
id or `conv_id` that contains a secret is replaced by `redacted-<hash of the original>`, so it stays unique and stable.

Indexes: GIN on `tsv`; GIN trigram on `text`; HNSW on `embedding` (`vector_cosine_ops`); `(source, ts)`;
`(conv_id, seq)`; `(ts)`; `(speaker)`; partial `(id) WHERE embed_status = 'pending'` (the embedder's work queue).

**Embedding contract (importer to embedder).**
- A new visible row is inserted `pending`; a hidden row is inserted `skipped` (tool noise is not embedded). The embedder
  may embed `skipped` rows if it wants to; it must not rely on them being `pending`.
- To find work: `SELECT id, text FROM messages WHERE embed_status = 'pending' ORDER BY id LIMIT n`.
- After embedding set `embedding`, `embed_status = 'done'`, `embedded_hash = sha1(text)`, `embedded_at = now()`.
  On a permanent failure set `failed`.
- Re-importing a row whose `text` is unchanged keeps `embedding`, `embedded_hash`, `embedded_at` and `done`. If the
  `text` changed, all of those are cleared and the row goes back to `pending` (or `skipped` when hidden).

### `conversations`: one row per conversation

`PK (source, conv_id)`; `title`, `platform`, `first_ts`, `last_ts`, `n_messages`, `n_visible`, `updated_at`. Kept up to
date by the importers.

### `anchors`: exact-recall strings

`(token, kind, msg_id, ts)`, `PK (msg_id, token, kind)`, `msg_id` references `messages` with `ON DELETE CASCADE`.
Kinds: `url path sha pr snowflake handle quote version ident`. Only visible messages produce anchors, and nothing
secret-shaped is anchored. Lookup: `WHERE token = $1` (index `anchors_token_kind`).

### `memories`: chronicle L1 memories

`id` PK (`memory:<sid>:<ordinal>:<source_hash>`), `source`, `sid` (equals `messages.conv_id` of the session), `level`,
`ordinal`, `ts` (when written), `t_from`, `t_to`, `from_uuid`, `to_uuid`, `text`, `tsv` (generated, as above),
`est_tokens`, `rows`, `source_hash`, `imported_at`, `importer` and `unit_key` (as in `messages`). Empty unless the chronicle mod's mirror was imported. Optional tools
(`drill`, memory listing in `overview`) appear only when this table has rows.

### `sources`: what has been imported

`PK (importer, unit_key)`; `label`, `content_hash`, `n_files`, `n_messages`, `n_memories`, `path`, `imported_at`. One row
per input unit (a file, a session, a conversation). `content_hash` covers the unit's file bytes, the importer's version, the redaction version
and the options; an unchanged unit is skipped on the next import.

### `schema_migrations`

`(version, name, checksum, applied_at)`.

## Reading

- Search uses the `simple` text-search configuration (language neutral): `tsv @@ plainto_tsquery('simple', $q)`.
- The MCP server and search should connect with `db.connect(readonly=True)`: it sets
  `default_transaction_read_only=on` and a 10 s `statement_timeout`, and prefers a read-only DSN when one is configured.
- Always pass values as parameters. Nothing in this package builds SQL from message text or user input.

## Where the database address comes from

In order: an explicit argument, `RESONANT_ARCHIVE_DSN` (`RESONANT_ARCHIVE_READONLY_DSN` for the read-only role), then
`~/.config/resonant-archive/config.toml` (`$XDG_CONFIG_HOME` is honoured; on Windows `%LOCALAPPDATA%/resonant-archive/config/config.toml`), written with mode 0600 (an owner-only ACL on Windows):

```toml
[database]
dsn = "postgresql://owner:password@127.0.0.1:54329/resonant_archive"
readonly_dsn = "postgresql://reader:password@127.0.0.1:54329/resonant_archive"
```

Never put a DSN in a file inside the repository, and keep it off the command line: an argument is visible to other users
on the machine (`ps`). Use the environment variable or the config file; `init` writes the config file for you.

## Easy path: the bundled Postgres

`resonant-archive init --docker --up` writes `.env` (random password, mode 0600), starts `docker-compose.yml`, saves the
DSN to the config file, runs the migrations and creates the read-only login (without `--up` it writes `.env` and the
DSN and prints the rest). The files go in `--project-dir`, by default `~/.local/share/resonant-archive/docker`, never the
current directory. An existing `.env` is kept (and made mode 0600) unless its password is a placeholder such as the one
in `.env.example`, or shorter than 16 characters: then it gets a new random password. The container is
`pgvector/pgvector:0.8.7-pg17`, pinned by digest in the compose file, published on `127.0.0.1:54329` only (loopback),
data in the named volume `resonant-archive_resonant_archive_pgdata`. By hand:

```
cp .env.example .env        # replace POSTGRES_PASSWORD with a long random string
docker compose up -d --wait
export RESONANT_ARCHIVE_DSN=postgresql://resonant:<password>@127.0.0.1:54329/resonant_archive
resonant-archive init
```

## Bring your own Postgres

1. PostgreSQL 15+ with the pgvector and contrib (`pg_trgm`) packages installed on the server (for example
   `postgresql-17-pgvector` on Debian/Ubuntu, `pgvector` in Homebrew).
2. Create a database and an owner role:
   ```sql
   CREATE ROLE archive_owner LOGIN PASSWORD '...';
   CREATE DATABASE resonant_archive OWNER archive_owner;
   ```
3. Create the extensions once, as a superuser, inside that database (a non-superuser owner often cannot):
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   CREATE EXTENSION IF NOT EXISTS pg_trgm;
   ```
4. `RESONANT_ARCHIVE_DSN=postgresql://archive_owner:...@host/resonant_archive resonant-archive init` applies the schema.
   If an extension is missing, `init` stops and `resonant-archive doctor` prints the exact `CREATE EXTENSION` line.
5. The read-only role for the MCP server and search. `init` creates it (`archive_reader`, or `archive_reader_<database>`
   when the database is not the default one, because a role name is shared by the whole server) when the owner may
   create roles: SELECT on every table in `public` and on future ones, `default_transaction_read_only = on` on the role,
   a random password sent to the server already hashed (SCRAM), and the address saved as `readonly_dsn`. If the owner
   cannot create roles, `init` says so and you do it by hand:
   ```sql
   CREATE ROLE archive_reader LOGIN PASSWORD '...';
   GRANT CONNECT ON DATABASE resonant_archive TO archive_reader;
   GRANT USAGE ON SCHEMA public TO archive_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO archive_reader;
   ALTER DEFAULT PRIVILEGES FOR ROLE archive_owner IN SCHEMA public GRANT SELECT ON TABLES TO archive_reader;
   ```
   Put its DSN in `RESONANT_ARCHIVE_READONLY_DSN` or `readonly_dsn` in the config file. `doctor` warns when
   `readonly_dsn` is unset, is the owner's login, logs in as a role that can write, or can `CREATE` in schema `public`.

   Two things about a role are wider than they look, because Postgres roles belong to the whole server:
   - **`CREATE` in `public`.** Before Postgres 15 every role may create objects in `public` (the grant sits on the
     pseudo-role `PUBLIC`), so the reader could create a table or function that shadows the archive's. `doctor` checks
     `has_schema_privilege(reader, 'public', 'CREATE')` and warns. As the owner or a superuser, in the archive's
     database: `REVOKE CREATE ON SCHEMA public FROM PUBLIC;`
   - **`CONNECT` is cluster-wide.** A new database grants `CONNECT` to `PUBLIC`, so the reader role can open a session in
     every other database on the same server (it still sees nothing without `SELECT` there, but it can look). Revoke it
     where that matters: `REVOKE CONNECT ON DATABASE other_db FROM PUBLIC;`, then grant it back to the roles that need
     it. `init` only grants `CONNECT` on the archive's own database. Keeping the archive in its own Postgres
     (the bundled compose database) avoids the question.

## Functions other code can call

`resonant_archive.db`: `connect(dsn, readonly=)`, `init`, `migrate`, `doctor(dsn, fix=, extra_checks=)` (other parts add
their own `Check`s), `failed(checks)`. `resonant_archive.commands`: `init`, `init_docker`, `doctor`, `list_importers`,
`detect_importer`, `import_path`, `stats`.
