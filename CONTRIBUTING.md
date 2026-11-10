# Contributing to resonant-archive

Thanks for your interest in contributing. `resonant-archive` is a focused tool — the architecture is opinionated and kept deliberately small. This guide helps you contribute effectively.

## How to reach us

- **Bug reports** — [GitHub Issues](https://github.com/nekyialabs/resonant-archive/issues)
- **Feature proposals** — [GitHub Issues](https://github.com/nekyialabs/resonant-archive/issues) (open an issue before writing code for anything non-trivial)
- **Questions & discussion** — [GitHub Discussions](https://github.com/nekyialabs/resonant-archive/discussions) (if enabled)
- **Everything else** — [nekyialabs.com](https://nekyialabs.com)

## What we welcome

These can go straight to a PR:

- **Bug fixes** — with a clear description of what was broken and how you fixed it
- **Documentation** — typos, clarifications, better examples, corrections
- **Additional tests** — coverage for edge cases we haven't thought of
- **Platform fixes** — Windows / macOS / Linux path-handling or file-locking quirks
- **Small DX improvements** — better error messages, clearer CLI help text, progress bar tweaks

## What needs an issue first

Open a GitHub Issue to discuss before writing code:

- **New CLI commands** — the command surface is intentionally small; adding one affects the whole UX
- **New importers for formats we don't read yet** (PDF, DOCX, HTML, other chat exports) — tell us the format and how you'd chunk it. Importers can also live in your own package ([docs/DESIGN.md](docs/DESIGN.md)).
- **New embedding models as defaults** — vectors from different models can't be compared, so this means a migration for everyone
- **New dependencies** — we keep the dependency tree small
- **Cloud features** — `resonant-archive` is local-first by design

## Help wanted

- **Direct importers** for raw ChatGPT and claude.ai `conversations.json` exports (we need real-export checks, run locally)
- **PostgreSQL-backed tests on Windows.** The mod and the Python package run on Windows and CI runs everything that needs no database there; the database tests run on Linux only, and nobody has run them against a native Windows PostgreSQL
- **Benchmarks** — reproducible retrieval quality and embedding speed numbers on stated machines
- **More tests** for edge cases in importers and redaction

Open an issue to discuss your approach before writing code.

## What we won't accept

- **Cloud-hosted variants.** Local-first is the point.
- **Telemetry**, or anything that sends conversation text off the machine.
- **Loosening the loopback-only rule** for the embed server.
- **GUI wrappers.** The workflow is through your AI and the CLI.

## Development setup

```bash
git clone https://github.com/nekyialabs/resonant-archive.git
cd resonant-archive
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows (PowerShell):

```powershell
git clone https://github.com/nekyialabs/resonant-archive.git
cd resonant-archive
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Run the tests:

```bash
pytest tests/
```

On Windows a few tests skip for a reason that is printed (`pytest -rs`): symbolic links need Developer Mode or an elevated shell, and there are no FIFOs. The repository's `.gitattributes` keeps text files as LF on every platform; do not commit CRLF. The mod's `scripts/lint.sh` and the `private-strip` lint need a `bash` (Git for Windows has one) and a working `python` or `python3` on the path.

Tests that need Postgres skip themselves when none is available. For the full set, bring up the bundled one
(`resonant-archive init --docker --up`). The tests use synthetic fixtures only; never add a real conversation to the
repository.

The mod has its own tests and lint, see [`claude-code/chronicle/README.md`](claude-code/chronicle/README.md).

Try the CLI end-to-end:

```bash
resonant-archive --help
resonant-archive import ./some-markdown-dir --importer markdown_folder --source dev
resonant-archive stats
resonant-archive recall search "a probe query"
```

## PR guidelines

- **One thing per PR.** Bug fix? One PR. New test? One PR. Don't bundle unrelated changes.
- **Describe what and why.** Not just what you changed — why it matters.
- **Run the tests.** `pytest tests/` must pass before you submit.
- **Add tests when it makes sense.** Especially for importers, redaction patterns and new CLI flags.
- **Match the existing style.** Look at the code around your change and follow the same patterns.
- **No AI-generated code without review.** If you used an AI to write it, review it thoroughly. We will.
- **No generated diffs in the commit.** Keep whitespace-only diffs out of PRs unless that's literally the purpose of the PR.

## Code style

- **Python 3.11+ syntax.** Use `str | None` instead of `Optional[str]`, `list[int]` instead of `List[int]`.
- **Strict typing** throughout `resonant_archive/`. No `Any` without a good reason.
- **Dataclasses** for structured data, not dictionaries.
- **Docstrings** on public functions and classes. Keep them concise — what and why, not how.
- **No defensive validation** for internal code — trust the types. Validate only at system boundaries (CLI input, file I/O, HTTP).
- **Functions over classes** where possible. Classes are for stateful things (the embedder client, the database connection), not for code organization.

## License

By contributing, you agree that your contributions will be licensed under the project's licence, Apache-2.0 (see [LICENSE](LICENSE)).
