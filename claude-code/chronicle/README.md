# chronicle

Makes compaction bearable.

A conversation with an AI lives in a window of limited size. When it is nearly full, Claude Code compacts it: everything so far is replaced by one summary, and the conversation carries on from the summary. That keeps you going, but the summary is written generically, in one pass, and the details go: file paths, ids, the reason behind a decision, what you were about to do next.

**Before chronicle:** one blob of summary, written without knowing what matters, replacing the whole past. The last few exchanges, the ones you most need, are summarised too. If something was lost, it is gone.

**With chronicle:** the older part is summarised in the assistant's own voice, told to keep identifiers, decisions and open threads. The recent part is kept exactly as it was. You pick the moment with `/squish`, and a meter tells you before it gets urgent. Every message was already saved to your disk as it happened, so if the summary dropped something, `recall` finds the original.

## The four promises

1. Your AI writes its own compaction summary, in a voice you configure, and keeps exact identifiers and decisions.
2. The recent stretch stays word for word (split compaction): you do not lose the last hour to a summary.
3. You choose when: `/squish` any time, and a context meter that flags at 75% and 90%.
4. Nothing is truly lost: every row is mirrored to disk (secrets redacted), and `recall` brings the sharp version back from the mirror, or from an archive if you add one.

## What it adds

- **Split compaction.** The older part goes to the native summariser with a steer (below); the recent raw tail is handed back whole. Any failure before the summariser runs falls back to ordinary native compaction.
- **`/squish [focus]`** runs `/compact` itself, so the trigger stays `manual` and everything above runs exactly as for a typed `/compact`. `/squish status` prints the fill. `/squish auto on|off` flips Claude Code's `autoCompact` setting.
- **The context line.** Prompts typed in the composer (or sent by a bridge) get `[context · 142k of 1M (14%)]` appended as context, then ` · squish soon` from 75% and ` · SQUISH NOW` from 90%.
- **The mirror.** Every main-loop row goes to `<archiveRoot>/<day>/<session>.jsonl`, redacted. Folders are 0700, files 0600.
- **`mcp__chronicle__recall`.** Searches the mirror (rows, compaction summaries, memories) newest first. With an archive configured it also searches that.
- **Memories** (off by default, see below).

## Install

Linux, macOS and Windows. On Linux and macOS it needs [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on the path for `recall`, and `sh`, `mkdir` and `cat`, which every Linux and macOS has. On Windows it needs nothing extra: the mirror is appended through `powershell.exe` (always present) and the archive is listed, searched and read through the engine's own file API, so ripgrep is not used there.

1. Copy this folder somewhere permanent, for example `~/.claude/mods/chronicle` (Windows: `%USERPROFILE%\.claude\mods\chronicle`).
2. Load it: set `CLAUDE_CODE_PLUGIN_DIRS=/path/to/chronicle` (colon-separate several) in the environment that starts Claude Code, or pass `--plugin-dir /path/to/chronicle`.
3. Restart Claude Code.

It runs with no configuration file at all. Rows are mirrored to `~/.chronicle/archive`, compaction is steered with the default steer, `/squish` and the context line are on, memories are off, and there is no archive.

## Configure

`cp chronicle.example.json chronicle.json` and edit it. The real `chronicle.json` is git-ignored. A missing key, or a value of the wrong type, falls back to the default shown; unknown keys are ignored. The mod never fails to load over its settings.

| Key | Default | Meaning |
|---|---|---|
| `archiveRoot` | `~/.chronicle/archive` | Where the mirror is written. `~` is your home folder. |
| `timezone` | system zone | Day folders are named in this zone (an IANA name such as `Europe/Rome`). The zone is recorded in `<archiveRoot>/.chronicle.json` the first time it is written, and that record is used afterwards, so changing your machine's zone cannot split a day. |
| `names.user`, `names.agent` | `You`, `Assistant` | The labels recall prints for each side. |
| `mirror.enabled` | `true` | `false` writes nothing (and so disables memories and session recall). |
| `compaction.tailTokens` | `60000` | How much of the recent conversation to keep raw, at least. |
| `compaction.maxTailTokens` | `120000` | The most to keep raw (also capped at 30% of the window). |
| `compaction.minOlderMessages` | `6` | Below this many older messages, compaction is left to the native one (still steered). |
| `compaction.voice` | empty | A short paragraph put in front of the steer, for example who the assistant is to you. |
| `compaction.steer` | empty | Replaces the default steer completely. A sentence forbidding credentials is added if yours lacks it. |
| `memories.enabled` | `false` | See "Memories". |
| `memories.chunkMinTokens`, `chunkMaxTokens` | `15000`, `25000` | The size of a stretch a memory is written for. |
| `memories.voice` | empty | A paragraph put in front of the memory instructions. |
| `memories.memoryVoice` | empty | Replaces the memory instructions completely. |
| `memories.assemble` | `manual` | `manual`: memories replace the older context only for a `/compact` or `/squish` you run. `auto`: for every compaction. `false`: never. |
| `memories.gapRawMaxTokens` | `12000` | A stretch not yet covered by a memory is kept raw up to this size; a bigger one is summarised. |
| `squish.enabled` | `true` | Registers `/squish`. |
| `contextLine.enabled`, `warnAt`, `urgentAt` | `true`, `75`, `90` | The meter. |
| `tools.rgPath` | `rg` | The ripgrep binary (Linux and macOS; ignored on Windows). |
| `archive.command` | none | See "Archive". |
| `archive.timeoutMs` | `30000` | How long the archive command may run. |
| `legacyToolNames` | none | Names this tool had under an earlier plugin name, for example `["mcp__old-name__recall"]`, so rows mirrored then are still recognised. |

Older flat keys (`tailTokens`, `maxTailTokens`, `minOlderMessages`, `steer`, `chunkMinTokens`, `chunkMaxTokens`, `memoryVoice`, `assembleWithMemories`, `gapRawMaxTokens`) are still read; the nested keys win when both are present. A file that already used the flat memory keys is taken to want memories on, unless `memories.enabled` says otherwise.

### The default steer

The summary of the older part is asked to be written in the first person, as the assistant, about "the person I'm working with", keeping every exact identifier verbatim, every decision and who made it, promises and open threads, the person's stated preferences and state where they matter, the tone between you, and what was about to happen next. It is told never to copy credentials. Set `compaction.voice` to add who you are to each other; set `compaction.steer` to replace it.

## Memories

With `memories.enabled: true`, every 15-25k tokens of conversation, at the end of a turn, the assistant writes a short memory of that stretch in the first person, in the background, using a forked call over the session's cached transcript. At a manual compaction, if the memories provably cover the older part (an unbroken chain from the first row, each memory signed with a key kept outside the mirror), the older part is replaced by them with no summariser call at all. Anything short of that proof falls back to split compaction.

**This spends model calls in the background.** The prefix is cached, so it is cheap, but it is still your usage. It is also the biggest saving at compaction time, which is why the switch exists rather than the feature being removed. It is off until you turn it on.

The signature is an integrity check, not secrecy: it stops a memory file pulled in from somewhere else (a `git pull`, say) from reaching the context. Memories are redacted like everything else.

## Archive (optional)

Without an archive, `recall` offers the session source only, and says so: asking for `source: archive` returns "no archive is set up" and how to set one up. Nothing but `rg` is run.

To search every past conversation, install `resonant-archive` and set:

```json
"archive": { "command": ["resonant-archive", "recall"] }
```

### Archive contract

For `source: archive` (the default once a command is set) the mod runs

```
<command...> --json <action> [options] [-- <positional>]
```

with no shell, one argument per element. `<action>` is `search`, `around`, `overview`, `drill` or `associate`.

| Action | Options | Positional |
|---|---|---|
| `search` | `--source a,b` `--from YYYY-MM-DD` `--to YYYY-MM-DD` `--speaker x,y` `--context x,y` `--limit N` `--mode hybrid\|fts\|vector\|anchor` `--hidden` `--recent` | the query |
| `around` | `--before N` `--after N` `--hidden` | a message id |
| `associate` | `--k N` `--recent` `--hidden` | a message id |
| `overview` | `--from` `--to` (both required) `--source a,b` `--memories-only` `--no-memories` | none |
| `drill` | `--hidden` | a memory `#index` or its `from_uuid` |

Standard output is one JSON object, `{"ok": true, "text": "<plain text for the assistant to read>"}`, or `{"ok": false, "error": "<message>"}`. The text should be compact (about 6,000 characters) and say when it capped itself; the mod cuts anything over 12,000. Plain text that is not JSON is passed through as is. A non-zero exit with nothing on standard output is reported with the last lines of standard error. The command holds its own database settings and credentials: the mod passes none.

## Mirror format

`<archiveRoot>/<YYYY-MM-DD>/<session id>.jsonl` holds one JSON object per line:

- a row: `{"t": <ms>, "sid", "uuid", "door", "origin", "role", "text", "tools": [...], "results": [...]}`, redacted. Tool results are cut at 2,000 characters; a result from a secret-bearing source (a `.env`, a private key, `env`, `ps`, a search for passwords; best effort, see docs/PRIVACY.md in the repository) is replaced by `[withheld: secret-bearing source]`, and the input of that call is cut to the tool name and the file or program; recall output is not mirrored.
- events: `{"t", "event": "compaction" | "session_fresh" | "memory_formed" | "memory_skipped" | "memory_failed" | "mirror_dropped", ...}`. A compaction event carries the (redacted) summary.

`<session id>.memories.jsonl` holds `{"event": "memory", "level": 1, "ordinal", "text", "from_uuid", "to_uuid", "t_from", "t_to", "mac", ...}` lines. `<archiveRoot>/.chronicle.json` records the day-folder zone.

The mirror is private data. Keep it out of any public repository.

## Validate

```sh
claude plugin validate .
claude plugin test .
./scripts/lint.sh
```

`claude plugin test .` runs the tests in `tests/` (they use `claude-code/testing`). `scripts/lint.sh` checks that the tree holds no private names, hosts or paths (the public checks always; your own terms from the file named by `PRIVATE_STRIP_TERMS_FILE`, see `scripts/private_strip_lint.py` in the repository root) and that `chronicle.example.json` matches the copy the tests use. To typecheck, `tsc --noEmit -p .` after Claude Code has written `.claude-plugin/types/` (the tests do not need it).

Tested against Claude Code 2.1.301.

## Caveats

- Mods (function hooks) are an early-access feature of Claude Code and may change between releases without notice. Claude Code writes the type declarations into `.claude-plugin/types/` itself when it loads the mod, so they are not included here.
- Redaction is pattern-based. Over-redaction is accepted (a few characters of record are lost); a leaked key can only be rotated. It is not a substitute for keeping the mirror private.
- Memory formation (off by default) spends model calls in the background.
- `/squish` starts `/compact` just after answering, because Claude Code refuses to run a command from inside a command hook.
- On Linux and macOS, session recall and assembly shell out to `rg`, `sh`, `mkdir` and `cat`. On Windows (the environment has `OS=Windows_NT`, or the home folder starts with a drive letter) the mirror is appended by a PowerShell script passed as `-EncodedCommand` (a byte copy of stdin, so no shell quoting is involved), and listing, searching and reading go through `$.fs`. Two Windows limits: a single mirror file over the engine's 4 MiB read limit is not searched or read whole, and `recall` says so; and the 0700/0600 permission bits do not apply, the mirror sits under the user's own profile folder.
- The memory block's heading used to carry a fixed city name in brackets; blocks written that way are still accepted.

Built by [Nekyia Labs](https://nekyialabs.com).
