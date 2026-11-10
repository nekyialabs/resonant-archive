# Upgrading from v0.1

Version 2 is a rewrite. It stores everything in Postgres and embeds with a different model, so a v0.1 index can't be
converted in place. Your source files are what matter, and importing them again is quick.

`resonant-archive migrate-v01` prints these steps. It reads nothing and changes nothing.

## What changed

| v0.1 | v2 |
|---|---|
| Chroma folder at `~/.resonant-archive/` | Postgres + pgvector (one container: `init --docker --up`) |
| `all-MiniLM-L6-v2`, 384 dimensions | `nomic-embed-text-v1.5`, 768 dimensions, ONNX, no torch |
| Semantic search only | Words and exact strings, meaning, and anchors, rank-fused |
| Namespaces | The `source` label on each row (`--namespace` still works as an alias of `--source`) |
| `serve` ran a search API daemon | `serve` runs the local embed server. The MCP server needs no daemon. |
| Python 3.11 or 3.12 | Python 3.11 or newer |
| MCP tools `archive_search`, `archive_stats` | `search`, `around`, `overview`, `associate`, `stats`. The old names stay as aliases for one release, then go. |
| Any OS | Linux, macOS and Windows (see the README's Platforms section). |

## Steps

1. **Set up v2.**

   ```bash
   git clone https://github.com/nekyialabs/resonant-archive && cd resonant-archive
   pip install -e .
   resonant-archive init --docker --up      # or: export RESONANT_ARCHIVE_DSN=postgresql://... ; resonant-archive init
   resonant-archive models fetch
   ```

2. **Import your source folders again,** under the same label v0.1 used as the namespace:

   ```bash
   resonant-archive import ~/path/to/folder --importer markdown_folder --source my-notes
   ```

   For Obsidian chat exports made with the Nexus AI Chat Importer, use `--importer nexus_markdown`. Run
   `resonant-archive importers` to see the rest.

3. **Add the vectors.** This is the slow step on a CPU; it is resumable.

   ```bash
   resonant-archive embed
   ```

4. **Update your MCP client config.** Your existing entry keeps working (`resonant-archive mcp`, with the old tool names
   as aliases). Remove any step that starts `resonant-archive serve` before Claude Desktop: the MCP server no longer
   needs it. Run `serve` only if you want a warm embed server.

5. **Check:** `resonant-archive stats` and `resonant-archive recall search "something you know is in there"`.

6. **When you're happy, delete `~/.resonant-archive/` yourself.** This tool never touches it.

## If you no longer have the source files

The text is stored inside the v0.1 Chroma folder. A tool to read it back out is not in v2.0. If this is your situation,
open an issue and we'll help; don't delete the old folder meanwhile.

## Things that don't carry over

- The v0.1 chunk ids. Re-importing creates new rows with new ids.
- The `~/.resonant-archive` location. v2 keeps its settings in `~/.config/resonant-archive/config.toml` and the model
  in the data folder.
- The cloud-import guide (`CLOUD_IMPORT.md`) is gone with the Chroma store it described.
