"""resonant-archive command line.

    init             prepare the database (your own Postgres with --dsn, or the bundled one with --docker)
    doctor           check the database, the embedding model and the embed server
    import PATH      import conversations or notes (the importer is detected, or named with --importer)
    importers        list the importers
    models fetch     download the embedding model (pinned revision, pinned SHA-256)
    serve            run the local embed server (loopback only)
    embed            compute vectors for rows that lack one
    recall ...       search the archive (the contract in docs/RECALL-CONTRACT.md; --json for programs)
    stats            counts by source and speaker
    mcp              run the MCP server on stdio
    migrate-v01      how to move a v0.1 (Chroma) archive over

Commands print plain text for a person. Message text is never written to a log.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

import typer

from . import __version__, commands, config, db, importers, runtime

app = typer.Typer(
    name="resonant-archive",
    help="Search your AI conversations and notes, locally: Postgres, full text, vectors and exact anchors.",
    add_completion=False,
    no_args_is_help=True,
)
models_app = typer.Typer(help="The local embedding model.", no_args_is_help=True)
app.add_typer(models_app, name="models")

DsnOpt = typer.Option(None, "--dsn", help="Database address. Default: RESONANT_ARCHIVE_DSN or the config file (prefer those: an argument is visible in `ps`).")


def _fail(message: str, code: int = 1) -> "typer.Exit":
    typer.echo(f"resonant-archive: {message}", err=True)
    return typer.Exit(code)


def _warn_argv_dsn(dsn: Optional[str]) -> None:
    """A password given with --dsn sits in the process list (`ps`) for as long as the command runs."""
    if dsn and config.redact_dsn(dsn) != dsn:
        typer.echo("resonant-archive: note: a password on the command line is visible to other users of this machine "
                   "(ps). Prefer RESONANT_ARCHIVE_DSN or the config file.", err=True)


def _guard(fn, *args, **kwargs):
    """Run ``fn``; turn the errors that are written for people into a one-line message and exit code 1."""
    try:
        return fn(*args, **kwargs)
    except (config.ConfigError, db.DatabaseError) as exc:
        raise _fail(str(exc)) from None


def _version(value: bool) -> None:
    if value:
        typer.echo(f"resonant-archive {__version__}")
        raise typer.Exit()


@app.callback()
def root(version: Optional[bool] = typer.Option(None, "--version", "-V", callback=_version, is_eager=True,
                                                help="Show the version and exit.")) -> None:
    """Global options."""


# ---------------------------------------------------------------- init

@app.command("init")
def cmd_init(
    dsn: Optional[str] = DsnOpt,
    docker: bool = typer.Option(False, "--docker", help="Use the bundled Postgres: write .env with a random password "
                                                         "and print the compose steps."),
    up: bool = typer.Option(False, "--up", help="With --docker: also start the container and create the schema now."),
    project_dir: Optional[Path] = typer.Option(None, "--project-dir", help="Folder for docker-compose.yml and .env "
                                               "(default: the data folder, ~/.local/share/resonant-archive/docker). "
                                               "If it has no compose file, the bundled one is written there."),
    port: int = typer.Option(config.DEFAULT_PORT, "--port", help="Host port for the bundled Postgres (loopback only)."),
    no_extensions: bool = typer.Option(False, "--no-extensions", help="Do not try CREATE EXTENSION (you did it yourself)."),
    save: bool = typer.Option(True, "--save/--no-save", help="Save --dsn, and the read-only address, to the config file (mode 0600)."),
) -> None:
    """Prepare the database: create the extensions and apply the migrations. Safe to run again."""
    _warn_argv_dsn(dsn)
    if docker:
        _init_docker(project_dir or commands.default_project_dir(), port, up)
        return
    target = dsn or None
    save_reader = save or not dsn  # --no-save with --dsn: the config file stays untouched, so no role whose password would be lost
    result = _guard(commands.init, target, create_extensions=not no_extensions, save_readonly=save_reader)
    if result.missing_extensions:
        typer.echo("missing extensions: " + ", ".join(result.missing_extensions), err=True)
        for name in result.missing_extensions:
            typer.echo(f"  as a superuser in this database: {db.create_extension_sql(name)}", err=True)
        typer.echo("install pgvector (and the postgresql contrib package) on the server, then run init again.", err=True)
        raise typer.Exit(1)
    typer.echo(f"database ready: schema version {result.version:04d}"
               + (f" (applied {', '.join(f'{v:04d}' for v in result.applied)})" if result.applied else " (already current)"))
    if dsn and save:
        path = config.write_dsn(dsn)
        typer.echo(f"saved the database address to {path}")
    _echo_reader(result)
    typer.echo("next: resonant-archive models fetch   then   resonant-archive import <path>")


def _echo_reader(result: "db.InitResult") -> None:
    r = result.reader
    if r is None:
        typer.echo("no read-only role made (--no-save): the MCP server and recall will use the owner's address. "
                   "Run `resonant-archive init` without --no-save to create one.", err=True)
    elif r.dsn:
        typer.echo(r.note + (" and its address is saved as readonly_dsn" if r.created or r.rotated else ""))
    else:
        typer.echo(r.note, err=True)


def _init_docker(project_dir: Path, port: int, up: bool) -> None:
    project_dir = _guard(commands.prepare_project_dir, project_dir)
    if _guard(commands.ensure_compose_files, project_dir):
        typer.echo(f"no docker-compose.yml in {project_dir}; wrote the bundled one there (and docker/init-extensions.sql)")
    if up:
        result = _guard(commands.init_docker, project_dir, port=port)
        typer.echo(f"database ready: schema version {result.version:04d}; address saved to {config.config_file()}")
        _echo_reader(result)
        typer.echo("next: resonant-archive models fetch   then   resonant-archive import <path>")
        return
    state = commands.env_file_state(project_dir)
    env, password = _guard(commands.write_env_file, project_dir, port)
    if state == "weak":
        typer.echo(f"the password in {env} was a placeholder or too short; replaced it with a random one. If you already "
                   "started the container with the old one, run `docker compose down -v` once (it holds no data yet).",
                   err=True)
    path = _guard(config.write_dsn, commands.docker_dsn(password, port))
    typer.echo(f"wrote {env} (mode 0600, random database password; keep it out of version control)")
    typer.echo(f"saved the database address to {path}")
    typer.echo("")
    typer.echo("now run:")
    typer.echo(f"  cd {project_dir}")
    typer.echo("  docker compose up -d --wait        # Postgres 17 + pgvector, bound to 127.0.0.1 only")
    typer.echo("  resonant-archive init              # creates the schema and the read-only login")
    typer.echo("  resonant-archive models fetch      # downloads the embedding model (about 550 MB, once)")
    typer.echo("  resonant-archive doctor            # checks it all")


# ---------------------------------------------------------------- doctor

@app.command("doctor")
def cmd_doctor(
    dsn: Optional[str] = DsnOpt,
    fix: bool = typer.Option(False, "--fix", help="Create missing extensions and apply pending migrations."),
) -> None:
    """Check the database, the extensions, the embedding model and the embed server."""
    _warn_argv_dsn(dsn)
    checks = commands.doctor(dsn, fix=fix, extra_checks=(runtime.embedder_checks,))
    for c in checks:
        mark = "ok  " if c.ok else ("info" if c.severity == "info" else ("warn" if c.severity == "warn" else "FAIL"))
        typer.echo(f"[{mark}] {c.name}: {c.detail}")
        if not c.ok and c.fix:
            typer.echo(f"        fix: {c.fix}")
    bad = db.failed(checks)
    if bad:
        typer.echo(f"{len(bad)} problem(s) found.")
        raise typer.Exit(1)
    typer.echo("all good.")


# ---------------------------------------------------------------- import

@app.command("import")
def cmd_import(
    path: Path = typer.Argument(..., help="A folder or file to import."),
    importer: str = typer.Option("auto", "--importer", "-i", help="Importer name (see `importers`); auto detects."),
    source: Optional[str] = typer.Option(None, "--source", "--namespace", "-n",
                                         help="Label stored on the rows (default: the importer's)."),
    timezone: Optional[str] = typer.Option(None, "--timezone", help="Zone for timestamps that carry none (IANA name)."),
    date_order: str = typer.Option("dmy", "--date-order", help="dmy or mdy, for claude.ai headings in Obsidian notes."),
    project: Optional[List[str]] = typer.Option(None, "--project", help="claude-code: only this project folder (repeatable)."),
    known_secrets: Optional[List[Path]] = typer.Option(
        None, "--known-secrets", help="File or folder whose values are redacted wherever they appear (repeatable)."),
    json_config: Optional[List[str]] = typer.Option(None, "--json-config", help="JSON config file whose string values are redacted."),
    force: bool = typer.Option(False, "--force", help="Re-import units even if unchanged."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Re-import everything, then remove the rows of re-read files "
                                 "that the input no longer produces (asks first; see --yes, --dry-run)."),
    yes: bool = typer.Option(False, "--yes", help="With --rebuild: remove without asking."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --rebuild: re-import as usual, but only report what "
                                 "the rebuild would remove."),
    dsn: Optional[str] = DsnOpt,
) -> None:
    """Import a folder or file. Incremental: an unchanged input is skipped, so running it again is cheap."""
    _warn_argv_dsn(dsn)
    if not path.exists():
        raise _fail(f"{path} does not exist")
    if date_order not in ("dmy", "mdy"):
        raise _fail("--date-order must be dmy or mdy")
    secrets = [str(p) for p in (known_secrets or [])]
    if rebuild and project:
        raise _fail("--rebuild cannot be combined with --project: a filtered run has not read the rest of the input")

    def progress(report, label):
        typer.echo(f"  {label}", err=True)

    def confirm(n_msg: int, n_mem: int) -> bool:   # only when a person is at the keyboard; a script must say --yes
        if not sys.stdin.isatty():
            return False
        return typer.confirm(f"remove {n_msg} message row(s) and {n_mem} memory row(s) that the input no longer produces?")

    report = _guard(commands.import_path, importer, path, dsn=dsn, source=source, timezone=timezone,
                    date_order=date_order, projects=list(project) if project else None, known_secrets=secrets,
                    json_configs=list(json_config or []), force=force, rebuild=rebuild, yes=yes, dry_run=dry_run,
                    confirm=confirm, progress=progress)
    typer.echo(report.summary())
    for err in report.errors[:20]:
        typer.echo(f"error: {err}", err=True)
    if len(report.errors) > 20:
        typer.echo(f"... and {len(report.errors) - 20} more errors", err=True)
    if report.inserted or report.updated:
        typer.echo("new rows are searchable by words and exact strings now; run `resonant-archive embed` "
                   "to add meaning-based search.")
    if not report.ok:
        raise typer.Exit(1)


@app.command("importers")
def cmd_importers() -> None:
    """List the importers (built in, and any installed as plugins)."""
    rows = commands.list_importers()
    width = max((len(n) for n, _, _ in rows), default=8)
    for name, desc, default_source in rows:
        typer.echo(f"{name.ljust(width)}  source={default_source or '-':<14} {desc}")
    for err in importers.plugin_errors():
        typer.echo(f"plugin problem: {err}", err=True)


# ---------------------------------------------------------------- embedder

@models_app.command("fetch")
def cmd_models_fetch(
    model_dir: Optional[Path] = typer.Option(None, "--model-dir", help="Where to put (or find) the model files."),
    force: bool = typer.Option(False, "--force", help="Download again even if the files verify."),
) -> None:
    """Download nomic-embed-text-v1.5 (ONNX, fp32) at a pinned revision and check every file's SHA-256.

    Files already in --model-dir that verify are used as they are, so this also works offline."""
    from .embed import ModelIntegrityError, fetch_model

    last = {"name": None, "pct": -1}

    def progress(name: str, done: int, total: int) -> None:
        pct = int(100 * done / total) if total else 100
        if last["name"] != name or pct >= last["pct"] + 10 or pct == 100:
            last.update(name=name, pct=pct)
            typer.echo(f"  {name}: {pct}%", err=True)

    try:
        d = fetch_model(model_dir, force=force, progress=progress)
    except ModelIntegrityError as exc:
        raise _fail(f"model check failed: {exc}") from None
    except OSError as exc:
        raise _fail(f"could not fetch the model: {exc}") from None
    typer.echo(f"model ready and verified in {d}")


@app.command("serve")
def cmd_serve(
    host: Optional[str] = typer.Option(None, "--host", help="Loopback address only (default 127.0.0.1)."),
    port: Optional[int] = typer.Option(None, "--port", help="Default 8899."),
    model_dir: Optional[Path] = typer.Option(None, "--model-dir"),
    threads: int = typer.Option(4, "--threads"),
) -> None:
    """Run the local embed server. It refuses any address that is not loopback."""
    from .embed import server

    try:
        code = server.serve(host, port, model_dir=model_dir, threads=threads)
    except (server.NonLoopbackHost, FileExistsError) as exc:
        raise _fail(str(exc), 2) from None
    except Exception as exc:  # ModelIntegrityError, FileNotFoundError (model not fetched), RuntimeError
        hint = " (run `resonant-archive models fetch`)" if isinstance(exc, (FileNotFoundError, RuntimeError)) or \
            type(exc).__name__ == "ModelIntegrityError" else ""
        raise _fail(f"{type(exc).__name__}: {exc}{hint}") from None
    raise typer.Exit(code)


@app.command("embed")
def cmd_embed(
    dsn: Optional[str] = DsnOpt,
    limit: Optional[int] = typer.Option(None, "--limit", help="Embed at most this many rows (shortest first)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count what is pending and estimate; write nothing."),
    no_index: bool = typer.Option(False, "--no-index", help="Do not check or build the vector index afterwards."),
    index_only: bool = typer.Option(False, "--index-only", help="Only check or build the vector index."),
    rebuild_index: bool = typer.Option(False, "--rebuild-index", help="Drop the vector index before embedding and "
                                       "build it fresh afterwards. Faster for a large first backfill."),
    no_fallback: bool = typer.Option(False, "--no-fallback", help="Require the embed server; do not load the model in this process."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Compute vectors for rows that lack one. Resumable: stop it any time and run it again.

    Uses the embed server (`resonant-archive serve`) when it is running, else loads the model in this process."""
    from .embed import EmbedClient, EmbedderUnavailable
    from .embed.backfill import BackfillOptions, backfill_postgres, make_stop_flag
    from .embed.backfill_sql import SchemaProblem

    _warn_argv_dsn(dsn)

    if not quiet:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    if rebuild_index and no_index:
        raise _fail("--rebuild-index and --no-index contradict each other")
    target = _guard(config.resolve_dsn, dsn)
    opts = BackfillOptions(limit=limit, dry_run=dry_run, build_index=not no_index, index_only=index_only,
                           rebuild_index=rebuild_index)
    try:
        res = backfill_postgres(target, client=EmbedClient(fallback=not no_fallback), opts=opts, stop=make_stop_flag())
    except EmbedderUnavailable as exc:
        hint = "" if "models fetch" in str(exc) else " Run `resonant-archive models fetch` to download the model, or start `resonant-archive serve`."
        raise _fail(f"the embedder is not available: {exc}.{hint}") from None
    except SchemaProblem as exc:
        raise _fail(f"the database is not set up for embedding: {exc} (run `resonant-archive doctor --fix`)") from None
    except db.DatabaseError as exc:
        raise _fail(str(exc)) from None
    if res.status == "locked":
        raise _fail("another `embed` is already running against this database")
    if res.status == "dry-run":
        typer.echo(f"{res.pending} rows pending ({res.blank_skipped} blank skipped); "
                   f"rough estimate {res.estimate_s / 60:.0f} min on a 4-thread CPU (uncalibrated)")
        return
    if res.status == "index-only":
        typer.echo("vector index built" if res.index_built else "vector index already present")
        return
    typer.echo(f"{res.status}: embedded {res.embedded}, left pending {res.remaining}, "
               f"changed meanwhile {res.skipped_changed}, truncated to the model's limit {res.truncated}")
    if res.status == "stopped":
        raise typer.Exit(130)


# ---------------------------------------------------------------- recall / search

def run_recall_cli(argv: list[str]) -> int:
    """`resonant-archive recall ...`: the engine behind the mod's archive.command. Returns the exit code."""
    from .search import SearchConfig, SearchError, error_envelope, open_readonly, run_recall

    as_json = "--json" in (argv[: argv.index("--")] if "--" in argv else argv)

    def fail(msg: str, kind: str) -> int:
        typer.echo(error_envelope(msg, kind) if as_json else f"recall failed: {msg}")
        return 1

    if not argv or argv[0] in ("-h", "--help"):
        typer.echo("usage: resonant-archive recall <search|around|overview|associate|drill> ... [--json]\n"
                   "see docs/RECALL-CONTRACT.md")
        return 0 if argv else 2
    try:
        cfg = SearchConfig.from_env()
        conn = open_readonly(runtime.readonly_dsn())
    except config.ConfigError as exc:
        return fail(str(exc), "db")
    except SearchError as exc:
        return fail(str(exc), exc.kind)
    try:
        code, out = run_recall(argv, conn, runtime.ClientQueryEmbedder(), cfg)
    finally:
        conn.close()
    typer.echo(out)
    return code


@app.command("recall", context_settings={"allow_extra_args": True, "ignore_unknown_options": True,
                                         "help_option_names": []})
def cmd_recall(ctx: typer.Context) -> None:
    """Search the archive: search, around, overview, associate, drill. Add --json for programs.

    Usage: resonant-archive recall search "words or an exact string" [--source S] [--from 2026-01-01] [--limit 10]"""
    raise typer.Exit(run_recall_cli(list(ctx.args)))


# ---------------------------------------------------------------- stats

@app.command("stats")
def cmd_stats(dsn: Optional[str] = DsnOpt, as_json: bool = typer.Option(False, "--json")) -> None:
    """Counts by source and speaker, and how many rows still await embedding."""
    _warn_argv_dsn(dsn)
    data = _guard(commands.stats, dsn)
    typer.echo(json.dumps(data, default=str, indent=2) if as_json else runtime.stats_text(data))


# ---------------------------------------------------------------- mcp

@app.command("mcp")
def cmd_mcp() -> None:
    """Run the MCP server on stdio (for Claude Desktop, Claude Code and other MCP clients). Read-only."""
    from .mcp_server import run

    run()


# ---------------------------------------------------------------- migrate-v01

@app.command("migrate-v01")
def cmd_migrate_v01(
    old_dir: Path = typer.Option(Path("~/.resonant-archive"), "--from", help="Where v0.1 kept its index."),
) -> None:
    """How to move a v0.1 archive over. Nothing is read from the old index and nothing is changed."""
    old = old_dir.expanduser()
    typer.echo("resonant-archive 2 keeps everything in Postgres and embeds with a different model, so the v0.1 index")
    typer.echo("(384-dimension MiniLM vectors in a Chroma folder) cannot be carried over. Your source files can: import them again.")
    typer.echo("")
    if old.exists():
        typer.echo(f"found a v0.1 folder at {old}. It was not read and is left exactly as it is.")
    else:
        typer.echo(f"no v0.1 folder at {old} (use --from if it lives elsewhere). That is fine: only your source files matter.")
    typer.echo("")
    typer.echo("1. Set up v2:")
    typer.echo("     resonant-archive init --docker        # or: resonant-archive init --dsn postgresql://...")
    typer.echo("     resonant-archive models fetch")
    typer.echo("2. For each folder you indexed with v0.1, import it again under the same label (v0.1 called it a namespace):")
    typer.echo("     resonant-archive import ~/path/to/folder --importer markdown_folder --source <old-namespace>")
    typer.echo("   Obsidian chat exports: --importer nexus_markdown. See `resonant-archive importers` for the rest.")
    typer.echo("3. Add the vectors:  resonant-archive embed")
    typer.echo("4. Your MCP client config keeps working: the tools archive_search and archive_stats are still there")
    typer.echo("   (as aliases, for one release). `resonant-archive mcp` replaces `resonant-archive serve` in that config;")
    typer.echo("   `serve` now runs the embed server.")
    typer.echo("5. When you are happy, delete the old folder yourself. This tool never deletes it.")


def _utf8_streams() -> None:
    """On Windows stdout and stderr use the ANSI code page (cp1252) when they are a pipe or a file, so printing a path
    or a search hit with Japanese or an emoji in it raised UnicodeEncodeError and the command died. Write UTF-8, and
    never fail on a character: a hit is shown, not dropped. Other platforms already default to UTF-8."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main() -> None:
    """Console-script entry point.

    ``recall`` (and its ``search`` shorthand) is handled before typer so its arguments reach the search parser
    untouched: queries that start with a dash, ``--`` terminators and ``--json`` all behave as documented."""
    _utf8_streams()
    argv = sys.argv[1:]
    if argv[:1] == ["recall"]:
        sys.exit(run_recall_cli(argv[1:]))
    if argv[:1] == ["search"]:
        sys.exit(run_recall_cli(["search"] + argv[1:]))
    app()


if __name__ == "__main__":
    main()
