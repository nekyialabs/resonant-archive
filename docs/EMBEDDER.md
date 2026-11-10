# The embedder

Meaning-based search needs vectors. resonant-archive makes them locally, with no API and no account.

## The model

[`nomic-embed-text-v1.5`](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5), Apache-2.0 licence, run with ONNX
Runtime on the CPU. fp32, 768 dimensions. Texts are prefixed `search_document:` when stored and `search_query:` when
searched, as the model expects. Long texts are cut to the model's 2,048-token limit and reported.

Credit to Nomic AI for the model.

## Getting it

The model is about half a gigabyte, so it is not bundled.

```bash
resonant-archive models fetch
```

This downloads four files from the Hugging Face repo at a **pinned revision**, into
the data folder (`~/.local/share/resonant-archive/models/nomic-embed-text-v1.5` on Linux, `%LOCALAPPDATA%/resonant-archive/models/nomic-embed-text-v1.5` on Windows), and verifies each against a **pinned SHA-256 sum and size**. A mismatch deletes
the file and stops. Nothing unverified is left under its final name.

**Offline:** put the files in a folder and run `resonant-archive models fetch --model-dir /that/folder`. Files that
already verify are used as they are. The same folder can be set with `RESONANT_EMBEDDER_MODEL_DIR`.

## Using it

```bash
resonant-archive embed                 # vectors for every row that lacks one
resonant-archive embed --dry-run       # count what is pending, write nothing
resonant-archive embed --limit 5000    # a bounded run
resonant-archive serve                 # keep a warm embed server on 127.0.0.1:8899
```

- **Resumable.** Stop it any time and run it again; it picks up where it was. Only one `embed` runs at a time per
  database.
- **Incremental.** A row is re-embedded only if its text changed.
- **Search works meanwhile.** `import` doesn't wait for vectors. Words and exact strings work at once; the answer footer
  says how many rows are pending.
- **With or without `serve`.** If the embed server is running, `embed`, `recall` and the MCP server use it. If not,
  they load the model in their own process (slower to start). `embed --no-fallback` insists on the server.
- **Hidden rows** (tool calls, harness input) are not embedded.

`serve` options: `--host` (loopback only, default 127.0.0.1), `--port` (8899), `--threads` (4), `--model-dir`. These can
also be set with `RESONANT_EMBEDDER_HOST`, `RESONANT_EMBEDDER_PORT` and `RESONANT_EMBEDDER_THREADS`. An example systemd
user unit is in [`contrib/systemd/`](../contrib/systemd/). It makes no outbound connections, and the unit says so to
the kernel as well.

## Why fp32 and not int8

The int8 export of this model gives a text a different vector depending on which other texts it was batched with. In
our tests the cosine between the same text embedded alone and in a batch was as low as 0.745. That means the same
message could land in different places depending on when it was embedded. fp32 is exactly batch-invariant. It is bigger
and a bit slower, and it is worth it. Please don't "optimise" this without re-running that check.

The server also checks it is still embedding in the same space: `GET /health?space=1` embeds a fixed probe and compares
it with a stored reference, and fails closed if the model has moved. A store that mixes two vector spaces fails
silently, so this one is loud.

## How long does it take?

On a CPU, the first embedding of a big archive is the slow step. We haven't published timings yet. `embed --dry-run`
prints a rough, uncalibrated estimate. Run it in the background and carry on.

## Changing the model

Don't, casually. Vectors from different models can't be compared, and the column is `vector(768)`. A new model means
a new migration and re-embedding everything.
