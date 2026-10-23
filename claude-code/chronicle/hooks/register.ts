import type { EngineInterface, Register, SessionCompactResult, SessionMessage } from 'claude-code'

// chronicle, v0.1. Keeps a conversation bearable across compaction.
// 1. Mirror: every main-loop row is written to a private folder on disk (secrets redacted first), day by day.
// 2. Split compaction: the older part goes to native compaction, steered toward the assistant's own voice and exact
//    identifiers; the recent raw tail comes through whole (prompts keep their engine handles; the rest is rebuilt,
//    see keptMessages). Any failure before the summarizer runs falls back to native compaction.
// 3. Recall: the tool mcp__chronicle__recall. The session source searches the mirror (rows, compaction summaries,
//    memories) with ripgrep. The archive source, only when archive.command is configured, hands the query to an
//    external `resonant-archive recall --json` command (see README.md for the contract).
// 4. Memories (off by default, memories.enabled): every 15-25k tokens of mirrored rows, at a turn's end, the assistant
//    writes its own memory of that stretch with $.model.fork over the session's cached transcript, in the
//    background, one at a time. Saved beside the mirror.
// 5. Assembly: at compaction, when the signed memories provably cover the older part (from the session's first row,
//    with no hole, checked against the mirror's uuid order), the older part becomes the memories block instead of a
//    summary: no model call when the uncovered gap is small, one summarizer call over the gap alone when it is not.
//    Any doubt keeps the split compaction of item 2 exactly as it was. Until it has run supervised in a real engine,
//    assembly runs only for a manual /compact (memories.assemble: "manual").
// 6. Redaction: every string bound for the mirror (rows, memories, summaries) goes through redact() before any cut,
//    and a tool result from a secret-bearing source (a .env, a private key, `env`, `ps`) is withheld whole.
// 7. /squish and the context-fill line (75% and 90% flags).
//
// Engine facts this file leans on (claude-code index.d.ts, 2.1.301):
// - A hook that throws or overruns gets `next(e)` run on its behalf. So after a hook has called `next`,
//   nothing in it may throw: that would double-append a row or double-compact.
// - /clear changes the session id with no session.start after it, so every row carries the id it was appended under.
// - A hot reload drops module memory. The buffer is flushed on a timer, at a size threshold and at each turn's end,
//   so the window a reload can lose is small. No unload event exists to flush on.

// The settings as the code reads them: normalizeConfig() turns chronicle.json (new nested keys, or the older flat ones)
// into this, with a safe default for anything missing or of the wrong type.
type Cfg = {
  archiveRoot: string
  timezone: string // '' = the stored zone of this archive, else the system zone
  names: { user: string; agent: string }
  mirrorEnabled: boolean
  tailTokens: number; maxTailTokens: number; minOlderMessages: number
  voice: string // a persona paragraph put in front of the steer
  steer: string // '' = the default steer; anything else replaces it
  memoriesEnabled: boolean
  chunkMinTokens: number; chunkMaxTokens: number
  memoryVoice: string // a persona paragraph put in front of the memory instructions
  memoryPrompt: string // '' = the default memory instructions; anything else replaces them
  // false: off. 'manual' (the default): only a /compact the person runs. true or 'auto': every main-loop compaction.
  assembleWithMemories: boolean | 'manual' | 'auto'; gapRawMaxTokens: number
  squishEnabled: boolean
  contextLine: { enabled: boolean; warnAt: number; urgentAt: number }
  rgPath: string
  archiveCommand: string[]; archiveTimeoutMs: number
  legacyToolNames: string[] // names of this tool under an earlier plugin name, so old mirrored rows are still recognised
  ignored: string[] // keys in the file that this version does not know
}
// suffix '.memories' sends the line to <sid>.memories.jsonl instead of the mirror file <sid>.jsonl.
type Entry = { day: string; sid: string; line: string; suffix?: string }

const FLUSH_EVERY_MS = 30_000
const FLUSH_AT_CHARS = 256 * 1024
const BUFFER_CAP_CHARS = 5 * 1024 * 1024
const WARN_AFTER_FAILURES = 3
const RUN_TIMEOUT_MS = 10_000
const GREP_OUT_CAP = 4 * 1024 * 1024 // Windows search: matched text is capped where rg's stdout was
const TAIL_SHARE_OF_WINDOW = 0.3
const SUMMARY_IN_LOG_CHARS = 20_000
// Double-quoted on purpose: an apostrophe inside a single-quoted string once kept this plugin from loading at all.
const TAIL_NOTE = "\n\nThe final reply of this slice, and the most recent stretch of the conversation after it, are kept verbatim right after your summary: the last request of the slice has already been answered. End your summary where this slice ends, and under pending tasks and next step list only what is still genuinely open, not that last request."

// Module state. A hot reload starts it over.
let buf: Entry[] = []
let bufChars = 0
let dropped = 0 // rows refused while the buffer was at its cap; recorded in the next flush
let failStreak = 0
let warned = false
let sizeFlushQueued = false
let chain: Promise<void> = Promise.resolve() // flushes run one at a time, in order
let inflight: Entry[] = [] // the batch a flush is writing right now (neither in buf nor surely on disk): read by memoryEvidence
let timer: { cancel: () => void } | undefined
let sid: string | undefined // cached session id; reset at session.end (a /clear or resume brings a new one)
let lastT = 0 // last good clock reading, a fallback stamp
// The uuids of the prompts of the turn now running (the person typed or queued them). recall leaves them out: the request
// that asked for a search holds its query verbatim, so it would always rank first (seen in live testing). Cleared at each
// turn end and session end; capped so a long turn cannot grow it.
const turnPrompts = new Set<string>()
// Set when the /exit (or /quit) command row is seen: the farewell the engine appends during shutdown is neither
// mirrored nor counted (seen in live testing: the final flush never wrote it, but it became the next chunk's start).
let exitingSid: string | undefined
const TURN_PROMPTS_MAX = 32
let cfgCache: Cfg | undefined

// The tool's full name. A tool registered by the plugin `chronicle` is called mcp__chronicle__<name>.
export const RECALL_TOOL = 'mcp__chronicle__recall'
// Written once into the archive root: the zone its day folders are named in, so a changed system zone cannot split a day.
const ARCHIVE_MARKER = '.chronicle.json'
const DEFAULT_ROOT = '~/.chronicle/archive'
const CONFIG_KEYS = new Set([
  'archiveRoot', 'timezone', 'names', 'mirror', 'compaction', 'memories', 'squish', 'contextLine', 'tools', 'archive', 'legacyToolNames',
  // older flat keys, still read
  'tailTokens', 'maxTailTokens', 'minOlderMessages', 'steer', 'chunkMinTokens', 'chunkMaxTokens', 'memoryVoice',
  'assembleWithMemories', 'gapRawMaxTokens',
])

function rec(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}
}
function pickStr(v: unknown, d: string): string { return typeof v === 'string' ? v : d }
function pickBool(v: unknown, d: boolean): boolean { return typeof v === 'boolean' ? v : d }
function pickNum(v: unknown, d: number): number { return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : d }
function pickPct(v: unknown, d: number): number { return typeof v === 'number' && Number.isFinite(v) && v > 0 && v <= 100 ? v : d }
function firstDefined(...vs: unknown[]): unknown { return vs.find(v => v !== undefined) }

export function validZone(z: string): boolean {
  if (!z) return false
  try { new Intl.DateTimeFormat('en-GB', { timeZone: z }); return true } catch { return false }
}

function expandHome(p: string, home: string | undefined): string {
  if (p === '~' || p.startsWith('~/')) return home ? `${home.replace(/\/+$/, '')}${p.slice(1)}` : ''
  return p
}

// Pure. Never throws: the mod must not fail to load over its settings. Wrong types fall back to the default, unknown
// keys are listed in `ignored`. The new nested keys win over the older flat ones when both are present.
export function normalizeConfig(raw: unknown, home?: string): Cfg {
  const r = rec(raw)
  const compaction = rec(r.compaction), memories = rec(r.memories), mirrorC = rec(r.mirror), squish = rec(r.squish)
  const line = rec(r.contextLine), tools = rec(r.tools), archive = rec(r.archive), names = rec(r.names)
  const legacyMemories = r.chunkMinTokens !== undefined || r.chunkMaxTokens !== undefined || r.assembleWithMemories !== undefined
  const asm = firstDefined(memories.assemble, r.assembleWithMemories)
  const warnAt = pickPct(line.warnAt, 75)
  const urgent = pickPct(line.urgentAt, 90)
  const command = Array.isArray(archive.command) ? archive.command.filter((x): x is string => typeof x === 'string' && x.length > 0) : []
  const root = expandHome(pickStr(r.archiveRoot, DEFAULT_ROOT).trim() || DEFAULT_ROOT, home)
  const min = pickNum(firstDefined(memories.chunkMinTokens, r.chunkMinTokens), DEFAULT_CHUNK_MIN)
  const zone = pickStr(r.timezone, '').trim()
  return {
    archiveRoot: root.replace(/\/+$/, '') || root,
    timezone: validZone(zone) ? zone : '',
    names: { user: pickStr(names.user, '').trim() || 'You', agent: pickStr(names.agent, '').trim() || 'Assistant' },
    mirrorEnabled: pickBool(mirrorC.enabled, true),
    tailTokens: pickNum(firstDefined(compaction.tailTokens, r.tailTokens), 60_000),
    maxTailTokens: pickNum(firstDefined(compaction.maxTailTokens, r.maxTailTokens), 120_000),
    minOlderMessages: pickNum(firstDefined(compaction.minOlderMessages, r.minOlderMessages), 6),
    voice: pickStr(compaction.voice, '').trim(),
    steer: pickStr(firstDefined(compaction.steer, r.steer), '').trim(),
    // Memories are off unless asked for. An older file that already tuned them (flat chunk or assembly keys) is
    // taken to have wanted them, so an install made before the switch existed keeps working.
    memoriesEnabled: pickBool(memories.enabled, legacyMemories),
    chunkMinTokens: min,
    chunkMaxTokens: Math.max(min, pickNum(firstDefined(memories.chunkMaxTokens, r.chunkMaxTokens), DEFAULT_CHUNK_MAX)),
    memoryVoice: pickStr(memories.voice, '').trim(),
    memoryPrompt: pickStr(firstDefined(memories.memoryVoice, r.memoryVoice), '').trim(),
    assembleWithMemories: asm === false || asm === true || asm === 'auto' || asm === 'manual' ? asm : 'manual',
    gapRawMaxTokens: pickNum(firstDefined(memories.gapRawMaxTokens, r.gapRawMaxTokens), DEFAULT_GAP_RAW_MAX),
    squishEnabled: pickBool(squish.enabled, true),
    contextLine: { enabled: pickBool(line.enabled, true), warnAt, urgentAt: Math.max(urgent, warnAt) },
    rgPath: pickStr(tools.rgPath, '').trim() || 'rg',
    archiveCommand: command,
    archiveTimeoutMs: pickNum(archive.timeoutMs, 30_000),
    legacyToolNames: (Array.isArray(r.legacyToolNames) ? r.legacyToolNames : []).filter((x): x is string => typeof x === 'string' && /^mcp__[\w-]+__[\w-]+$/.test(x)),
    ignored: Object.keys(r).filter(k => !CONFIG_KEYS.has(k)),
  }
}

// What the rest of the file reads without a $ in hand: set from the settings the first time they are read.
let zone = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { return 'UTC' } })()
let labels = { user: 'You', agent: 'Assistant' }
let rgBin = 'rg'
// Windows host: the mirror is appended through PowerShell and the archive is listed, searched and read through $.fs
// (no sh, rg or cat there). Set from the environment the first time the settings are read.
let isWin = false
let memoriesOn = false
let recallNames: readonly string[] = [RECALL_TOOL]
let zoneToStore: string | undefined // set when the archive has no .chronicle.json yet: flush writes it once

function applyConfig(cfg: Cfg) {
  labels = cfg.names
  rgBin = cfg.rgPath
  memoriesOn = cfg.memoriesEnabled
  recallNames = [RECALL_TOOL, ...cfg.legacyToolNames]
}

// Reads chronicle.json once (a missing file is not an error: the defaults run), works out the day-folder zone and
// the archive root, and caches. Never throws.
async function cfgOf($: EngineInterface): Promise<Cfg> {
  if (cfgCache) return cfgCache
  let raw: unknown
  try { raw = JSON.parse(String(await $.fs.read(`${$.plugin.root}/chronicle.json`))) } catch (err) {
    if (!/ENOENT|no such file|not found|does not exist|no implementation/i.test(why(err))) { try { $.ui.log(`chronicle: chronicle.json not used (${why(err)}); defaults apply`, { to: 'debug' }) } catch {} }
  }
  let home: string | undefined
  try { home = (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) } catch {}
  let os = ''
  try { os = (await $.env.get('OS')) ?? '' } catch {}
  isWin = os === 'Windows_NT' || /^[A-Za-z]:[\\/]/.test(home ?? '')
  const cfg = normalizeConfig(raw, home)
  if (cfg.ignored.length) { try { $.ui.log(`chronicle: unknown settings ignored: ${cfg.ignored.join(', ')}`, { to: 'debug' }) } catch {} }
  // The day folders are named in one zone for the life of an archive: the one it recorded, unless the settings name one.
  let stored = ''
  if (cfg.archiveRoot) {
    try {
      const m = JSON.parse(String(await $.fs.read(`${cfg.archiveRoot}/${ARCHIVE_MARKER}`))) as { timezone?: unknown }
      if (typeof m.timezone === 'string' && validZone(m.timezone)) stored = m.timezone
    } catch {}
  }
  if (cfg.timezone) zone = cfg.timezone
  else if (stored) zone = stored
  zoneToStore = stored === zone ? undefined : zone
  applyConfig(cfg)
  cfgCache = cfg
  return cfg
}

function dayOf(ms: number): string {
  if (!ms) return 'undated'
  const p = new Intl.DateTimeFormat('en-GB', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(ms))
  const g = (t: string) => p.find(x => x.type === t)?.value ?? ''
  return `${g('year')}-${g('month')}-${g('day')}`
}

// Path-safe piece of a folder or file name.
function safe(s: string): string {
  return s.replace(/[^A-Za-z0-9._-]/g, '_') || 'unknown'
}

function why(err: unknown): string {
  return String((err as { message?: unknown } | undefined)?.message ?? err).slice(0, 200)
}

// ---------- mirror buffer ----------

// `keep`: never dropped at the cap (a memory is small and cannot be made again cheaply).
function push(entry: Entry, keep = false) {
  if (!keep && bufChars + entry.line.length > BUFFER_CAP_CHARS) { dropped++; return }
  buf.push(entry)
  bufChars += entry.line.length
}

function requeue(entries: Entry[]) {
  buf = entries.concat(buf)
  bufChars = buf.reduce((n, x) => n + x.line.length, 0)
}

function noteFailure($: EngineInterface, reason: string) {
  failStreak++
  try { $.ui.log(`chronicle: mirror flush failed (${reason})`, { to: 'debug' }) } catch {}
  if (failStreak >= WARN_AFTER_FAILURES && !warned) {
    warned = true
    try { $.ui.log(`chronicle: archive mirror has failed ${failStreak} times in a row (${reason}). Rows are held in memory (cap 5 MB) and retried.`) } catch {}
  }
}

function noteSuccess($: EngineInterface) {
  if (warned) { try { $.ui.log('chronicle: archive mirror recovered.') } catch {} }
  failStreak = 0
  warned = false
}

// Writes the buffer to <archiveRoot>/<day of each row, in the archive's zone>/<row's session id>.jsonl.
// Failed groups go back to the front of the buffer, in order. Never throws.
async function flush($: EngineInterface, timeoutMs: number): Promise<void> {
  sizeFlushQueued = false
  if (!buf.length) return
  const batch = buf
  buf = []
  bufChars = 0
  inflight = batch
  if (dropped > 0) {
    const last = batch[batch.length - 1]!
    batch.push({ day: last.day, sid: last.sid, line: JSON.stringify({ t: lastT, event: 'mirror_dropped', count: dropped, reason: 'buffer cap' }) + '\n' })
    dropped = 0
  }
  let root: string
  try { root = (await cfgOf($)).archiveRoot } catch (err) { inflight = []; requeue(batch); noteFailure($, `config: ${why(err)}`); return }
  if (!root) { inflight = []; requeue(batch); noteFailure($, 'no archive folder: archiveRoot starts with ~ and HOME is not set'); return }
  await writeZoneMarker($, root)

  const groups = new Map<string, Entry[]>()
  for (const x of batch) {
    const k = `${x.day}\u0000${x.sid}\u0000${x.suffix ?? ''}`
    const g = groups.get(k)
    if (g) g.push(x); else groups.set(k, [x])
  }
  const failed: Entry[] = []
  let reason = ''
  for (const entries of groups.values()) {
    const dir = `${root}/${safe(entries[0]!.day)}`
    const file = `${dir}/${safe(entries[0]!.sid)}${entries[0]!.suffix ?? ''}.jsonl`
    try {
      const r = await appendLines($, dir, file, entries.map(x => x.line).join(''), timeoutMs)
      if (r.exitCode !== 0) { failed.push(...entries); reason = r.stderr.trim().slice(0, 200) || `exit ${r.exitCode}` }
    } catch (err) {
      // A timeout may have written part of the group; the retry can then duplicate rows (see the uuid TODO below).
      failed.push(...entries)
      reason = why(err)
    }
  }
  inflight = []
  if (failed.length) { requeue(failed); noteFailure($, reason) } else noteSuccess($)
}

// Records the day-folder zone once, in the archive root, if it has none yet. Best effort; never throws.
async function writeZoneMarker($: EngineInterface, root: string): Promise<void> {
  const z = zoneToStore
  if (!z) return
  zoneToStore = undefined
  try {
    const file = `${root}/${ARCHIVE_MARKER}`
    const text = JSON.stringify({ timezone: z, format: 1 }) + '\n'
    if (isWin) { if (!(await $.fs.exists(file))) await $.fs.write(file, text) }
    else await $.process.run(['sh', '-c', 'umask 077; mkdir -p -m 700 "$1" && { [ -e "$2" ] || cat > "$2"; }', 'sh', root, file], { stdin: text, timeoutMs: RUN_TIMEOUT_MS })
  } catch {}
}

// ---------- host files ----------
// Linux and macOS: sh, cat and ripgrep, as before. Windows: the mirror is appended through PowerShell (always present,
// a byte copy of stdin, no shell quoting: the script travels base64-encoded), and the archive is listed, searched and
// read through $.fs. Every helper answers the same shape on both, so the callers do not know which host they are on.

// Appends `text` to `file`, creating `dir` first. POSIX: umask 077 so new folders are 0700 and new files 0600,
// whatever the inherited umask. Windows: the user's profile folder is private to the account already.
async function appendLines($: EngineInterface, dir: string, file: string, text: string, timeoutMs: number): Promise<{ exitCode: number; stderr: string }> {
  if (!isWin) return $.process.run(['sh', '-c', 'umask 077; mkdir -p -m 700 "$1" && cat >> "$2"', 'sh', dir, file], { stdin: text, timeoutMs })
  const script = [
    "$ErrorActionPreference = 'Stop'",
    `[void][System.IO.Directory]::CreateDirectory('${psQuote(dir)}')`,
    '$s = [System.Console]::OpenStandardInput()',
    `$f = [System.IO.File]::Open('${psQuote(file)}', [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)`,
    'try { $s.CopyTo($f) } finally { $f.Dispose() }',
  ].join('\n')
  return $.process.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', b64utf16le(script)], { stdin: text, timeoutMs })
}

function psQuote(s: string): string { return s.replace(/'/g, "''") }

// PowerShell's -EncodedCommand takes the script as base64 of its UTF-16LE bytes. Written out here: the module runs
// with neither Node nor a DOM, so no Buffer and no btoa.
export function b64utf16le(s: string): string {
  const T = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  const bytes: number[] = []
  for (let i = 0; i < s.length; i++) { const c = s.charCodeAt(i); bytes.push(c & 0xff, c >> 8) }
  let out = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i]!, b = bytes[i + 1], c = bytes[i + 2]
    const n = (a << 16) | ((b ?? 0) << 8) | (c ?? 0)
    out += T[(n >> 18) & 63]! + T[(n >> 12) & 63]! + (b === undefined ? '=' : T[(n >> 6) & 63]!) + (c === undefined ? '=' : T[n & 63]!)
  }
  return out
}

// Reads whole files, one after another, in the shape `cat` answers: POSIX runs cat; Windows reads each through $.fs
// (a file over its 4 MiB read limit counts as truncated, any other failure as a non-zero exit).
async function readWhole($: EngineInterface, paths: string[]): Promise<{ stdout: string; exitCode: number; isStdoutTruncated: boolean }> {
  if (!isWin) return $.process.run(['cat', '--', ...paths], { timeoutMs: RECALL_RUN_MS })
  let stdout = ''
  let exitCode = 0
  let isStdoutTruncated = false
  for (const p of paths) {
    try {
      const t = String(await $.fs.read(p))
      stdout += t.endsWith('\n') || !t ? t : `${t}\n`
    } catch (err) {
      if (/MiB|too large|exceeds/i.test(why(err))) isStdoutTruncated = true; else exitCode = 1
    }
  }
  return { stdout, exitCode, isStdoutTruncated }
}

// Windows: <root>/<day>/*.jsonl through $.fs.list, two levels, the shape archiveFiles answers.
async function archiveFilesWin($: EngineInterface, root: string): Promise<{ files: { day: string; name: string; path: string }[]; missing?: boolean; error?: string }> {
  let days
  try { days = await $.fs.list(root) } catch (err) {
    const w = why(err)
    return /ENOENT|no such file|not found|does not exist/i.test(w) ? { files: [], missing: true } : { files: [], error: w.slice(0, 200) }
  }
  const files: { day: string; name: string; path: string }[] = []
  for (const d of days) {
    if (d.kind !== 'dir') continue
    let ents
    try { ents = await $.fs.list(`${root}/${d.name}`) } catch { continue }
    for (const f of ents) if (f.kind === 'file' && f.name.endsWith('.jsonl')) files.push({ day: d.name, name: f.name, path: `${root}/${d.name}/${f.name}` })
  }
  return { files }
}

// Windows: the lines of the given files that hold any pattern (fixed strings, case-insensitive), read through $.fs.
// Capped at 4 MiB of matched text, as rg's stdout was.
async function grepLinesWin($: EngineInterface, patterns: string[], paths: string[]): Promise<{ lines: string[]; cut: boolean; error?: string }> {
  const needles = patterns.map(p => p.toLowerCase()).filter(Boolean)
  const lines: string[] = []
  let used = 0
  let cut = false
  let error: string | undefined
  for (const p of paths) {
    let text: string
    try { text = String(await $.fs.read(p)) } catch (err) {
      const w = why(err)
      if (!/ENOENT|no such file|not found|does not exist/i.test(w)) error ??= w.slice(0, 200)
      continue
    }
    for (const line of text.split('\n')) {
      if (!line) continue
      if (needles.length) { const low = line.toLowerCase(); if (!needles.some(n => low.includes(n))) continue }
      if (used + line.length > GREP_OUT_CAP) { cut = true; break }
      lines.push(line)
      used += line.length + 1
    }
    if (cut) break
  }
  return { lines, cut, error }
}

// Queues a flush behind any in flight, so batches never interleave or reorder.
function scheduleFlush($: EngineInterface, timeoutMs: number = RUN_TIMEOUT_MS): Promise<void> {
  chain = chain.then(() => flush($, timeoutMs)).catch(() => {})
  return chain
}

// Detaches a flush from the current dispatch (the scribe's pattern), so the hook returns at once.
function flushSoon($: EngineInterface) {
  try { $.clock.after(1, () => { void scheduleFlush($) }) } catch { void scheduleFlush($) }
}

// One periodic flush timer per module environment. Started at session.start, and lazily on the first row
// after a hot reload (session.start does not fire again then).
function ensureTimer($: EngineInterface, restart: boolean) {
  if (timer && !restart) return
  try { timer?.cancel() } catch {}
  timer = undefined
  try { timer = $.clock.every(FLUSH_EVERY_MS, () => { void scheduleFlush($) }) } catch {}
}

// ---------- redaction ----------
// The mirror is private data and must stay out of any public repository; redaction is the last line of defence, not a
// licence to share it. redact() runs over the WHOLE string before any cut (a key split by a cut would slip past a
// pattern), and replaces the secret value only, keeping a key name before it. Over-redaction is accepted: the record
// loses a few characters; a leaked key can only be rotated.
const REDACT_SCAN_MAX = 512 * 1024 // longer strings are cut here first (every caller keeps far less than this)
// Private keys are found by a linear scan, not a BEGIN...END regex (that backtracks quadratically on many BEGIN lines
// with no END: 500 KB took over a minute). BEGIN, then the first END after it, then on; no END at all: the rest goes.
const PRIVATE_KEY_MARK = '[redacted:private-key]'
const PK_BEGIN = /-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----/g
const PK_END = /-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----/g
export function scrubPrivateKeys(s: string): string {
  if (!s.includes('PRIVATE KEY')) return s
  let out = '', pos = 0
  for (;;) {
    PK_BEGIN.lastIndex = pos
    const b = PK_BEGIN.exec(s)
    if (!b) break
    out += s.slice(pos, b.index) + PRIVATE_KEY_MARK
    PK_END.lastIndex = b.index + b[0].length
    const e = PK_END.exec(s)
    if (!e) return out
    pos = e.index + e[0].length
  }
  return out + s.slice(pos)
}

// The twin of PATTERNS in resonant_archive/redact.py: change both together. A second pass over redacted text is meant to
// change nothing (the tests check it for every shape listed); it is not proven for arbitrary input.
const FLAG_VALUE = String.raw`('[^']*'|"[^"]*"|[^\s"']+)`
const REDACTIONS: readonly [RegExp, string][] = [
  [/\bsk-(?:ant-|or-v1-|proj-)?[A-Za-z0-9_-]{20,}/g, '[redacted:api-key]'],
  [/\bxai-[A-Za-z0-9_-]{16,}/g, '[redacted:api-key]'],
  [/\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}/g, '[redacted:api-key]'],
  [/\bwhsec_[A-Za-z0-9]{16,}/g, '[redacted:api-key]'],
  [/\bnpm_[A-Za-z0-9]{30,}/g, '[redacted:npm-token]'],
  [/\bhf_[A-Za-z0-9]{30,}/g, '[redacted:hf-token]'],
  [/\bgl(?:pat|ptt|rt|dt|oas|soat|ft|imt)-[A-Za-z0-9_-]{20,}/g, '[redacted:gitlab-token]'],
  [/\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}/g, '[redacted:sendgrid-key]'],
  [/\bgh[pousr]_[A-Za-z0-9]{30,}/g, '[redacted:github-token]'],
  [/\bgithub_pat_[A-Za-z0-9_]{40,}/g, '[redacted:github-token]'],
  [/\b(?:AKIA|ASIA)[0-9A-Z]{16}/g, '[redacted:aws-key]'],
  [/(aws[ _-]?secret[ _-]?(?:access[ _-]?)?key["']?[ \t]*[:=][ \t]*["']?)(?!\[redacted)[A-Za-z0-9/+=]{16,}/gi, '$1[redacted:secret]'],
  [/(\b(?:AccountKey|SharedAccessKey|Pwd)[ \t]*=[ \t]*)(?!\[redacted)[^;\s"']{8,}/gi, '$1[redacted:secret]'],
  [/\bAIza[0-9A-Za-z_-]{35}/g, '[redacted:google-key]'],
  [/\bxox[abprs]-[A-Za-z0-9-]{10,}/g, '[redacted:slack-token]'],
  [/https?:\/\/hooks\.slack\.com\/(?:services|workflows|triggers|actions)\/[A-Za-z0-9/_-]{8,}/g, '[redacted:webhook]'],
  [/https?:\/\/(?:[a-z0-9-]+\.)?discord(?:app)?\.com\/api\/(?:v\d+\/)?webhooks\/\d+\/[A-Za-z0-9._-]{20,}/g, '[redacted:webhook]'],
  [/\b[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}/g, '[redacted:discord-token]'],
  [/\b(?:AC|SK)[0-9a-f]{32}\b/g, '[redacted:twilio]'],
  [/\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}/g, '[redacted:jwt]'],
  [/\b\d{8,10}:AA[A-Za-z0-9_-]{30,}/g, '[redacted:telegram-token]'],
  // any scheme://user:password@host; the password may hold '@' (the last one before the host ends it) but not '/'
  [/\b([a-z][a-z0-9+.-]{1,20}:\/\/[^:\/\s@"'<>]*:)[^\s\/"'<>]+@/gi, '$1[redacted:password]@'],
  [/(?<![\w-])((?:-u|--user)(?:[ \t]+|=)["']?)(?!\[redacted)[^\s:"']{1,200}:[^\s"']+/g, '$1[redacted:credentials]'],
  [/(?<![\w-])(--(?!no-)(?:[a-z0-9]+-)*(?:password|passwd|pass|passphrase|pwd|token|secret|api-key|apikey)(?:=|[ \t]+)["']?)(?!-|\[redacted)[^\s"']{4,}/gi, '$1[redacted:secret]'],
  [new RegExp(String.raw`(\bsshpass[ \t]+-p[ \t]*)(?!\[redacted)` + FLAG_VALUE, 'g'), '$1[redacted:password]'],
  [new RegExp(String.raw`(\bmysql(?:dump|admin|pump|sh)?\b[^\n]{0,200}?[ \t]-p)(?![\s\[])` + FLAG_VALUE, 'g'), '$1[redacted:password]'],
  [new RegExp(String.raw`(\bredis-cli\b[^\n]{0,200}?[ \t]-a[ \t]+)(?!\[redacted)` + FLAG_VALUE, 'g'), '$1[redacted:password]'],
  [/\b(bearer)(\s+)[A-Za-z0-9._~+\/=-]{16,}/gi, '$1$2[redacted:token]'],
  [/\b(basic)(\s+)(?=[A-Za-z0-9+\/=]*\d)[A-Za-z0-9+\/]{16,}={0,2}/gi, '$1$2[redacted:token]'],
  [/(\bauthorization["']?[ \t]*[:=][ \t]*["']?(?:(?:token|basic|bearer|digest|apikey|api-key|key)[ \t]+)?)(?!\[redacted|(?:token|basic|bearer|digest)[ \t])[^\s"',;}]{8,}/gi, '$1[redacted:secret]'],
  [/\/mcp\/[A-Za-z0-9_-]{16,}/g, '/mcp/[redacted:secret-path]'],
  [/\bmcp_[0-9a-f]{8,}/g, '[redacted:secret-path]'],
  [/([?&](?:key|token|api_key|apikey|access_token|sig|secret)=)[^&\s"'<>]+/gi, '$1[redacted]'],
  [/^([ \t]*(?:export\s+)?[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS)[A-Z0-9_]*\s*=\s*).+$/gm, '$1[redacted:secret]'],
  // name [: or =] value, the name anywhere in the word (DB_PASSWORD, "apiKey"); placeholders and short values stay
  [/((?:secret[ _-]?access[ _-]?key|secret[ _-]?key|access[ _-]?key|api[ _-]?key|private[ _-]?key|client[ _-]?secret|secret|password|passwd|passphrase|token|credentials?)[A-Za-z0-9_-]{0,30}["']?[ \t]*[:=][ \t]*["']?)(?!\[redacted|[$<{%])[^\s"',;}\]]{8,}/gi, '$1[redacted:secret]'],
  // a short value after an explicit password key (the rule above wants 8+); a type name or a call is left, and so is a
  // path after `pwd` (the current directory in an environment listing)
  [/((?:password|passwd)[A-Za-z0-9_-]{0,30}["']?[ \t]*[:=][ \t]*["']?|pwd["']?[ \t]*[:=][ \t]*["']?(?![\/~]))(?!\[redacted|[$<{%])(?!(?:none|null|nil|true|false|undefined|string|str|bool|boolean|int|integer|number|input|getpass|prompt|hidden|text|field|value|optional|required|default|self|args|env|os|config|process|form|request|user|data|dict|list|type)(?![\w]))(?![A-Za-z_][\w.]*[(\[])[^\s"',;}\]]{4,}/gi, '$1[redacted:secret]'],
  // ~/.pgpass lines: host:port:database:user:password
  [/^([^\s:#]+:(?:\d{1,5}|\*):[^\s:]+:[^\s:]+:)\S+$/gm, '$1[redacted:password]'],
  // prose: "the password is hunter2"
  [/\b((?:password|passwd|passphrase|passcode|secret|api[ _-]key|token)s?(?:[ \t]+for[ \t]+[\w.@-]{1,40})?[ \t]+(?:is|was|=)[ \t]*:?[ \t]*["'`]?)(?!\[redacted)(?!(?:the|an?|not|now|set|stored|required|wrong|correct|incorrect|invalid|valid|empty|missing|expired|different|same|here|in|on|for|to|used|sent|ok|fine|right|changed|reset|updated|saved|unchanged|supposed|needed|still|also|only|just|being|that|this|there)(?![\w-]))[^\s"'`,;]{6,}/gi, '$1[redacted:secret]'],
  // last: a long token (40+: shorter ones were mostly identifiers in code) mixing upper, lower and digits, not hex-only (commit ids, UUIDs and paths pass)
  [/(?<![A-Za-z0-9_+-])(?=[A-Za-z0-9_+-]*[a-z])(?=[A-Za-z0-9_+-]*[A-Z])(?=[A-Za-z0-9_+-]*\d)[A-Za-z0-9_+-]{40,}/g, '[redacted:secret]'],
]

// Pure. Never throws on a string.
export function redact(input: string): string {
  let s = String(input ?? '')
  if (!s) return s
  if (s.length > REDACT_SCAN_MAX) s = s.slice(0, REDACT_SCAN_MAX)
  s = scrubPrivateKeys(s)
  for (const [re, to] of REDACTIONS) { re.lastIndex = 0; s = s.replace(re, to) }
  return s
}

// Every string inside a tool input, redacted (so a multi-line command meets the line patterns before JSON escapes it).
function redactDeep(v: unknown, depth = 0): unknown {
  if (typeof v === 'string') return redact(v)
  if (depth > 6 || v === null || typeof v !== 'object') return v
  if (Array.isArray(v)) return v.map(x => redactDeep(x, depth + 1))
  const o: Record<string, unknown> = {}
  for (const [k, x] of Object.entries(v as Record<string, unknown>)) o[k] = redactDeep(x, depth + 1)
  return o
}

// Cuts a string already redacted, saying how much went.
function capText(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max)}[…cut ${s.length - max} chars]`
}

// Second layer: a tool call that reads a secret-bearing source has its result withheld whole. The tool_use row
// reaches the mirror before its result row, so the call is remembered by id (the last few hundred).
// Best effort, not a guarantee: the common shapes only (files by name, commands that print an environment or a
// credential, searches whose pattern or path names a secret). The twin of resonant_archive/secret_sources.py: change
// both together, and keep the case lists in both test suites the same.
const SECRET_FILE_SOURCE = String.raw`[/.](?:env(?:\.[\w-]{1,40}){0,4}|dev\.vars|mcp\.json|pem|key|netrc|pgpass|environ|p12|pfx|jks|keystore|ppk|kdbx|htpasswd)$|(?:^|[/\\])\.env[\w.~*?-]*$|(?:^|[/\\])\.(?:npmrc|pypirc|git-credentials|vault-token|bash_history|zsh_history)$|\.docker/config\.json$|\.kube/config$|(?:^|[/.\\])(?:credentials(?:\.json)?|id_[a-z0-9]+)$|(?:^|[/\\])secrets?\.[\w.*-]+$|terraform\.tfstate[\w.-]{0,40}$|\.tfvars(?:\.json)?$|/proc/[^/\s]+/(?:environ|cmdline)$|/etc/g?shadow$|(?:^|[/\\])[\w.-]{0,100}(?:service[-_]?account|application_default_credentials)[\w.-]{0,100}\.json$|(?:^|[/\\])client_secrets?\.json$|/private/`
const SECRET_HINT_SOURCE = String.raw`pass(?:word|wd|phrase|code)|secret|token|api[_ -]?key|credential|private[_ -]?key|bearer|\.env\b|\benv\b|id_rsa|id_ed25519|\.pem\b|\.pfx|\.p12|\.npmrc|\.netrc|\.pgpass|BEGIN[ \w]{0,40}PRIVATE`
const SECRET_PROGRAM_SOURCE = String.raw`env(?:\s|$)|\S*/env\s*$|\S*/env\s+-\S+|(?:\S*/)?(?:printenv|ps)(?:\s|$)|(?:\S*/)?pm2\s+(?:env|show|describe|prettylist|jlist)\b|set\s*$|(?:export|declare|typeset)(?:\s+-[A-Za-z]+)?\s*$|compgen\s+-[ve]\b|history(?:\s|$)|pgrep\s+-[A-Za-z]*[af]|(?:docker|podman)(?:\s+compose|-compose)?\s+(?:config|inspect)\b|(?:docker|podman)\s+(?:exec|run)\b[^\n]*\s(?:env|printenv)(?:\s|$)|kubectl\s+(?:(?:get|describe)\s+secrets?\b|config\s+view\b)|systemctl\s+(?:--?\S+\s+)*(?:show|cat)\b|ssh-add\s+-[A-Za-z]*[Ll]|gh\s+auth\s+(?:token\b|status\b[^\n]*--show-token)|gcloud\s+auth\s+(?:print-access-token|print-identity-token|application-default\s+print-access-token)|aws\s+(?:configure\s+(?:get|list|export-credentials)|secretsmanager\s+get-secret-value|ssm\s+get-parameters?(?:-by-path)?|sts\s+get-session-token)|vault\s+(?:read|kv\s+get|token\s+lookup)|op\s+(?:read|item\s+get|inject)`
const SECRET_FILE = new RegExp(SECRET_FILE_SOURCE, 'i')
const SECRET_HINT = new RegExp(SECRET_HINT_SOURCE, 'i')
const SECRET_PROGRAM = new RegExp('^(?:' + SECRET_PROGRAM_SOURCE + ')')
const SEARCH_COMMAND = new RegExp(String.raw`^(?:\S*/)?(?:grep|egrep|fgrep|zgrep|rg|ag|ack|find|locate|fd)(?:\s|$)`)
// Prefixes that run the next command, peeled off one at a time (sudo is tried with and without an option argument).
const PEELERS: readonly RegExp[] = [
  new RegExp(String.raw`^[A-Za-z_]\w*=(?:'[^']*'|\"[^\"]*\"|\S*)\s+`),
  new RegExp(String.raw`^(?:exec|time|command|builtin|nohup|setsid)\s+(?:-\S+\s+)*`),
  new RegExp(String.raw`^(?:sudo|doas)\s+(?:-\S+\s+)*`),
  new RegExp(String.raw`^(?:sudo|doas)\s+(?:-\S+\s+)*\S+\s+`),
  new RegExp(String.raw`^xargs\s+(?:-\S+\s+)*`),
  new RegExp(String.raw`^(?:nice|ionice|stdbuf|timeout)\s+(?:-\S+\s+(?:\d+\s+)?)*(?:\d+[smhd]?\s+)?`),
]
const SHELL_C = new RegExp(String.raw`^(?:\S*/)?(?:ba|z|da|k|fi|a|c)?sh\s+(?:-\S+\s+)*?-[A-Za-z]*c\s+(.+)$`)
const SEGMENTS = /[;&|\n]+|\$\(|`/
const TOKENS = /[\s'"`;&|()<>=]+/
const ENV_BIN = /\/bin\/env$/
const ENV_READ = /\b(?:os\.environ|os\.getenv|process\.env|ENV\[)/
const PATH_KEYS = ['file_path', 'path', 'notebook_path']
const SEARCH_KEYS = ['pattern', 'path', 'glob', 'include', 'query']
const ASSIGN_VALUE = /^[A-Za-z_]\w*=/
const WITHHELD = '[withheld: secret-bearing source]'
const INPUT_WITHHELD = '[input withheld: secret-bearing source]'
const ROW_TEXT_MAX = 64_000
const ROW_RESULTS_MAX = 20
const RESULT_CHARS = 2000
const TOOL_INPUT_CHARS = 300
const NOTE_COUNT_MAX_CHARS = 4000
const secretUses = new Map<string, true>()
// Recall output quotes the archive back: mirrored, every recall would find the last recall first (seen in live
// testing). Its call is remembered by id like a secret one, and its output is replaced by RECALL_RESULT_NOTE.
const RECALL_RESULT_NOTE = '[recall output: not mirrored, it quotes the archive]'
// The head of every recall output (search, plain read, around, nothing found, errors), for rows mirrored before the
// fix: recall leaves them out of its own results.
const RECALL_OUTPUT_HEAD = /^recall(?: failed)?: |^recall (?:around [A-Za-z0-9-]{8,64} \(session |.*?\((?:this session|all sessions)[^)\n]*\): )/
const recallUses = new Map<string, true>()

// A loop, not a regex ending in `$`: a run of 400,000 quotes followed by one more character made that regex quadratic.
function trimSegment(seg: string): string {
  const t = seg.trim().replace(/^[({\s]+/, '')
  let e = t.length
  while (e > 0 && /[)}"'\s]/.test(t[e - 1]!)) e--
  return t.slice(0, e)
}

// The quotes and spaces around a `sh -c` body, with a loop for the same reason: `/["'\s]+$/` was quadratic on a long run
// of quotes inside the body (2 s at 50,000, 40 s at 200,000).
export function trimQuotes(s: string): string {
  const t = s.replace(/^["'\s]+/, '')
  let e = t.length
  while (e > 0 && /["'\s]/.test(t[e - 1]!)) e--
  return t.slice(0, e)
}

function segmentSecret(segment: string, depth = 0): boolean {
  const seg = trimSegment(segment)
  if (!seg || depth > 5) return false
  if (SECRET_PROGRAM.test(seg)) return true
  for (const rx of PEELERS) {
    const m = rx.exec(seg)
    if (m && segmentSecret(seg.slice(m[0].length), depth + 1)) return true
  }
  const m = SHELL_C.exec(seg)
  if (m) {
    const inner = trimQuotes(m[1]!)
    return inner.split(SEGMENTS).some(x => segmentSecret(x, depth + 1))
  }
  return false
}

// True when a tool call reads a secret-bearing file, prints the environment or a credential, or searches for a secret by
// name. Best effort (see above).
export function secretBearing(name: unknown, input: unknown): boolean {
  const o = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  for (const k of PATH_KEYS) {
    const v = o[k]
    if (typeof v === 'string' && SECRET_FILE.test(v.trim())) return true
  }
  if (typeof name === 'string' && ['grep', 'glob'].includes(name.toLowerCase())) {
    for (const k of SEARCH_KEYS) {
      const v = o[k]
      if (typeof v === 'string' && (SECRET_HINT.test(v) || SECRET_FILE.test(v.trim()))) return true
    }
  }
  const cmd = typeof o.command === 'string' ? o.command : ''
  if (cmd) {
    if (ENV_READ.test(cmd)) return true
    for (const seg of cmd.split(SEGMENTS)) {
      if (segmentSecret(seg)) return true
      const t = trimSegment(seg)
      if (SEARCH_COMMAND.test(t) && SECRET_HINT.test(t)) return true
    }
    for (const tok of cmd.split(TOKENS)) if (tok && !ENV_BIN.test(tok) && SECRET_FILE.test(tok)) return true
  }
  return false
}

// What is kept of a secret-bearing call's input: the file it names, or for a command only the program (and the names of
// any secret files it mentions); never arguments, content, new_string, old_string or a heredoc body.
export function secretCallSummary(_name: unknown, input: unknown): string {
  const o = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  for (const k of PATH_KEYS) {
    const v = o[k]
    if (typeof v === 'string' && v.trim()) return v.trim().slice(0, 300)
  }
  const cmd = typeof o.command === 'string' ? o.command : ''
  if (cmd) {
    const first = (cmd.trimStart().split(SEGMENTS)[0] ?? '').split(/[<>]/)[0]!.trim()
    const words = first.split(/\s+/).filter(w => w && !ASSIGN_VALUE.test(w))
    const prog = (words[0] ?? '').slice(0, 80)
    const files = cmd.split(TOKENS).filter(t => t && !ENV_BIN.test(t) && SECRET_FILE.test(t))
    let out = prog + (words.length > 1 || cmd.trim().length > prog.length ? ' [arguments withheld]' : '')
    if (files.length) out += ' ' + [...new Set(files.slice(0, 5).map(f => f.slice(0, 200)))].join(' ')
    return out.trim() || INPUT_WITHHELD
  }
  return INPUT_WITHHELD
}

export function blockText(content: unknown): { text: string; tools: string[]; results: string[] } {
  const text: string[] = [], tools: string[] = [], results: string[] = []
  let extra = 0
  for (const b of (Array.isArray(content) ? content : []) as any[]) {
    if (b?.type === 'text' && b.text) text.push(String(b.text))
    else if (b?.type === 'tool_use') {
      const secret = secretBearing(b.name, b.input)
      if (secret && typeof b.id === 'string') {
        secretUses.set(b.id, true)
        if (secretUses.size > 512) secretUses.delete(secretUses.keys().next().value as string)
      }
      if (recallNames.includes(String(b.name)) && typeof b.id === 'string') {
        recallUses.set(b.id, true)
        if (recallUses.size > 256) recallUses.delete(recallUses.keys().next().value as string)
      }
      tools.push(secret
        ? `${b.name}(${redact(secretCallSummary(b.name, b.input)).slice(0, TOOL_INPUT_CHARS)})`
        : `${b.name}(${redact(JSON.stringify(redactDeep(b.input ?? {}))).slice(0, TOOL_INPUT_CHARS)})`)
    } else if (b?.type === 'tool_result') {
      if (results.length >= ROW_RESULTS_MAX) { extra++; continue }
      if (typeof b.tool_use_id === 'string' && recallUses.has(b.tool_use_id)) { results.push(RECALL_RESULT_NOTE); continue }
      if (typeof b.tool_use_id === 'string' && secretUses.has(b.tool_use_id)) { results.push(WITHHELD); continue }
      const c = typeof b.content === 'string' ? b.content : Array.isArray(b.content) ? b.content.filter((x: any) => x?.type === 'text').map((x: any) => x.text).join('\n') : ''
      results.push(redact(String(c)).slice(0, RESULT_CHARS))
    }
  }
  if (extra) results.push(`[… ${extra} more results not kept]`)
  return { text: capText(redact(text.join('\n')), ROW_TEXT_MAX), tools, results }
}

const REMINDER_CLOSE = '</system-reminder>'
const QUEUED_HEAD = /^The user sent a new message while you were working:[ \t]*\n/
const QUEUED_NOTE = 'This is how Claude Code surfaces messages the user sends mid-turn'

// The person's words inside a prompt folded into a running turn (an attachment row named queued_command), from the
// engine's rendering. `user` false: the wrapper was not recognised, so the rendering is kept whole (never lost).
// No regex here runs to `$` over the person's words: `\s*<\/system-reminder>\s*$` and the lazy body before the engine's
// note were both quadratic on a long run of spaces or newlines (2 s and 4 s at 50,000).
export function queuedPrompt(rendered: string): { text: string; user: boolean } {
  let s = String(rendered ?? '').replace(/^\s*<system-reminder>\s*/, '')
  const end = s.trimEnd()
  if (end.endsWith(REMINDER_CLOSE)) s = end.slice(0, -REMINDER_CLOSE.length).trimEnd()
  const head = QUEUED_HEAD.exec(s)
  if (!head) return { text: s.trim(), user: false }
  const body = s.slice(head[0].length)
  return { text: body.slice(0, queuedNoteStart(body)).trim(), user: true }
}

// Where the engine's note starts: the first newline of the whitespace run before the first occurrence of the note that
// has a newline in that run (the end of the body when there is none). Each run is walked once, so this is linear.
function queuedNoteStart(body: string): number {
  for (let q = body.indexOf(QUEUED_NOTE); q !== -1; q = body.indexOf(QUEUED_NOTE, q + 1)) {
    let b = q
    while (b > 0 && /\s/.test(body[b - 1]!)) b--
    const nl = body.indexOf('\n', b)
    if (nl !== -1 && nl < q) return nl
  }
  return body.length
}

// Records one stored row. Called after next(): must never throw (the caller also wraps it).
function mirror($: EngineInterface, e: { door: string; origin: unknown }, stored: any, t: number, rowSid: string) {
  if (cfgCache && !cfgCache.mirrorEnabled) return
  if (!stored || stored.deny !== undefined) return
  const m = stored.message
  if (!m) return
  // A prompt the person types while the assistant is working is stored as an attachment row: their words, so they are kept.
  const queued = m.type === 'attachment' && m.name === 'queued_command'
  if (m.type !== 'user' && m.type !== 'assistant' && !queued) return
  const bt = blockText(m.content)
  const { tools, results } = bt
  let text = bt.text
  let queuedAs: string | undefined
  if (queued) {
    const q = queuedPrompt(text)
    text = q.text
    queuedAs = q.user ? 'prompt' : 'other'
  }
  if (!text && !tools.length && !results.length) return
  // The engine's local-command caveat is boilerplate, not conversation.
  if (m.isMeta && !tools.length && !results.length && text.trimStart().startsWith('<local-command-caveat>')) return
  if (e.door === 'command' && /^<command-name>\/(?:exit|quit)\b/.test(text.trimStart())) { exitingSid = rowSid; return }
  if (exitingSid !== undefined && exitingSid === rowSid) return // the farewell and anything after /exit
  // The memories block and its acknowledgement, as an assembled compaction put them back, and a memory fork if one
  // ever surfaced as a row: tagged, so the memory chunks and the coverage check skip them (one rule for both).
  const chron = queued ? undefined : synthKind(m.type, text)
  const row = {
    t, sid: rowSid, uuid: stored.uuid, door: e.door, chronicle: chron, queued: queuedAs,
    origin: (e.origin as { kind?: string } | undefined)?.kind, role: queued ? 'user' : (m.role ?? m.type), meta: m.isMeta ? true : undefined,
    text: (chron === 'memories' ? blockNote(text) : chron === 'fork' ? '[a memory formation prompt]' : text) || undefined,
    tools: tools.length ? tools : undefined, results: results.length ? results : undefined,
  }
  push({ day: dayOf(t), sid: rowSid, line: JSON.stringify(row) + '\n' })
  if (typeof stored.uuid === 'string' && !m.isMeta && !results.length && text && (queued ? queuedAs === 'prompt' : e.door === 'prompt')) {
    if (turnPrompts.size >= TURN_PROMPTS_MAX) turnPrompts.clear()
    turnPrompts.add(stored.uuid)
  }
  if (bufChars >= FLUSH_AT_CHARS && !sizeFlushQueued) { sizeFlushQueued = true; flushSoon($) }
  ensureTimer($, false)
  // Local-command output (/context and the like, door note) is counted at most NOTE_COUNT_MAX_CHARS: a chunk is about
  // the conversation, not a table the engine printed.
  if (memoriesOn) try { noteRow(row, e.door === 'note' ? Math.min(rowChars(m.content), NOTE_COUNT_MAX_CHARS) : rowChars(m.content)) } catch {}
}

// ---------- split compaction ----------

// A user message with words in it and no tool results. Injected text-only rows (a clock receipt, a scribe
// whisper) also pass this, which is why splitPoint adds its own pairing and turn-boundary checks.
function isPrompt(m: SessionMessage): boolean {
  return m.role === 'user' && !(m.toolResults && m.toolResults.length) && String(m.text ?? '').trim().length > 0
}

// Rough token estimate (~3.6 characters per token for our mix of prose and code).
// A tool use counts its input; its outcome counts once, on the result row, or on the use when no result row exists.
function estimate(m: SessionMessage, resultAt: Map<string, number>): number {
  let n = String(m.text ?? '').length
  for (const u of m.toolUses ?? []) {
    n += JSON.stringify(u.input ?? {}).length
    if (!resultAt.has(u.tool_use_id)) n += String(u.text ?? '').length
  }
  for (const r of m.toolResults ?? []) n += String(r.text ?? '').length
  return Math.ceil(n / 3.6)
}

// Where the raw tail starts, or -1. A cut at i is valid only when:
//  (a) msgs[i] is a prompt-shaped user message,
//  (b) no tool_use before i has its tool_result at or after i (nothing orphaned either side),
//  (c) msgs[i-1] is an assistant message with no tool uses (a real turn boundary).
// Picks the smallest valid tail of at least `target` tokens; if that would pass `cap`, the largest valid tail under it.
export function splitPoint(msgs: readonly SessionMessage[], target: number, cap: number): number {
  const resultAt = new Map<string, number>()
  msgs.forEach((m, i) => { for (const r of m.toolResults ?? []) resultAt.set(r.tool_use_id, i) })

  // open[i] > 0 when some pair straddles a cut at i (use at j < i, result at k >= i). Difference array over [j+1, k].
  const diff = new Int32Array(msgs.length + 2)
  msgs.forEach((m, j) => {
    for (const u of m.toolUses ?? []) {
      const k = resultAt.get(u.tool_use_id)
      if (k !== undefined && k > j) { diff[j + 1]!++; diff[k + 1]!-- }
    }
  })
  const open = new Int32Array(msgs.length + 1)
  for (let i = 0, s = 0; i <= msgs.length; i++) { s += diff[i]!; open[i] = s }

  const valid = (i: number) => {
    const prev = msgs[i - 1]
    return isPrompt(msgs[i]!) && open[i] === 0 && !!prev && prev.role === 'assistant' && !(prev.toolUses && prev.toolUses.length)
  }

  let acc = 0
  let best = -1
  for (let i = msgs.length - 1; i >= 1; i--) {
    acc += estimate(msgs[i]!, resultAt)
    if (acc > cap) break
    if (valid(i)) {
      best = i
      if (acc >= target) return i
    }
  }
  return best
}

// ---------- recall ----------
// A summary alone loses things; searching back into the record finds them.
// Archive layout recall reads (the mirror format, documented in README.md):
//   <archiveRoot>/<YYYY-MM-DD in the archive's zone>/<sid>.jsonl   rows, plus event lines (compaction carries `summary`)
//   <archiveRoot>/<YYYY-MM-DD in the archive's zone>/<sid>.memories.jsonl   memories (step 2): {t, event:"memory", level, sid, text, ...}
// A line is classified by its content, never by its file name, so a memory line in either file is found.

type RecallInput = {
  query?: unknown; from?: unknown; to?: unknown; session?: unknown; kind?: unknown; limit?: unknown; around?: unknown
  // source archive
  source?: unknown; action?: unknown; id?: unknown; memory?: unknown; collection?: unknown; speaker?: unknown; context?: unknown
  hidden?: unknown; mode?: unknown; before?: unknown; after?: unknown; k?: unknown; recent?: unknown; memories?: unknown
}
type Kind = 'row' | 'summary' | 'memory'
type Item = { kind: Kind; t: number; sid: string; uuid?: string; label: string; parts: { tag: string; s: string }[] }
type Bound = { day?: string; ms?: number }

const RECALL_OUT_CAP = 12_000
const RECALL_SNIPPET = 400
const RECALL_MAX_CANDIDATES = 3000
const RECALL_FILES_PER_RUN = 300
const RECALL_RUN_MS = 20_000
// The recall tool as the model sees it. Built from the settings at session start: with no archive command only the
// session fields are offered (and the model is told so); with one, the archive fields and actions are added.
export function recallSpec(hasArchive: boolean): { description: string; inputSchema: Record<string, unknown> } {
  const sessionProps = {
    query: { type: 'string', description: 'Words to look for, in any order; double quotes for an exact phrase.' },
    from: { type: 'string', description: 'Start, YYYY-MM-DD (a day in the archive\'s time zone) or an ISO time.' },
    to: { type: 'string', description: 'End, inclusive, YYYY-MM-DD or an ISO time.' },
    session: { type: 'string', enum: ['current', 'all'], description: 'Only this session, or all (default all).' },
    kind: { type: 'string', enum: ['rows', 'memories', 'summaries', 'all'], description: 'What to search (default all).' },
    limit: { type: 'number', description: 'Hits to return (default 12, max 40). With around: rows each side.' },
    around: { type: 'string', description: 'A row uuid from an earlier hit: shows the rows around it.' },
  }
  const sessionText = 'Search the record of this and earlier sessions kept on disk by chronicle: every row, compaction summaries and memories, newest first. Arguments: query (words, any order; double quotes for an exact phrase), from/to (YYYY-MM-DD or ISO time), session (current or all), kind (rows, memories, summaries or all), limit, around=<row uuid> (a stretch of rows around an earlier hit). Use it to bring back the exact wording of something a compaction summary lost.'
  if (!hasArchive) {
    return {
      description: `${sessionText} No archive of older conversations is set up, so this searches the session record only.`,
      inputSchema: { type: 'object', properties: sessionProps },
    }
  }
  return {
    description: `Search the record of my past conversations. Two sources.\n\nsource archive (the default): the archive of every conversation imported into it, by meaning as well as by words. action:\n- search (default): query is required. Hybrid of full text, meaning (vectors) and exact anchors (paths, urls, ids, quoted phrases), fused by rank. Each hit gives its id, source, time, conversation title, speaker, which signals found it, and a snippet. mode narrows to one signal: fts, vector or anchor.\n- around: id = a hit's id. Reads that conversation around it (before/after: messages each side, default 4).\n- overview: from and to (YYYY-MM-DD) required. What a stretch held: counts by source and speaker, the busiest conversations, and chronicle memories in range if the archive holds any.\n- drill: memory = a #index from overview (or a memory's from_uuid). The archive messages that memory was written from.\n- associate: id = a hit's id. Its nearest kin by meaning in other conversations.\nFilters for search (and associate): collection (source labels, comma-separated), speaker (user, agent, assistant, peer, system, tool), context (free tags), from/to as YYYY-MM-DD; limit (default 10, max 40). Tool and system rows are left out unless hidden is true. The archive may leave out the most recent hours of this live conversation, which is already in context, unless recent is true; its footer says how many.\n\nAsking about time: search ranks by meaning and words, never by date, so for "when", "first" or "last time" questions set from/to. First: search an early window, read the hit dates, then narrow the window until the earliest real match stays put; never call the top hit the first. Last: a window ending today. A stretch you can't place: overview over from/to.\n\nsource session: ${sessionText} Use it for what happened minutes ago, which the archive may not have loaded yet.`,
    inputSchema: {
      type: 'object',
      properties: {
        source: { type: 'string', enum: ['archive', 'session'], description: 'archive (default): the whole archive. session: the record of this chronicle mirror.' },
        action: { type: 'string', enum: ['search', 'around', 'overview', 'drill', 'associate'], description: 'archive only. Default search.' },
        ...sessionProps,
        query: { type: 'string', description: 'search: words or a phrase. session source: keywords (double quotes for an exact phrase).' },
        id: { type: 'string', description: 'around / associate: a message id from an archive hit.' },
        memory: { type: 'string', description: 'drill: a memory #index from overview, or its from_uuid.' },
        collection: { type: 'string', description: 'archive: comma-separated source labels to search (as the archive lists them).' },
        speaker: { type: 'string', description: 'archive search: comma-separated: user, agent, assistant, peer, system, tool.' },
        context: { type: 'string', description: 'archive search: comma-separated context tags.' },
        hidden: { type: 'boolean', description: 'archive: include tool/system rows (default false).' },
        mode: { type: 'string', enum: ['hybrid', 'fts', 'vector', 'anchor'], description: 'archive search: one signal only (default hybrid).' },
        limit: { type: 'number', description: 'Hits to return (archive default 10, session default 12; max 40). session with around: rows each side.' },
        before: { type: 'number', description: 'archive around: messages before (default 4).' },
        after: { type: 'number', description: 'archive around: messages after (default 4).' },
        k: { type: 'number', description: 'archive associate: neighbours (default 8, max 20).' },
        recent: { type: 'boolean', description: 'archive search/associate: include the most recent hours too (default false).' },
        memories: { type: 'string', enum: ['include', 'only', 'none'], description: 'archive overview: list memories (include, default), only them, or none.' },
        around: { type: 'string', description: 'session source: a row uuid from an earlier hit. (archive: same as id with action around.)' },
      },
    },
  }
}

let recallWarned = false

function warnOnce($: EngineInterface, line: string) {
  try { $.ui.log(`chronicle: ${line}`, { to: 'debug' }) } catch {}
  if (recallWarned) return
  recallWarned = true
  try { $.ui.log(`chronicle: ${line}`) } catch {}
}

function flat(s: string): string {
  return s.replace(/\s+/g, ' ').trim()
}

// The text of a JSON string value as it sits inside a JSON line (quotes, backslashes, control characters escaped).
// JSON escapes one character at a time, so a substring of the text is a substring of its escaped form: rg on this is a superset.
export function jsonEscaped(s: string): string {
  return JSON.stringify(s).slice(1, -1)
}

function zoneParts(ms: number): Record<string, string> {
  const p = new Intl.DateTimeFormat('en-GB', { timeZone: zone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).formatToParts(new Date(ms))
  const o: Record<string, string> = {}
  for (const x of p) o[x.type] = x.value
  return o
}

function stampOf(ms: number): string {
  if (!ms) return 'undated'
  const o = zoneParts(ms)
  return `${o.year}-${o.month}-${o.day} ${o.hour}:${o.minute}`
}

// The zone's wall-clock time minus UTC at this instant, in ms.
function zoneOffset(ms: number): number {
  const o = zoneParts(ms)
  return Date.UTC(+o.year!, +o.month! - 1, +o.day!, +o.hour!, +o.minute!, +o.second!) - (ms - (((ms % 1000) + 1000) % 1000))
}

// A bound from YYYY-MM-DD (a whole day in the archive's zone, matched on the folder) or an ISO time (in that zone when it names no offset).
export function parseBound(raw: unknown): Bound | string | undefined {
  if (raw === undefined || raw === null || String(raw).trim() === '') return undefined
  const s = String(raw).trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return { day: s }
  const iso = s.replace(' ', 'T')
  let ms: number
  if (/(Z|[+-]\d{2}:?\d{2})$/i.test(iso)) ms = Date.parse(iso)
  else {
    const asUtc = Date.parse(`${iso}Z`)
    ms = asUtc
    if (Number.isFinite(asUtc)) {
      ms = asUtc - zoneOffset(asUtc)
      ms = asUtc - zoneOffset(ms)
    }
  }
  if (!Number.isFinite(ms)) return `could not read the date "${s.slice(0, 40)}" (use YYYY-MM-DD or an ISO time)`
  return { day: dayOf(ms), ms }
}

function rowLabel(o: any): string {
  const role = String(o.role ?? '')
  const door = String(o.door ?? '')
  const origin = o.origin ? String(o.origin) : ''
  if (role === 'assistant') return labels.agent
  if (o.queued === 'prompt') return `${labels.user} (mid-turn)`
  if (o.queued) return 'queued message'
  if (door === 'prompt' && (origin === 'composer' || origin === 'bridge' || origin === '')) return labels.user
  if (door === 'tool-result') return 'tool result'
  if (door === 'tool-message') return 'tool message'
  return `${role || 'row'} ${door}${origin ? ` (${origin})` : ''}${o.meta ? ' meta' : ''}`
}

// One archive line, or undefined for a line recall does not show (a mirror_dropped note, a cut or broken line).
export function toItem(line: string): Item | undefined {
  let o: any
  try { o = JSON.parse(line) } catch { return undefined }
  if (!o || typeof o !== 'object') return undefined
  const t = Number(o.t) || 0
  const sid = String(o.sid ?? o.session ?? '')
  if (o.event === 'compaction') {
    if (typeof o.summary !== 'string' || !o.summary) return undefined
    return { kind: 'summary', t, sid, label: `compaction summary (${o.trigger ?? 'compaction'}; ${o.summarized ?? '?'} rows summarized, ${o.kept_raw ?? '?'} kept raw)`, parts: [{ tag: '', s: o.summary }] }
  }
  if (o.event === 'memory') {
    if (typeof o.text !== 'string' || !o.text) return undefined
    const span = o.t_from && o.t_to ? `, ${stampOf(Number(o.t_from))} to ${stampOf(Number(o.t_to))}` : ''
    return { kind: 'memory', t, sid, label: `L${o.level ?? 1} memory${span}`, parts: [{ tag: '', s: o.text }] }
  }
  if (o.event !== undefined || typeof o.uuid !== 'string') return undefined
  const parts: { tag: string; s: string }[] = []
  if (typeof o.text === 'string' && o.text) parts.push({ tag: '', s: o.text })
  // The assistant's own recall calls and their output are not searched or shown: they only echo the archive (the person's prompt asking
  // for a recall stays, those are their words).
  for (const x of Array.isArray(o.tools) ? o.tools : []) { const s = String(x); if (recallNames.some(n => s.startsWith(`${n}(`))) continue; parts.push({ tag: '[tool] ', s }) }
  for (const x of Array.isArray(o.results) ? o.results : []) { const s = String(x); if (s === RECALL_RESULT_NOTE || RECALL_OUTPUT_HEAD.test(s)) continue; parts.push({ tag: '[result] ', s }) }
  if (!parts.length) return undefined
  return { kind: 'row', t, sid, uuid: o.uuid, label: rowLabel(o), parts }
}

// Words to look for, and the phrase when the query is quoted. Words are deduped, at most 12.
export function parseQuery(raw: unknown): { words: string[]; phrase?: string } {
  let q = flat(String(raw ?? ''))
  let quoted = false
  if (q.length >= 2 && q.startsWith('"') && q.endsWith('"')) { q = flat(q.slice(1, -1)); quoted = true }
  // A word is cut at 200 characters: rg takes each as an argument, and a huge one fails the whole search.
  const words = [...new Set(q.toLowerCase().split(' ').filter(Boolean).map(w => w.slice(0, 200)))].slice(0, 12)
  return { words, phrase: quoted && q ? q.toLowerCase() : undefined }
}

// How well an item matches: 0 = not a hit. A quoted phrase must appear whole; otherwise one point per word present,
// plus one when the whole query appears as written. Also says where to cut the snippet.
export function scoreItem(it: Item, words: string[], phrase: string | undefined): { score: number; part: number; at: number; len: number; matched: number } {
  const flats = it.parts.map(p => flat(p.s).toLowerCase())
  const find = (needle: string): { part: number; at: number } | undefined => {
    for (let i = 0; i < flats.length; i++) { const at = flats[i]!.indexOf(needle); if (at >= 0) return { part: i, at } }
    return undefined
  }
  if (phrase) {
    const hit = find(phrase)
    return hit ? { score: words.length + 1, ...hit, len: phrase.length, matched: words.length } : { score: 0, part: 0, at: 0, len: 0, matched: 0 }
  }
  let matched = 0
  let first: { part: number; at: number; len: number } | undefined
  for (const w of words) {
    const hit = find(w)
    if (hit) { matched++; if (!first) first = { ...hit, len: w.length } }
  }
  if (!matched) return { score: 0, part: 0, at: 0, len: 0, matched: 0 }
  const whole = words.length > 1 ? find(words.join(' ')) : undefined
  const anchor = whole ? { ...whole, len: words.join(' ').length } : first!
  return { score: matched + (whole ? 1 : 0), ...anchor, matched }
}

function snippet(it: Item, part: number, at: number, len: number, width: number): string {
  const p = it.parts[part] ?? it.parts[0]!
  const s = flat(p.s)
  if (s.length <= width) return p.tag + s
  const start = Math.max(0, Math.min(at - Math.floor((width - len) / 3), s.length - width))
  return `${p.tag}${start > 0 ? '…' : ''}${s.slice(start, start + width)}${start + width < s.length ? '…' : ''}`
}

function wholeText(it: Item, width: number): string {
  const s = it.parts.map(p => p.tag + flat(p.s)).join(' | ')
  return s.length <= width ? s : `${s.slice(0, width)}…`
}

function hitHeader(it: Item, showSid: boolean): string {
  const where = showSid && it.sid ? ` · s:${it.sid.slice(0, 8)}` : ''
  return `• ${stampOf(it.t)} · ${it.label}${it.uuid ? ` · uuid ${it.uuid}` : ''}${where}`
}

// ripgrep, at tools.rgPath. A spawn that fails (not installed, not executable) becomes one clear message instead of a
// stack of engine text; any other failure passes through.
function rgHelp(): string {
  return `ripgrep ("${rgBin}") was not found or could not run. Install ripgrep, or set tools.rgPath in chronicle.json to its full path.`
}

class RgMissing extends Error {}

async function rgRun($: EngineInterface, args: string[], timeoutMs: number) {
  let r
  try { r = await $.process.run([rgBin, ...args], { timeoutMs }) } catch (err) {
    if (/ENOENT|not found|no such file|spawn|EACCES|permission denied/i.test(why(err))) throw new RgMissing(rgHelp())
    throw err
  }
  if (r.exitCode === 127) throw new RgMissing(rgHelp())
  return r
}

// Lists <root>/<day>/*.jsonl as { day, name, path }. Missing archive: an empty list and `missing`.
async function archiveFiles($: EngineInterface, root: string): Promise<{ files: { day: string; name: string; path: string }[]; missing?: boolean; error?: string }> {
  if (isWin) return archiveFilesWin($, root)
  let r
  try { r = await rgRun($, ['--files', '--no-config', '--no-ignore', '--hidden', '--max-depth', '2', '-g', '*.jsonl', root], RECALL_RUN_MS) } catch (err) {
    if (err instanceof RgMissing) return { files: [], error: err.message }
    throw err
  }
  if (r.exitCode !== 0 && !r.stdout.trim()) {
    if (/No such file|not found|os error 2/i.test(r.stderr)) return { files: [], missing: true }
    if (r.exitCode === 1) return { files: [] }
    return { files: [], error: r.stderr.trim().split('\n')[0]?.slice(0, 200) || `exit ${r.exitCode}` }
  }
  const files: { day: string; name: string; path: string }[] = []
  for (const path of r.stdout.split('\n')) {
    if (!path.startsWith(`${root}/`)) continue
    const rel = path.slice(root.length + 1).split('/')
    if (rel.length !== 2) continue
    files.push({ day: rel[0]!, name: rel[1]!, path })
  }
  return { files }
}

// Lines of the given files that hold any pattern (fixed strings, case-insensitive); every line when no pattern.
async function grepLines($: EngineInterface, patterns: string[], paths: string[]): Promise<{ lines: string[]; cut: boolean; error?: string }> {
  if (isWin) return grepLinesWin($, patterns, paths)
  const argv =['--no-config', '--no-ignore', '--hidden', '--fixed-strings', '--ignore-case', '--no-filename', '--no-line-number', '--no-heading', '--color', 'never']
  if (patterns.length) for (const p of patterns) argv.push('-e', p)
  else argv.push('-e', '')
  argv.push('--', ...paths)
  const r = await rgRun($, argv, RECALL_RUN_MS)
  const error = r.exitCode > 1 ? (r.stderr.trim().split('\n')[0]?.slice(0, 200) || `exit ${r.exitCode}`) : undefined
  return { lines: r.stdout.split('\n').filter(Boolean), cut: r.isStdoutTruncated, error }
}

async function currentSid($: EngineInterface): Promise<string> {
  if (!sid) sid = String(await $.session.id())
  return sid
}

// The whole recall. Returns the text the assistant reads. Throws only on an engine failure; the tool hook catches that.
async function recall($: EngineInterface, input: RecallInput): Promise<string> {
  const cfg = await cfgOf($)
  const root = cfg.archiveRoot.replace(/\/+$/, '')
  if (!cfg.mirrorEnabled) return 'recall: the mirror is switched off (mirror.enabled is false in chronicle.json), so there is no session record to search.'
  if (!root) return 'recall: no archive folder (archiveRoot starts with ~ and HOME is not set).'
  // Rows from the last few seconds are still in the buffer: write them first so they can be found.
  try { await scheduleFlush($, 5000) } catch {}

  const listed = await archiveFiles($, root)
  if (listed.missing) return `The mirror at ${root} does not exist yet: nothing has been mirrored there.`
  if (listed.error) warnOnce($, `recall could not list the mirror (${listed.error})`)
  if (!listed.files.length) return listed.error ? `recall could not list the mirror: ${listed.error}` : `The mirror at ${root} is empty so far.`

  const lim = Math.floor(Number(input.limit))
  const limitGiven = Number.isFinite(lim) && lim > 0

  if (input.around !== undefined && String(input.around).trim()) {
    return recallAround($, listed.files, String(input.around).trim(), limitGiven ? Math.min(lim, 40) : 6)
  }

  const limit = limitGiven ? Math.min(lim, 40) : 12
  const from = parseBound(input.from)
  const to = parseBound(input.to)
  if (typeof from === 'string') return `recall: from: ${from}`
  if (typeof to === 'string') return `recall: to: ${to}`
  const kindIn = String(input.kind ?? 'all')
  const kinds: Kind[] = kindIn === 'rows' ? ['row'] : kindIn === 'memories' ? ['memory'] : kindIn === 'summaries' ? ['summary'] : ['row', 'summary', 'memory']
  const onlySid = input.session === 'current' ? await currentSid($) : undefined
  const { words, phrase } = parseQuery(input.query)
  const ranked = words.length > 0

  // Files in range. Folders are days in the archive's zone; `undated` only when no bound is given.
  const isDay = (d: string) => /^\d{4}-\d{2}-\d{2}$/.test(d)
  let files = listed.files.filter(f => {
    if (!isDay(f.day)) return f.day === 'undated' && !from && !to
    if (from?.day && f.day < from.day) return false
    if (to?.day && f.day > to.day) return false
    if (onlySid && !f.name.startsWith(`${safe(onlySid)}.`)) return false
    return true
  })
  // A query or no `from`: newest days first (so a cap drops the oldest). A plain read from `from`: oldest first.
  const ascending = !ranked && !!from
  files.sort((a, b) => (a.day === b.day ? a.name.localeCompare(b.name) : a.day < b.day ? -1 : 1) * (ascending ? 1 : -1))

  const patterns = words.map(jsonEscaped)
  const needed = ranked ? RECALL_MAX_CANDIDATES : limit
  const items: { it: Item; score: number; part: number; at: number; len: number; matched: number }[] = []
  const seen = new Set<string>()
  const notes: string[] = []
  let stoppedAt: string | undefined
  // Batches never split a day, so a plain read can stop at a day boundary with its order intact.
  let i = 0
  while (i < files.length) {
    if (items.length >= needed) { stoppedAt = files[i]!.day; break }
    const batch: string[] = []
    const day0 = files[i]!.day
    while (i < files.length && (batch.length < RECALL_FILES_PER_RUN || files[i]!.day === files[i - 1]?.day) && (ranked || files[i]!.day === day0)) batch.push(files[i++]!.path)
    const g = await grepLines($, patterns, batch)
    if (g.error && !notes.some(n => n.startsWith('search error'))) notes.push(`search error: ${g.error}`)
    if (g.cut) notes.push('one search returned more than 4 MB and was cut; narrow the query or the dates')
    for (const line of g.lines) {
      const it = toItem(line)
      if (!it || !kinds.includes(it.kind)) continue
      if (it.uuid && turnPrompts.has(it.uuid)) continue // this turn's own request (see turnPrompts)
      if (onlySid && it.sid !== onlySid) continue
      if (from?.ms !== undefined && it.t < from.ms) continue
      if (to?.ms !== undefined && it.t > to.ms) continue
      const key = it.uuid ?? `${it.kind}:${it.t}:${it.sid}`
      if (seen.has(key)) continue
      if (ranked) {
        const s = scoreItem(it, words, phrase)
        if (!s.score) continue
        seen.add(key)
        items.push({ it, ...s })
      } else {
        seen.add(key)
        items.push({ it, score: 0, part: 0, at: 0, len: 0, matched: 0 })
      }
    }
  }

  let shown: typeof items
  if (ranked) shown = [...items].sort((a, b) => b.score - a.score || b.it.t - a.it.t)
  else {
    const byTime = [...items].sort((a, b) => a.it.t - b.it.t)
    shown = ascending ? byTime.slice(0, limit) : byTime.slice(-limit)
  }
  const total = shown.length
  shown = shown.slice(0, limit)

  const scope = [
    onlySid ? 'this session' : 'all sessions',
    from || to ? `${input.from ? String(input.from) : '…'} to ${input.to ? String(input.to) : 'now'}` : '',
    kindIn !== 'all' ? kindIn : '',
  ].filter(Boolean).join(', ')
  if (!total) {
    const what = ranked ? `nothing matched ${phrase ? `"${phrase}"` : words.join(' ')}` : 'nothing in that range'
    return [`recall (${scope}): ${what}.`, ...notes, stoppedAt ? `Search stopped before ${stoppedAt}.` : ''].filter(Boolean).join('\n')
  }

  const showSid = !onlySid
  const head = ranked
    ? `recall ${phrase ? `"${phrase}"` : words.join(' ')} (${scope}): ${total > limit ? `top ${shown.length} of ${total}${stoppedAt ? '+' : ''}` : `${total}`} hit${total === 1 ? '' : 's'}, most words matched first, then newest. around=<uuid> opens a stretch.`
    : `recall (${scope}): ${shown.length} ${ascending ? 'earliest' : 'latest'} entries, in time order. around=<uuid> opens a stretch.`
  const out: string[] = [head]
  let used = head.length
  let omitted = 0
  for (const h of shown) {
    const partial = ranked && !phrase && h.matched < words.length ? ` (matched ${h.matched}/${words.length} words)` : ''
    const body = ranked ? snippet(h.it, h.part, h.at, h.len, RECALL_SNIPPET) : snippet(h.it, 0, 0, 0, RECALL_SNIPPET)
    const block = `${hitHeader(h.it, showSid)}${partial}\n  ${body}`
    if (used + block.length + 1 > RECALL_OUT_CAP) { omitted++; continue }
    out.push(block)
    used += block.length + 1
  }
  if (omitted) notes.push(`truncated: ${omitted} more hit${omitted === 1 ? '' : 's'} did not fit in ${RECALL_OUT_CAP} characters; lower limit or narrow the query`)
  if (stoppedAt) {
    if (ranked) notes.push(`stopped after ${RECALL_MAX_CANDIDATES} matches; ${stoppedAt} and earlier days were not searched (use from/to)`)
    else if (ascending) notes.push(`more follows from ${stoppedAt} on: call again with from set to the last time shown`)
    else notes.push(`earlier days (${stoppedAt} and before) not read: set to, or from, to go back`)
  }
  if (ranked && total > limit) notes.push(`${total - shown.length} lower-ranked hits not shown (raise limit up to 40, or narrow)`)
  return [...out, ...notes.map(n => `[${n}]`)].join('\n')
}

// A row with `n` rows each side, from its session file(s) across days, in order.
async function recallAround($: EngineInterface, files: { day: string; name: string; path: string }[], uuid: string, n: number): Promise<string> {
  if (!/^[A-Za-z0-9-]{8,64}$/.test(uuid)) return `recall: around wants a row uuid from an earlier hit, not "${uuid.slice(0, 80)}".`
  const sessionFiles = files.filter(f => !f.name.endsWith('.memories.jsonl'))
  const found = await grepLines($, [`"uuid":"${uuid}"`], sessionFiles.map(f => f.path))
  const target = found.lines.map(toItem).find(x => x?.uuid === uuid)
  if (!target) return `recall: no row with uuid ${uuid} in the mirror${found.error ? ` (search error: ${found.error})` : ''}.`
  const own = sessionFiles.filter(f => f.name === `${safe(target.sid || 'unknown')}.jsonl`).sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0))
  if (!own.length) return `recall: found ${uuid} but not its session file.`
  const r = await readWhole($, own.map(f => f.path))
  const readNote = r.isStdoutTruncated ? '[the session file was larger than one read allows: rows past that point are not shown here; narrow with from/to]' : r.exitCode !== 0 ? `[the session file was not read whole (exit ${r.exitCode})]` : ''
  const seq: Item[] = []
  const seen = new Set<string>()
  for (const line of r.stdout.split('\n')) {
    if (!line) continue
    const it = toItem(line)
    if (!it || it.kind === 'memory') continue
    const key = it.uuid ?? `${it.kind}:${it.t}`
    if (seen.has(key)) continue
    seen.add(key)
    seq.push(it)
  }
  const at = seq.findIndex(x => x.uuid === uuid)
  if (at < 0) return `recall: found ${uuid} but could not read it back from its session file.${readNote ? ` ${readNote}` : ''}`
  const lo = Math.max(0, at - n)
  const hi = Math.min(seq.length, at + n + 1)
  const width = Math.max(200, Math.min(1200, Math.floor(10_000 / (hi - lo))))
  const blocks: string[] = []
  for (let k = lo; k < hi; k++) {
    const it = seq[k]!
    if (it.kind === 'summary') { blocks.push(`— ${stampOf(it.t)} · compaction here —`); continue }
    const mark = k === at ? '▶ ' : ''
    blocks.push(`${mark}${hitHeader(it, false).slice(2)}\n  ${wholeText(it, k === at ? Math.max(width, 3000) : width)}`)
  }
  // Keep the target: trim from the far ends first when over the cap (the header and the note are budgeted too).
  const note = `[trimmed to fit ${RECALL_OUT_CAP} characters; ask again with a smaller limit for wider rows]`
  const mid = at - lo
  let first = 0, last = blocks.length - 1
  const size = () => 160 + note.length + blocks.slice(first, last + 1).reduce((s, b) => s + b.length + 1, 0)
  let trimmed = false
  while (size() > RECALL_OUT_CAP && (first < mid || last > mid)) {
    if (mid - first >= last - mid) first++; else last--
    trimmed = true
  }
  const a = lo + first, b = lo + last + 1
  const head = `recall around ${uuid} (session ${target.sid.slice(0, 8)}, rows ${a + 1}-${b} of ${seq.length}${a === 0 ? ', from its start' : ''}${b === seq.length ? ', to its end' : ''}):`
  const out = [head, ...blocks.slice(first, last + 1)]
  if (trimmed) out.push(note)
  if (readNote) out.push(readNote)
  return out.join('\n')
}

// ---------- recall: the optional archive ----------
// With archive.command set (for example ["resonant-archive", "recall"]), source "archive" runs
//   <command...> --json <action> [options] [-- <positional>]
// and returns the "text" of the JSON it prints (README.md, "Archive contract"). This side builds the argv and passes
// the output through; the command checks every value itself (sources, dates, ranges): a bad one comes back as its own
// message. The mod holds no database settings and no credentials: the command reads its own.

function argStr(v: unknown): string | undefined {
  if (v === undefined || v === null) return undefined
  const s = (Array.isArray(v) ? v.map(String).join(',') : String(v)).trim()
  return s || undefined
}

function argInt(v: unknown): string | undefined {
  if (v === undefined || v === null || String(v).trim() === '') return undefined
  const n = Number(v)
  return Number.isFinite(n) ? String(Math.floor(n)) : String(v).trim()
}

// The argv after the command and --json, or a message for the assistant when the call cannot be made.
export function archiveArgv(input: RecallInput): string[] | string {
  const around = argStr(input.around)
  const action = argStr(input.action) ?? (around ? 'around' : 'search')
  const id = argStr(input.id) ?? around
  const opt = (flag: string, v: string | undefined, a: string[]) => { if (v !== undefined) a.push(flag, v) }
  const a: string[] = []
  switch (action) {
    case 'search': {
      const q = argStr(input.query)
      if (!q) return 'recall: search needs a query. For a stretch of time with no query, use action overview with from and to.'
      a.push('search')
      opt('--source', argStr(input.collection), a); opt('--from', argStr(input.from), a); opt('--to', argStr(input.to), a)
      opt('--speaker', argStr(input.speaker), a); opt('--context', argStr(input.context), a)
      opt('--limit', argInt(input.limit), a); opt('--mode', argStr(input.mode), a)
      if (input.hidden === true || input.hidden === 'true') a.push('--hidden')
      if (input.recent === true || input.recent === 'true') a.push('--recent')
      a.push('--', q)
      return a
    }
    case 'around':
    case 'associate': {
      if (!id) return `recall: ${action} needs id, a message id from an archive search hit.`
      a.push(action)
      if (action === 'around') { opt('--before', argInt(input.before), a); opt('--after', argInt(input.after), a) }
      else {
        opt('--k', argInt(input.k), a)
        if (input.recent === true || input.recent === 'true') a.push('--recent')
      }
      if (input.hidden === true || input.hidden === 'true') a.push('--hidden')
      a.push('--', id)
      return a
    }
    case 'overview': {
      const from = argStr(input.from), to = argStr(input.to)
      if (!from || !to) return 'recall: overview needs from and to (YYYY-MM-DD).'
      a.push('overview', '--from', from, '--to', to)
      opt('--source', argStr(input.collection), a)
      const mem = argStr(input.memories)
      if (mem === 'only') a.push('--memories-only')
      else if (mem === 'none') a.push('--no-memories')
      else if (mem !== undefined && mem !== 'include') return `recall: memories "${mem.slice(0, 40)}" is not include, only or none.`
      return a
    }
    case 'drill': {
      const m = argStr(input.memory) ?? id
      if (!m) return 'recall: drill needs memory, a #index from overview (or a memory\'s from_uuid).'
      a.push('drill')
      if (input.hidden === true || input.hidden === 'true') a.push('--hidden')
      a.push('--', m)
      return a
    }
    default:
      return `recall: action "${action.slice(0, 40)}" is not one of search, around, overview, drill, associate.`
  }
}

const NO_ARCHIVE_TEXT = (root: string) => `recall: no archive is set up. Session recall searches the mirror${root ? ` at ${root}` : ''}; use source session (or leave source out). To search every past conversation, install resonant-archive and set archive.command in chronicle.json.`
const ARCHIVE_OUT_CAP = RECALL_OUT_CAP

// What the archive command printed, as the text to hand back. JSON {"ok":true,"text":"..."} is the contract; plain text
// is passed through too, so a command that has no --json mode yet still works.
export function archiveReply(stdout: string, truncated: boolean): string {
  const out = stdout.trim()
  let text = out
  try {
    const j = JSON.parse(out) as { ok?: unknown; text?: unknown; error?: unknown }
    if (j && typeof j === 'object') {
      if (typeof j.text === 'string' && j.ok !== false) text = j.text.trim()
      else if (typeof j.error === 'string') return `recall failed: ${j.error.slice(0, 400)}`
    }
  } catch {}
  if (text.length > ARCHIVE_OUT_CAP) return `${text.slice(0, ARCHIVE_OUT_CAP)}\n[output cut at ${ARCHIVE_OUT_CAP} characters]`
  return truncated ? `${text}\n[output cut by the process limit]` : text
}

async function recallArchive($: EngineInterface, input: RecallInput): Promise<string> {
  const cfg = await cfgOf($)
  if (!cfg.archiveCommand.length) return NO_ARCHIVE_TEXT(cfg.archiveRoot)
  const argv = archiveArgv(input)
  if (typeof argv === 'string') return argv
  let r
  try { r = await $.process.run([...cfg.archiveCommand, '--json', ...argv], { timeoutMs: cfg.archiveTimeoutMs }) } catch (err) {
    return `recall failed: the archive command could not run or timed out after ${Math.round(cfg.archiveTimeoutMs / 1000)} s (${why(err)})`
  }
  if (r.stdout.trim()) return archiveReply(r.stdout, r.isStdoutTruncated)
  const err = r.stderr.trim().split('\n').slice(-3).join(' | ').slice(0, 400)
  return `recall failed: exit ${r.exitCode}${err ? `: ${err}` : ''}`
}

// ---------- L1 memories ----------
// Autobiographical memory (the idea comes from Anima Labs' connectome): the assistant writes its own memory of each
// 15-25k-token stretch, in the first person, as the work goes. Off unless memories.enabled is true.
// A chunk accumulates the estimated tokens of mirrored main-loop rows. At a main-loop turn's end (answered, not
// interrupted), once it holds chunkMinTokens, it is sealed (so a chunk always ends on a turn boundary) and formed in
// the background with $.model.fork, which re-asks over the transcript as the main thread last sent it (a cached
// prefix, so cheap) with the marker prompt after it. The fork cannot re-send a slice, so the prompt names the
// slice by its first and last rows (local time, speaker, opening words) and carries the turn's final reply, which
// the last request of the turn did not contain yet.
// One formation at a time. While one runs, the next chunk keeps growing (deferred); only past chunkMaxTokens is it
// sealed into a short queue, so chunks stay inside the window. A failed formation waits for the next turn's end
// and is tried once more, never in a loop. The chunk state is saved to $.store at each turn's end, so a hot reload
// or a resume picks the stretch up where it was.
// Written to <archiveRoot>/<day of t>/<sid>.memories.jsonl (README.md, "Mirror format"), with a
// {event:"memory_formed"} marker in the mirror file. Recall finds memories by content (event "memory" with text).

type Chunk = {
  sid: string
  fromUuid: string; toUuid: string; tFrom: number; tTo: number
  firstLabel: string; firstSnippet: string; lastLabel: string; lastSnippet: string
  tokens: number; rows: number
  compacted?: boolean // a compaction landed inside the stretch: its start may now be only in the summary
  answer?: string // the final reply of the sealing turn (redacted, clipped)
  attempts?: number
  ordinal?: number // the chunk's number in its session, from 0, given at sealing: a missing number is a hole
  retryAt?: number // a failed formation is not tried again before this time
}
// `next`: the ordinal the session's next sealed chunk gets (kept even when nothing is pending).
type Saved = { at: number; acc?: Chunk; queue: Chunk[]; next?: number }

// The in-band marker, the same words every time. The prompt begins with it exactly.
export const MEMORY_MARKER = "System: You will soon form a new memory, get ready. The messages that follow are the slice of recent experience you are about to compress. After them, write the memory in your own voice."
const NO_SECRETS = "Never copy credentials: API keys, tokens, passwords, connection strings, secret URLs. Write [secret: <what it was for>] instead."
export const DEFAULT_MEMORY_VOICE = "Write my memory of only that stretch: nothing before its first row (earlier memories hold that) and nothing after its last. First person, as the assistant: I, and the person I'm working with (by name if they gave one). Not a report, but something I could wake into later and recognise as mine. Keep: what happened, in order; what they said and decided, and their reasons, with every exact identifier verbatim (file paths, ids, commit hashes, URLs, config keys, error strings, numbers); their state where it matters to the work (mood, energy, constraints, what they are doing next); the tone between us, briefly and honestly; what I wanted or proposed; what is still open, theirs and mine. " + NO_SECRETS + " Drop the mechanics of tool calls unless the outcome matters. 300 to 700 words. Answer with the memory alone: no heading, no preamble, no tool calls."
// The default steer for the summary of the older part of a compaction. compaction.steer replaces it; compaction.voice is put in front.
export const DEFAULT_STEER = "You are writing the memory of this stretch of work and conversation, so that it can be picked up later without the original. Write in the first person, as the assistant: 'I', and 'the person I'm working with' (or their name, if they gave it). Keep: (1) every exact identifier verbatim: file paths, ids, commit hashes, URLs, config keys, error strings, numbers; (2) every decision and who made it, with the reason; (3) promises and open threads, mine and theirs; (4) the person's stated preferences, constraints and state where they matter to the work; (5) the tone between us, briefly and honestly; (6) what I was about to do next. Drop the mechanics of tool calls unless the outcome matters. Prefer specific over general. " + NO_SECRETS

// The steer text for a compaction: persona paragraph, then the default or the person's own steer, always ending with the
// no-credentials rule.
export function steerOf(cfg: Pick<Cfg, 'voice' | 'steer'>): string {
  const body = cfg.steer || DEFAULT_STEER
  const full = (cfg.voice ? `${cfg.voice}\n\n` : '') + body
  return full.includes('Never copy credentials') ? full : `${full} ${NO_SECRETS}`
}

// The instructions of a memory formation: the person's replacement if they gave one, else the default, with the persona paragraph in front.
export function memoryVoiceOf(cfg: Pick<Cfg, 'memoryVoice' | 'memoryPrompt'>): string {
  const body = cfg.memoryPrompt || DEFAULT_MEMORY_VOICE
  const full = (!cfg.memoryPrompt && cfg.memoryVoice ? `${cfg.memoryVoice}\n\n` : '') + body
  return full.includes('Never copy credentials') ? full : `${full} ${NO_SECRETS}`
}
const COMPACTED_NOTE = "A compaction happened during this stretch. If its beginning now sits only inside the compaction summary above, write that part from the summary and say it is blurry. Do not invent."
const DEFAULT_CHUNK_MIN = 15_000
const DEFAULT_CHUNK_MAX = 25_000
const MEMORY_QUEUE_CAP = 3
const MEMORY_MAX_ATTEMPTS = 4
const MEMORY_RETRY_BASE_MS = 60_000 // a failed formation waits 1, 2, then 4 minutes (and a turn end) before the next try
const ACC_HARD_FACTOR = 3 // an open chunk past chunkMaxTokens times this, with no room to seal it, is given up with a note
const MEMORY_STALE_MS = 15 * 60_000
const MEMORY_TEXT_MAX = 12_000
const ANSWER_IN_PROMPT_CHARS = 6000
const ANCHOR_CHARS = 120
const END_SNIPPET_MIN_CHARS = 40 // a final reply shorter than this is never an end anchor (by hash or by snippet)
const MEMORY_STORE_KEY = 'memoryState'
const MEMORY_STORE_SESSIONS = 8

let acc: Chunk | undefined // the stretch since the last sealed chunk
let queue: Chunk[] = [] // sealed, waiting to be formed (a failed one goes back to the front)
let forming: { gen: number; since: number; chunk: Chunk; started?: boolean } | undefined
let formGen = 0
const restored = new Set<string>() // sessions whose saved state was merged in during this module life
const cleared = new Set<string>() // sessions a /clear ended in this module life: nothing of theirs is pending any more
const nextOrdinal = new Map<string, number>() // per session: the ordinal the next sealed chunk gets
let saveChain: Promise<void> = Promise.resolve()
let memWarned = false

function warnMem($: EngineInterface, line: string) {
  try { $.ui.log(`chronicle: ${line}`, { to: 'debug' }) } catch {}
  if (memWarned) return
  memWarned = true
  try { $.ui.log(`chronicle: ${line}`) } catch {}
}

function posInt(v: unknown, dflt: number): number {
  const n = Math.floor(Number(v))
  return Number.isFinite(n) && n > 0 ? n : dflt
}

function takeOrdinal(s: string): number {
  const n = nextOrdinal.get(s) ?? 0
  nextOrdinal.set(s, n + 1)
  return n
}

// Characters of a row as the model reads it: text, tool inputs and tool results whole (the mirror truncates results;
// the context does not). Divided by 3.6 like estimate() above.
export function rowChars(content: unknown): number {
  let n = 0
  for (const b of (Array.isArray(content) ? content : []) as any[]) {
    if (b?.type === 'text') n += String(b.text ?? '').length
    else if (b?.type === 'tool_use') n += String(b.name ?? '').length + JSON.stringify(b.input ?? {}).length
    else if (b?.type === 'tool_result') {
      if (typeof b.content === 'string') n += b.content.length
      else if (Array.isArray(b.content)) for (const x of b.content) if (x?.type === 'text') n += String(x.text ?? '').length
    }
  }
  return n
}

// A mirrored row's anchor: the first characters of its (already redacted) text, else its first tool or result.
export function anchorOf(row: { text?: string; tools?: string[]; results?: string[] }): string {
  return flat(row.text || row.tools?.[0] || row.results?.[0] || '').slice(0, ANCHOR_CHARS)
}

// The same anchor for a context message's text, redacted the way the mirror redacted that row.
export function msgAnchor(text: unknown): string {
  return flat(redact(String(text ?? ''))).slice(0, ANCHOR_CHARS)
}

// Adds one mirrored main-loop row to the open chunk. Pure memory work; never throws in practice (the caller wraps it).
export function noteRow(row: { t: number; sid: string; uuid?: unknown; door: string; chronicle?: string; queued?: string; text?: string; tools?: string[]; results?: string[] }, chars: number) {
  if (row.door === 'compaction' || typeof row.uuid !== 'string' || row.chronicle) return
  // A fork that surfaced as a row is tagged chronicle:"fork" by mirror(); this catches one that was not.
  if (row.text && row.text.startsWith(MEMORY_MARKER)) return
  if (acc && acc.sid !== row.sid) acc = undefined // a new session id that no session.end announced
  const label = rowLabel(row)
  const snip = anchorOf(row)
  const tokens = Math.ceil(chars / 3.6)
  if (!acc) {
    acc = { sid: row.sid, fromUuid: row.uuid, toUuid: row.uuid, tFrom: row.t, tTo: row.t, firstLabel: label, firstSnippet: snip, lastLabel: label, lastSnippet: snip, tokens, rows: 1 }
    return
  }
  acc.toUuid = row.uuid
  acc.tTo = row.t
  acc.lastLabel = label
  acc.lastSnippet = snip
  acc.tokens += tokens
  acc.rows++
}

// A compaction happened (its row arrived, or this plugin returned one): whatever is not formed yet may now sit
// partly inside a summary. A formation whose fork has not started yet is told too.
function noteCompaction() {
  if (acc) acc.compacted = true
  for (const c of queue) c.compacted = true
  if (forming && !forming.started) forming.chunk.compacted = true
}

function clipAnswer(s: string): string {
  const a = String(s ?? '').trim()
  if (a.length <= ANSWER_IN_PROMPT_CHARS) return a
  return `${a.slice(0, 4000)}\n[...]\n${a.slice(-(ANSWER_IN_PROMPT_CHARS - 4100))}`
}

// The form a final reply is kept, hashed and matched in: redacted first, then clipped. One function for both sides.
export function answerForm(s: unknown): string {
  return clipAnswer(redact(String(s ?? '')))
}

// Two 32-bit FNV-1a passes with different bases: a stable 16-hex id, no crypto needed.
export function hash16(s: string): string {
  let a = 0x811c9dc5, b = 0x050c5d1f
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i)
    a = Math.imul(a ^ c, 0x01000193) >>> 0
    b = Math.imul(b ^ c, 0x01000193) >>> 0
    b = (b ^ (b >>> 13)) >>> 0
  }
  return a.toString(16).padStart(8, '0') + b.toString(16).padStart(8, '0')
}

// ---- the memory rows are signed (HMAC-SHA-256 built on crypto.subtle.digest, the one primitive the environment
// declares), with a key kept in $.store, outside the archive and its repo. Assembly uses only rows whose mac checks,
// so a row written into the archive by anything else (a git pull of someone else's commit) never reaches context.

const MAC_STORE_KEY = 'memoryMacKey'
let macKeyP: Promise<Uint8Array | undefined> | undefined

function toHex(b: Uint8Array): string {
  let s = ''
  for (const x of b) s += x.toString(16).padStart(2, '0')
  return s
}

function fromHex(s: string): Uint8Array {
  const out = new Uint8Array(s.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.slice(i * 2, i * 2 + 2), 16)
  return out
}

export async function hmacHex(key: Uint8Array, msg: string): Promise<string> {
  const B = 64
  let k = key
  if (k.length > B) k = new Uint8Array(await crypto.subtle.digest('SHA-256', k))
  const m = new TextEncoder().encode(msg)
  const inner = new Uint8Array(B + m.length)
  const outer = new Uint8Array(B + 32)
  for (let i = 0; i < B; i++) { const x = i < k.length ? k[i]! : 0; inner[i] = x ^ 0x36; outer[i] = x ^ 0x5c }
  inner.set(m, B)
  outer.set(new Uint8Array(await crypto.subtle.digest('SHA-256', inner)), B)
  return toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', outer)))
}

// The key, made once (32 random bytes) and read back, so two module lives that raced agree on the one stored.
async function loadMacKey($: EngineInterface): Promise<Uint8Array | undefined> {
  const ok = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v)
  const raw = await $.store.get(MAC_STORE_KEY)
  if (ok(raw)) return fromHex(raw)
  if (raw !== undefined) return undefined // something else sits there: never overwrite it
  await $.store.set(MAC_STORE_KEY, toHex(crypto.getRandomValues(new Uint8Array(32))))
  const back = await $.store.get(MAC_STORE_KEY)
  return ok(back) ? fromHex(back) : undefined
}

async function macKeyOf($: EngineInterface): Promise<Uint8Array | undefined> {
  if (!macKeyP) macKeyP = loadMacKey($).catch(() => undefined)
  const k = await macKeyP
  if (!k) macKeyP = undefined // not cached: a later call tries again
  return k
}

// What the mac covers: every field assembly relies on.
export function macMessage(o: Record<string, unknown>): string {
  return JSON.stringify([
    o.sid, o.level, o.ordinal, o.from_uuid, o.to_uuid, o.t_from, o.t_to, o.first_text_snippet, o.last_text_snippet,
    o.last_assistant_text_hash ?? null, o.answer_chars ?? null, o.text,
  ])
}

function clockOf(ms: number): string {
  if (!ms) return 'an unknown time'
  const o = zoneParts(ms)
  return `${o.year}-${o.month}-${o.day} ${o.hour}:${o.minute}:${o.second}`
}

export function memoryPrompt(c: Chunk, voice: string): string {
  const lines = [
    MEMORY_MARKER,
    '',
    `This is a side question over the conversation as it stands, so the slice is not sent again: it is the stretch above, from ${clockOf(c.tFrom)} to ${clockOf(c.tTo)} (${zone}), about ${c.rows} rows and ${Math.max(1, Math.round(c.tokens / 1000))}k tokens.`,
    `It begins with the row (${c.firstLabel}) that starts: "${c.firstSnippet}"`,
    `It ends with the row (${c.lastLabel}) that starts: "${c.lastSnippet}"`,
  ]
  if (c.answer) lines.push('', 'My final reply of the stretch, in case it is not above yet:', '<<<', c.answer, '>>>')
  if (c.compacted) lines.push('', COMPACTED_NOTE)
  lines.push('', voice)
  return lines.join('\n')
}

// Seals the open chunk at a turn's end when it is due, and starts the next formation. Called from turn.complete
// before next(); its caller catches. Only one store read per session per module life, the rest is in memory.
async function memoryTurnEnd($: EngineInterface, e: { isAborted: boolean; reason: string; answer: string }): Promise<void> {
  if (!(await cfgOf($)).memoriesEnabled) return
  const cur = await currentSid($)
  if (!restored.has(cur)) { restored.add(cur); await restoreState($, cur) }
  const cfg = await cfgOf($)
  const min = posInt(cfg.chunkMinTokens, DEFAULT_CHUNK_MIN)
  const max = Math.max(min, posInt(cfg.chunkMaxTokens, DEFAULT_CHUNK_MAX))
  const now = await $.clock.now()
  if (forming && now - forming.since > MEMORY_STALE_MS) {
    // A fork that never came back. Its result, if it ever does, is still written; the queue moves on, and the
    // stretch is marked, so a hole in the chain says why.
    warnMem($, `a memory formation has run ${Math.round((now - forming.since) / 60000)} min without an answer; moving on`)
    memoryNote(now, forming.chunk, 'memory_failed', { reason: 'stale: no answer after 15 min', attempts: (forming.chunk.attempts ?? 0) + 1, ordinal: forming.chunk.ordinal })
    forming = undefined
    flushSoon($)
  }
  if (!e.isAborted && e.reason === 'answer' && acc && acc.sid === cur && acc.tokens >= min) {
    const busy = !!forming || queue.length > 0
    if (!busy || (acc.tokens >= max && queue.length < MEMORY_QUEUE_CAP)) {
      queue.push({ ...acc, answer: answerForm(e.answer) || undefined, ordinal: takeOrdinal(cur) })
      acc = undefined
    }
  }
  if (acc && acc.sid === cur && acc.tokens >= max * ACC_HARD_FACTOR && queue.length >= MEMORY_QUEUE_CAP) {
    // Formations are failing or stuck and the queue is full: one huge low-fidelity memory would be worse than an
    // honest hole. The raw mirror keeps the stretch; the note says why the chain stops here.
    // Only then: a long stretch of turns that did not answer (API errors, an Esc on a long tool-heavy turn) with
    // room in the queue is NOT given up; the next answered turn seals it (seen in live testing: a hole here capped
    // assembly for the rest of the session).
    const c: Chunk = { ...acc, ordinal: takeOrdinal(cur) }
    memoryNote(now, c, 'memory_skipped', { reason: `backlog: the open stretch passed ${max * ACC_HARD_FACTOR} tokens with no room to form it`, ordinal: c.ordinal })
    warnMem($, 'memories are backed up (formations failing or stuck); one stretch was left to the raw mirror')
    acc = undefined
    flushSoon($)
  }
  pumpMemory($, now)
  saveSoon($)
}

function pumpMemory($: EngineInterface, now: number) {
  if (forming || !queue.length) return
  if (queue[0]!.retryAt && now < queue[0]!.retryAt) return // a failed one waits its turn; a later turn end pumps it
  const chunk = queue.shift()!
  const gen = ++formGen
  forming = { gen, since: now, chunk }
  try {
    $.clock.after(1, () => { void formMemory($, gen, chunk) })
  } catch {
    forming = undefined
    queue.unshift(chunk)
  }
}

// Marker and skip lines go to the mirror file; recall does not show them (an event other than compaction/memory).
function memoryNote(t: number, c: Chunk, event: string, extra: Record<string, unknown>) {
  push({ day: dayOf(t), sid: c.sid, line: JSON.stringify({ t, event, level: 1, sid: c.sid, from_uuid: c.fromUuid, to_uuid: c.toUuid, t_from: c.tFrom, t_to: c.tTo, est_tokens: c.tokens, ...extra }) + '\n' }, true)
}

// One formation, in the background. Never throws; every arm of the fork's result is handled; no retry from here.
async function formMemory($: EngineInterface, gen: number, chunk: Chunk): Promise<void> {
  let moveOn = false // true: this chunk is settled (written or given up), so the next queued one may start now
  let written = false // once the memory row is pushed, nothing may requeue the chunk (that would write it twice)
  let failure: string | undefined
  let t = lastT
  try {
    const cur = String(await $.session.id())
    if (cur !== chunk.sid) {
      // A /clear or resume came first: forking now would read another conversation.
      t = await $.clock.now()
      memoryNote(t, chunk, 'memory_skipped', { reason: 'the session changed before the memory formed', ordinal: chunk.ordinal })
      moveOn = true
      return
    }
    const cfg = await cfgOf($)
    const started = await $.clock.now()
    if (forming?.gen === gen) forming.started = true // from here a compaction no longer reaches this prompt
    const prompt = memoryPrompt(chunk, memoryVoiceOf(cfg))
    const r = await $.model.fork({ prompt })
    t = await $.clock.now()
    lastT = Math.max(lastT, t)
    if (r.isAnswered && r.text.trim()) {
      const u = r.usage
      const fed = (u?.input_tokens ?? 0) + (u?.cache_read_input_tokens ?? 0) + (u?.cache_creation_input_tokens ?? 0)
      const answer = chunk.answer ?? ''
      // A short final reply ("Done.", a ritual line) is no anchor: it can repeat later in the session.
      const anchorable = answer.length >= END_SNIPPET_MIN_CHARS
      const row: Record<string, unknown> = {
        t, event: 'memory', level: 1, sid: chunk.sid, ordinal: chunk.ordinal, text: capText(redact(r.text.trim()), MEMORY_TEXT_MAX),
        from_uuid: chunk.fromUuid, to_uuid: chunk.toUuid, t_from: chunk.tFrom, t_to: chunk.tTo,
        est_tokens: chunk.tokens, rows: chunk.rows,
        first_text_snippet: chunk.firstSnippet, last_text_snippet: chunk.lastSnippet,
        last_assistant_text_hash: anchorable ? hash16(answer) : undefined,
        answer_chars: answer ? answer.length : undefined,
        source_hash: hash16(`${chunk.sid}|${chunk.fromUuid}|${chunk.toUuid}`),
        compacted: chunk.compacted ? true : undefined,
        attempts: (chunk.attempts ?? 0) + 1,
        usage: u, cache_share: fed > 0 ? Math.round(((u?.cache_read_input_tokens ?? 0) / fed) * 1000) / 1000 : undefined,
        ms: t - started,
      }
      try {
        const key = await macKeyOf($)
        if (key) row.mac = await hmacHex(key, macMessage(JSON.parse(JSON.stringify(row))))
      } catch {}
      if (!row.mac) { try { $.ui.log('chronicle: a memory was written unsigned (no key); assembly will not use it', { to: 'debug' }) } catch {} }
      push({ day: dayOf(t), sid: chunk.sid, suffix: '.memories', line: JSON.stringify(row) + '\n' }, true)
      written = true
      moveOn = true
      memoryNote(t, chunk, 'memory_formed', { source_hash: row.source_hash, ordinal: chunk.ordinal, words: String(row.text).split(/\s+/).length })
      flushSoon($)
      return
    }
    if (!r.isAnswered && r.reason === 'nothing-to-fork') {
      memoryNote(t, chunk, 'memory_skipped', { reason: 'nothing to fork (a fresh or cleared conversation)', ordinal: chunk.ordinal })
      moveOn = true
      return
    }
    failure = r.isAnswered ? 'empty-reply' : r.reason === 'api-error' ? `api-error ${r.status ?? 'no status'} ${r.error}` : r.reason
  } catch (err) {
    if (!written) failure = `fork refused: ${why(err)}`
  } finally {
    try {
      if (failure) {
        const attempts = (chunk.attempts ?? 0) + 1
        if (attempts < MEMORY_MAX_ATTEMPTS) {
          // Back to the front, tried again at a turn end after a growing wait (never at once: no loop, and an API
          // overload across two turn ends no longer ends the chain).
          queue.unshift({ ...chunk, attempts, retryAt: t + MEMORY_RETRY_BASE_MS * 2 ** (attempts - 1) })
          try { $.ui.log(`chronicle: memory formation failed (${failure}); will try again after a later turn (attempt ${attempts} of ${MEMORY_MAX_ATTEMPTS})`, { to: 'debug' }) } catch {}
        } else {
          memoryNote(t, chunk, 'memory_failed', { reason: failure, attempts, ordinal: chunk.ordinal })
          warnMem($, `a memory could not be formed (${failure}); the stretch stays in the raw mirror`)
          moveOn = true
        }
      }
      if (forming?.gen === gen) forming = undefined
      flushSoon($) // the memory row, or a skip/fail note, lands now rather than at the 30 s timer
      saveSoon($)
      if (moveOn) pumpMemory($, t)
    } catch {}
  }
}

// ---- the chunk state across a hot reload or a resume ($.store, one key, the last few sessions) ----

function isChunk(c: unknown): c is Chunk {
  const x = c as Chunk | undefined
  return !!x && typeof x.sid === 'string' && typeof x.fromUuid === 'string' && typeof x.toUuid === 'string' && Number.isFinite(x.tokens)
}

// Merges what was saved for `cur` before this module loaded with what has come in since. Never throws.
async function restoreState($: EngineInterface, cur: string): Promise<void> {
  try {
    const all = (await $.store.get(MEMORY_STORE_KEY)) as Record<string, Saved> | undefined
    const s = all && typeof all === 'object' ? all[cur] : undefined
    if (!s) return
    const savedQueue = (Array.isArray(s.queue) ? s.queue : []).filter(isChunk).filter(c => c.sid === cur && !queue.some(q => q.fromUuid === c.fromUuid))
    queue = [...savedQueue, ...queue]
    const highest = Math.max(-1, ...queue.filter(c => c.sid === cur).map(c => (Number.isInteger(c.ordinal) ? c.ordinal! : -1)))
    nextOrdinal.set(cur, Math.max(nextOrdinal.get(cur) ?? 0, Number.isInteger(s.next) ? s.next! : 0, highest + 1))
    if (isChunk(s.acc) && s.acc.sid === cur) {
      const old = s.acc
      if (!acc || acc.sid !== cur) acc = old
      else if (acc.fromUuid !== old.fromUuid) {
        acc = { ...acc, fromUuid: old.fromUuid, tFrom: old.tFrom, firstLabel: old.firstLabel, firstSnippet: old.firstSnippet, tokens: old.tokens + acc.tokens, rows: old.rows + acc.rows, compacted: acc.compacted || old.compacted }
      }
    }
  } catch (err) {
    try { $.ui.log(`chronicle: memory state not restored (${why(err)})`, { to: 'debug' }) } catch {}
  }
}

// Writes the state of every session restored in this module life. The ordinal counter is kept even when nothing is
// pending (a resume must not number chunks from 0 again). Serialized; never throws.
async function saveState($: EngineInterface): Promise<void> {
  try {
    const raw = await $.store.get(MEMORY_STORE_KEY)
    const all: Record<string, Saved> = raw && typeof raw === 'object' ? { ...(raw as Record<string, Saved>) } : {}
    const pending = [...(forming ? [forming.chunk] : []), ...queue]
    for (const s of restored) {
      const q = cleared.has(s) ? [] : pending.filter(c => c.sid === s)
      const a = !cleared.has(s) && acc && acc.sid === s ? acc : undefined
      const prevNext = Number.isInteger(all[s]?.next) ? all[s]!.next! : 0
      const next = Math.max(prevNext, nextOrdinal.get(s) ?? 0)
      if (!a && !q.length && !next) delete all[s]
      else all[s] = { at: lastT, acc: a, queue: q, next }
    }
    const keep = Object.entries(all).sort((x, y) => (Number(y[1]?.at) || 0) - (Number(x[1]?.at) || 0)).slice(0, MEMORY_STORE_SESSIONS)
    await $.store.set(MEMORY_STORE_KEY, Object.fromEntries(keep))
  } catch (err) {
    try { $.ui.log(`chronicle: memory state not saved (${why(err)})`, { to: 'debug' }) } catch {}
  }
}

function saveSoon($: EngineInterface) {
  const run = () => { saveChain = saveChain.then(() => saveState($)).catch(() => {}) }
  try { $.clock.after(1, run) } catch { run() }
}

// At session.end, the part that needs no $: a /clear takes the transcript the fork would read, so what is not formed
// is given up (the raw mirror keeps it, and the notes go out with the final flush). Never throws.
function memoryEndNotes(reason: string, endingSid: string) {
  try {
    if (reason !== 'clear') return
    const t = lastT
    const lost = [...queue.filter(c => c.sid === endingSid), ...(acc && acc.sid === endingSid && acc.rows > 0 ? [acc] : [])]
    for (const c of lost) memoryNote(t, c, 'memory_skipped', { reason: 'cleared before the memory formed', ordinal: c.ordinal })
    queue = queue.filter(c => c.sid !== endingSid)
    if (acc && acc.sid === endingSid) acc = undefined
    // Nothing saved for it may come back either: it is marked restored (no merge) and cleared (nothing pending).
    cleared.add(endingSid)
    restored.add(endingSid)
  } catch {}
}

// At session.end, the store part: any other end saves the state, so a resume can carry on. Never throws.
async function memoryEndSave($: EngineInterface, endingSid: string): Promise<void> {
  try {
    if (!memoriesOn) return
    // Merge the saved state first, so ending before this module life's first turn end cannot erase it.
    if (!restored.has(endingSid)) { restored.add(endingSid); await restoreState($, endingSid) }
    await saveState($)
  } catch {}
}

// ---------- assembly: compaction from the assistant's own memories ----------
// The older part of the context (before the safe cut) is replaced by the L1 memories of it, when, and only when, they
// provably cover it. A SessionMessage carries no uuid or time, so the proof has two halves:
//  (1) in the archive: the session's signed memories form a chain over the mirror's counted rows (main-loop rows
//      other than compaction rows and chronicle's own tagged rows) from its very first row, each memory starting on the row
//      after the last one ended and carrying the next chunk number (ordinal), no hole. A failed, skipped, stale or
//      lost chunk, rows lost to a reload, or a mirror that starts later all break it.
//  (2) in the context: the end of the latest chained memory is found among the older messages by its final reply
//      (last_assistant_text_hash for a reply of 40+ characters, else a 40+ character last_text_snippet), exactly once
//      in the whole transcript, on a real turn boundary, AND followed by the row the mirror holds right after that
//      memory (the forward edge), so a later identical reply cannot stand in for one that was summarized away.
//      The context must start where the chain starts: either at the chain's first row in a session the mirror saw
//      begin (a session_fresh mark, no earlier compaction, a 40+ character opening or two matching rows), or with
//      the memories block an earlier assembly put there (whose memories must be the chain's first ones).
// Then messages [0..k] are covered and dropped; [k+1 .. cut-1] is the gap; [cut..] the raw tail.
// Small gap: [block, ack, ...gap raw, ...tail], no model call. Large gap: one next() over the gap alone.
// Anything short of that proof: the split compaction, unchanged.

export const MEMORY_BLOCK_HEADER = '[chronicle — my own memories of this session so far, oldest first]'
const MEMORY_BLOCK_FOOTER = `[end of my memories. The conversation continues raw from where they end. Any stretch can be brought back whole with recall (${RECALL_TOOL}).]`
export const MEMORY_ACK = "I have them: my own memories of this work, up to here. I carry on from where they end."
// Acknowledgements written by earlier builds. A row or a block that carries one is still recognised as chronicle's own.
export const LEGACY_MEMORY_ACKS: readonly string[] = ["I have them: my own memories of us, up to here. I carry on from where they end."]
const GAP_NOTE = "\n\nThis slice is only the most recent uncovered stretch of the session. Everything before it is kept in context as the assistant's own memories, right before your summary, so summarize this slice alone and begin where it begins."
const DEFAULT_GAP_RAW_MAX = 12_000
const BLOCK_SHARE_OF_WINDOW = 0.25 // past it, the memories need L2 merges (not built), so the split compaction runs instead
const BLOCK_MAX_NO_WINDOW = 50_000
const START_SEARCH_MESSAGES = 6 // the chain's first row is looked for among the first few messages
const START_PREFIX_MAX_TOKENS = 1500 // engine boilerplate the mirror skips may sit ahead of it, nothing more
const FORWARD_LOOK = 3 // the next mirrored row may sit behind up to two injected user rows the mirror did not keep
const FLUSH_WAIT_MS = 2000 // compaction waits this long at most for a flush another dispatch started
const MIN_BUDGET_AFTER_PLAN_MS = 4000
const MIRROR_ROW_HEAD = '^\\{"t":[0-9]+,"sid":"[^"]*","uuid":"[^"]*","door":"[^"]*"(,"chronicle":"[a-z]+")?'
const MIRROR_ROW_HEAD_JS = /^\{"t":[0-9]+,"sid":"([^"]*)","uuid":"([^"]*)","door":"([^"]*)"(?:,"chronicle":"([a-z]+)")?/
const MIRROR_EVENT_HEAD = '^\\{"t":[0-9]+,"event":"(compaction|session_fresh)"'
const MIRROR_EVENT_HEAD_JS = /^\{"t":[0-9]+,"event":"(compaction|session_fresh)"/

export type MemRow = {
  t: number; text: string; from_uuid: string; to_uuid: string; t_from: number; t_to: number
  first_text_snippet?: string; last_text_snippet?: string; last_assistant_text_hash?: string; source_hash?: string
  ordinal?: number; answer_chars?: number
}
export type SeqRow = { uuid: string; door: string; chronicle?: string }
// What the mirror says about the session beyond its row order (memoryEvidence reads it; planAssembly trusts nothing else).
export type MirrorFacts = {
  freshMarked: boolean // a session_fresh mark stands before the first counted row: the mirror saw the session begin
  compacted: boolean // a compaction row or a compaction event line exists for the session
  anchors: ReadonlyMap<string, string> // uuid -> anchorOf(row), for the rows planAssembly needs to see
}
export type AssemblyPlan = {
  memories: MemRow[]; k: number; gap: SessionMessage[]; gapTokens: number; blockText: string; blockTokens: number
  mode: 'fresh' | 'reassembled'; start: number; gapRaw: boolean
}

let assembleWarned = false

// 'memories' / 'ack' for chronicle's own assembled rows, 'fork' for a memory formation that surfaced as a row, else undefined. Pure.
export function synthKind(type: unknown, text: string): string | undefined {
  if (text.startsWith(MEMORY_MARKER)) return 'fork'
  if (type === 'user' && text.startsWith(MEMORY_BLOCK_HEADER)) return 'memories'
  if (type === 'assistant' && (text.trim() === MEMORY_ACK || LEGACY_MEMORY_ACKS.includes(text.trim()))) return 'ack'
  return undefined
}

// A memory's heading in the block: `— <span> (<zone>) —`. Older blocks wrote `(London)` there; any zone name matches.
const BLOCK_HEADING = /^— .+ \([^()\n]+\) —$/gm

function blockNote(text: string): string {
  const n = (text.match(BLOCK_HEADING) ?? []).length
  return `${MEMORY_BLOCK_HEADER} (${n} memor${n === 1 ? 'y' : 'ies'}; their full texts are in the memories file)`
}

function spanLabel(from: number, to: number): string {
  if (!from || !to) return `${stampOf(from || to)}`
  const a = stampOf(from), b = stampOf(to)
  return a.slice(0, 10) === b.slice(0, 10) ? `${a} to ${b.slice(11)}` : `${a} to ${b}`
}

export function memoryBlock(mems: readonly MemRow[]): string {
  const parts = [MEMORY_BLOCK_HEADER]
  for (const m of mems) parts.push('', `— ${spanLabel(m.t_from, m.t_to)} (${zone}) —`, m.text.trim())
  parts.push('', MEMORY_BLOCK_FOOTER)
  return parts.join('\n')
}

function textOnlyAssistant(m: SessionMessage | undefined): boolean {
  return !!m && m.role === 'assistant' && !(m.toolUses && m.toolUses.length)
}

// How a kept message (raw tail, raw gap) is handed back. A message re-persisted under its handle keeps its ORIGINAL
// message.id, and below 5 MiB the resume loader merges it with its pre-compaction original (doubled text and thinking
// in the request, seen in live testing). A tool-result row kept under its handle also keeps its old parentUuid (the
// resume-relink bug). So every kept message is rebuilt from role, text and tool blocks (fresh ids, thinking dropped),
// except:
//  - a prompt (a user message with no tool results): a user row has no message.id to merge on, and its handle carries
//    its attachments (the startup listings) along;
//  - an assistant message of a tool loop still open at the end of the transcript (keepAssistant): a tool-use
//    continuation needs its thinking, so it keeps the plain behaviour (it may double on a resume; a 400 would be worse).
// A rebuilt assistant message with neither text nor tool uses (a thinking-only row) is left out: built, it would be empty.
function keptForm(m: SessionMessage, keepAssistant: boolean): SessionMessage | undefined {
  if (m.role === 'user' && !(m.toolResults && m.toolResults.length)) return m
  if (m.role === 'assistant' && keepAssistant) return m
  const { handle: _old, ...fresh } = m
  if (fresh.role === 'assistant' && !String(fresh.text ?? '').trim() && !(fresh.toolUses && fresh.toolUses.length)) return undefined
  return fresh
}

// True when the transcript ends inside a tool loop (a compaction mid-turn): its last message is a tool result, or an
// assistant message with tool uses.
export function turnOpen(msgs: readonly SessionMessage[]): boolean {
  const last = msgs[msgs.length - 1]
  if (!last) return false
  return last.role === 'user' ? !!(last.toolResults && last.toolResults.length) : !!(last.toolUses && last.toolUses.length)
}

// The kept messages as handed back (see keptForm). With `open`, the assistant messages from the last turn start in
// `msgs` (a prompt after a text-only assistant message, else msgs[0], which is always a turn start here) keep theirs.
export function keptMessages(msgs: readonly SessionMessage[], open: boolean): SessionMessage[] {
  let from = msgs.length
  if (open) {
    from = 0
    for (let i = msgs.length - 1; i >= 1; i--) if (isPrompt(msgs[i]!) && textOnlyAssistant(msgs[i - 1])) { from = i; break }
  }
  const out: SessionMessage[] = []
  for (let i = 0; i < msgs.length; i++) {
    const k = keptForm(msgs[i]!, i >= from)
    if (k) out.push(k)
  }
  return out
}

// What core hands back after its summary, in the kept form (seen in live testing: core kept the last older reply
// under its handle, re-persisted with its earlier message.id, the resume-doubling shape below 5 MiB). The summary is a
// user message with no tool results, so it keeps its own handle. Called after next() resolved: never throws; on any
// doubt the messages go up exactly as core gave them (the plain behaviour).
export function coreKept(msgs: readonly SessionMessage[]): SessionMessage[] {
  try {
    if (!msgs.every(m => !!m && typeof m === 'object')) return [...msgs]
    return keptMessages(msgs, false)
  } catch {
    return [...msgs]
  }
}

// A context message's anchor built the way the mirror built its row's: text, else the first tool use, else the first
// tool result, each redacted first.
export function messageAnchor(m: SessionMessage | undefined): string {
  if (!m) return ''
  if (String(m.text ?? '').trim()) return msgAnchor(m.text)
  const u = m.toolUses?.[0]
  // built the way blockText stored it: a secret-bearing call is kept as its summary, never its arguments
  if (u) {
    const body = secretBearing(u.tool, u.input)
      ? redact(secretCallSummary(u.tool, u.input))
      : redact(JSON.stringify(redactDeep(u.input ?? {})))
    return flat(`${u.tool}(${body.slice(0, TOOL_INPUT_CHARS)})`).slice(0, ANCHOR_CHARS)
  }
  const r = m.toolResults?.[0]
  if (r) {
    if (typeof r.tool_use_id === 'string' && recallUses.has(r.tool_use_id)) return flat(RECALL_RESULT_NOTE).slice(0, ANCHOR_CHARS)
    if (typeof r.tool_use_id === 'string' && secretUses.has(r.tool_use_id)) return flat(WITHHELD).slice(0, ANCHOR_CHARS)
    return flat(redact(String(r.text ?? '')).slice(0, RESULT_CHARS)).slice(0, ANCHOR_CHARS)
  }
  return ''
}

// open[i] > 0 when a tool_use before i has its result at or after i (the same count splitPoint keeps).
function openPairs(msgs: readonly SessionMessage[]): Int32Array {
  const resultAt = new Map<string, number>()
  msgs.forEach((m, i) => { for (const r of m.toolResults ?? []) resultAt.set(r.tool_use_id, i) })
  const diff = new Int32Array(msgs.length + 2)
  msgs.forEach((m, j) => {
    for (const u of m.toolUses ?? []) {
      const k = resultAt.get(u.tool_use_id)
      if (k !== undefined && k > j) { diff[j + 1]!++; diff[k + 1]!-- }
    }
  })
  const open = new Int32Array(msgs.length + 1)
  for (let i = 0, s = 0; i <= msgs.length; i++) { s += diff[i]!; open[i] = s }
  return open
}

// Where each final reply could sit: hash of the reply as turn.complete gave it (redacted and clipped like the memory
// row), for the message alone and for a short run of text-only assistant messages ending at it (a reply split over blocks).
function replyIndex(msgs: readonly SessionMessage[]): { byHash: Map<string, number[]>; bySnippet: Map<string, number[]> } {
  const byHash = new Map<string, number[]>()
  const bySnippet = new Map<string, number[]>()
  const add = (map: Map<string, number[]>, key: string, i: number) => {
    const l = map.get(key)
    if (!l) map.set(key, [i]); else if (l[l.length - 1] !== i) l.push(i)
  }
  msgs.forEach((m, i) => {
    if (!textOnlyAssistant(m) || !String(m.text ?? '').trim()) return
    const own = String(m.text)
    const hashes = new Set<string>([hash16(answerForm(own))])
    for (let j = i - 1, n = 0; j >= 0 && n < 3 && textOnlyAssistant(msgs[j]) && String(msgs[j]!.text ?? '').trim(); j--, n++) {
      for (const sep of ['\n', '\n\n', '']) {
        const parts: string[] = []
        for (let x = j; x <= i; x++) parts.push(String(msgs[x]!.text))
        hashes.add(hash16(answerForm(parts.join(sep))))
      }
    }
    for (const h of hashes) add(byHash, h, i)
    add(bySnippet, msgAnchor(own), i)
  })
  return { byHash, bySnippet }
}

// The message holding a memory's final reply: a single match in the whole transcript, else undefined.
// Neither a hash nor a snippet of a reply under 40 characters is trusted: short replies repeat.
function locateEnd(mem: MemRow, idx: ReturnType<typeof replyIndex>): number | undefined {
  if (mem.last_assistant_text_hash && (mem.answer_chars ?? 0) >= END_SNIPPET_MIN_CHARS) {
    const l = idx.byHash.get(mem.last_assistant_text_hash)
    if (l && l.length === 1) return l[0]
    if (l && l.length > 1) return undefined
  }
  const sn = mem.last_text_snippet
  if (sn && sn.length >= END_SNIPPET_MIN_CHARS) {
    const l = idx.bySnippet.get(sn)
    if (l && l.length === 1) return l[0]
  }
  return undefined
}

// The mirror's counted rows in order (compaction rows, chronicle's own tagged rows and repeats skipped).
function countedOrder(seq: readonly SeqRow[]): { order: string[]; pos: Map<string, number> } {
  const pos = new Map<string, number>()
  const order: string[] = []
  for (const r of seq) {
    if (r.door === 'compaction' || r.chronicle || pos.has(r.uuid)) continue
    pos.set(r.uuid, order.length)
    order.push(r.uuid)
  }
  return { order, pos }
}

// The memories as a chain over the mirror's counted rows from its first row, numbered 0, 1, 2, ... with no gap, or
// why not. Pure.
export function memoryChain(mems: readonly MemRow[], seq: readonly SeqRow[]): MemRow[] | string {
  const { pos } = countedOrder(seq)
  const seen = new Set<string>()
  const placed: { m: MemRow; pf: number; pt: number }[] = []
  for (const m of mems) {
    const key = `${m.from_uuid}|${m.to_uuid}`
    if (seen.has(key)) continue
    seen.add(key)
    const pf = pos.get(m.from_uuid), pt = pos.get(m.to_uuid)
    // A row the mirror never got (lost at exit, seen in live testing) is a hole: skip it; the ordinal check below stops the chain there.
    if (pf === undefined || pt === undefined || pt < pf) continue
    placed.push({ m, pf, pt })
  }
  if (!placed.length) return 'no memories for this session yet'
  placed.sort((a, b) => a.pf - b.pf || a.m.t - b.m.t)
  if (placed[0]!.pf !== 0) return 'my memories do not start at the first row of this session'
  if (placed[0]!.m.ordinal !== 0) return 'my first memory is not chunk 0 of this session'
  const chain = [placed[0]!]
  for (const x of placed.slice(1)) {
    const last = chain[chain.length - 1]!
    // A hole (failed, skipped, stale or lost chunk, even one whose rows the mirror never got) or an overlap:
    // only what came before counts.
    if (x.pf !== last.pt + 1 || !Number.isInteger(x.m.ordinal) || x.m.ordinal !== last.m.ordinal! + 1) break
    chain.push(x)
  }
  return chain.map(x => x.m)
}

// The whole plan from data, or why not. Pure: everything it needs is passed in. Facts left out count against.
export function planAssembly(
  msgs: readonly SessionMessage[], cut: number, mems: readonly MemRow[], seq: readonly SeqRow[],
  opts: { gapRawMax: number; blockMax: number; facts?: MirrorFacts },
): AssemblyPlan | string {
  if (cut < 1 || cut > msgs.length) return 'no safe cut'
  const chain = memoryChain(mems, seq)
  if (typeof chain === 'string') return chain
  const facts: MirrorFacts = opts.facts ?? { freshMarked: false, compacted: true, anchors: new Map() }
  const { order, pos } = countedOrder(seq)
  const open = openPairs(msgs)
  const resultAt = new Map<string, number>()
  msgs.forEach((m, i) => { for (const r of m.toolResults ?? []) resultAt.set(r.tool_use_id, i) })
  // A cut at i is a real turn boundary: same three conditions as splitPoint.
  const boundaryAt = (i: number) => i === cut || (i < cut && isPrompt(msgs[i]!) && open[i] === 0 && textOnlyAssistant(msgs[i - 1]))
  // The anchor of the mirror row right after `uuid`, when the mirror holds and showed it.
  const anchorAfter = (uuid: string): string | undefined => {
    const p = pos.get(uuid)
    const nxt = p === undefined ? undefined : order[p + 1]
    const a = nxt === undefined ? undefined : facts.anchors.get(nxt)
    return a ? a : undefined
  }

  // Where the context starts, against the chain.
  let mode: AssemblyPlan['mode']
  let start: number
  let inBlock = 0 // chain memories the old block already holds (reassembled)
  const first = msgs[0]!
  if (first.role === 'user' && String(first.text ?? '').startsWith(MEMORY_BLOCK_HEADER)) {
    if (!textOnlyAssistant(msgs[1])) return 'an old memories block without its acknowledgement'
    const old = String(first.text)
    let at = 0
    for (const m of chain) {
      const probe = m.text.trim().slice(0, 200)
      const found = old.indexOf(probe, at)
      if (found < 0) break
      inBlock++
      at = found + probe.length
    }
    if (!inBlock) return 'the old memories block holds none of the archived memories'
    if ((old.match(BLOCK_HEADING) ?? []).length !== inBlock) return 'the old memories block holds memories the archive chain does not'
    for (const m of chain.slice(inBlock)) if (old.includes(m.text.trim().slice(0, 200))) return 'the old memories block does not match the archive'
    mode = 'reassembled'
    start = 2
  } else {
    // Fresh: the mirror must have seen this session begin, and nothing may have been compacted away before now.
    if (!facts.freshMarked) return 'no fresh-start mark for this session (its mirror may have begun mid-session)'
    if (facts.compacted) return 'this session was compacted before, and its context does not open with my memories block'
    const want = chain[0]!.first_text_snippet
    if (!want) return 'my first memory has no opening snippet to find'
    let s = -1
    let prefix = 0
    for (let i = 0; i < Math.min(START_SEARCH_MESSAGES, cut); i++) {
      if (msgAnchor(msgs[i]!.text) === want) { s = i; break }
      prefix += estimate(msgs[i]!, resultAt)
    }
    if (s < 0) return 'the context does not start where my first memory starts (an earlier compaction, or rows from before the mirror)'
    if (prefix > START_PREFIX_MAX_TOKENS) return 'too much ahead of where my first memory starts'
    if (want.length < END_SNIPPET_MIN_CHARS) {
      // A short opening ("ok", "reloaded!") repeats: the second row must match too.
      const second = order[1] === undefined ? undefined : facts.anchors.get(order[1])
      if (!second || messageAnchor(msgs[s + 1]) !== second) return 'my first memory opens with a short row, and the row after it does not match'
    }
    mode = 'fresh'
    start = s
  }

  // The forward edge: what follows message i (past up to two injected user rows) must be the mirror row that follows
  // the memory's last row.
  const forwardOk = (i: number, want: string | undefined): boolean => {
    if (!want) return false
    for (let x = i + 1; x < msgs.length && x <= i + FORWARD_LOOK; x++) {
      const m = msgs[x]!
      if (messageAnchor(m) === want) return true
      if (m.role !== 'user' || (m.toolResults && m.toolResults.length)) return false
    }
    return false
  }

  // The latest chained memory whose end is found once, among the older messages, on a turn boundary, with its
  // forward edge in place.
  const idx = replyIndex(msgs)
  let k = -1
  let upto = -1 // chain index of the last memory the block holds
  for (let j = chain.length - 1; j >= (mode === 'reassembled' ? inBlock : 0); j--) {
    const i = locateEnd(chain[j]!, idx)
    if (i === undefined || i >= cut) continue // not found, ambiguous, or it ends inside the tail
    if (i < start || !textOnlyAssistant(msgs[i]) || !boundaryAt(i + 1)) continue
    if (!forwardOk(i, anchorAfter(chain[j]!.to_uuid))) continue // an identical reply elsewhere, or no next row known
    k = i
    upto = j
    break
  }
  if (k < 0) {
    if (mode === 'fresh') return 'could not find where my latest memory ends among the older messages'
    k = 1 // no new memory located: the old block stands for itself, rebuilt from the archive
    upto = inBlock - 1
  }
  const memories = chain.slice(0, upto + 1)
  const gap = msgs.slice(k + 1, cut)
  const gapTokens = gap.reduce((n, m) => n + estimate(m, resultAt), 0)
  const blockText = memoryBlock(memories)
  const blockTokens = Math.ceil(blockText.length / 3.6)
  if (blockTokens > opts.blockMax) return `my memories block would be ${blockTokens} tokens, over ${opts.blockMax} (L2 merges are not built yet)`
  return { memories, k, gap, gapTokens, blockText, blockTokens, mode, start, gapRaw: gapTokens <= opts.gapRawMax }
}

// Waits for the flush chain at most `ms` (a flush another dispatch started must not eat this hook's own time).
// Rows it is still writing are read from `inflight`, rows not yet taken from `buf`. Never throws.
async function flushBounded($: EngineInterface, ms: number): Promise<boolean> {
  let timer: { cancel: () => void } | undefined
  try {
    const timeout = new Promise<boolean>(res => {
      try { timer = $.clock.after(ms, () => res(false)) } catch { res(false) }
    })
    return await Promise.race([scheduleFlush($, 5000).then(() => true, () => true), timeout])
  } catch {
    return false
  } finally {
    try { timer?.cancel() } catch {}
  }
}

// Reads this session's signed memories, the mirror's row order and facts. Returns why not, as a string, on anything
// short of a clean read.
async function memoryEvidence($: EngineInterface, root: string, sessionId: string): Promise<{ mems: MemRow[]; seq: SeqRow[]; facts: MirrorFacts } | string> {
  await flushBounded($, FLUSH_WAIT_MS)
  // What is not surely on disk, taken now, before the files are read: a row written meanwhile is then read twice
  // (repeats are skipped), never missed in the middle of the order. Rows appended after this are the newest, at the end.
  const pending = [...inflight, ...buf].filter(x => x.sid === sessionId)
  const listed = await archiveFiles($, root)
  if (listed.missing) return 'no archive yet'
  if (listed.error) return `archive not listed (${listed.error})`
  const name = safe(sessionId)
  const byDay = (a: { day: string }, b: { day: string }) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0)
  const memFiles = listed.files.filter(f => f.name === `${name}.memories.jsonl`).sort(byDay)
  const rowFiles = listed.files.filter(f => f.name === `${name}.jsonl`).sort(byDay)
  const pendingLines = (memories: boolean) => pending.filter(x => (memories ? x.suffix === '.memories' : !x.suffix)).map(x => x.line)
  const memLines: string[] = []
  if (memFiles.length) {
    const r = await readWhole($, memFiles.map(f => f.path))
    if (r.exitCode !== 0 || r.isStdoutTruncated) return `memories not read (exit ${r.exitCode})`
    memLines.push(...r.stdout.split('\n'))
  }
  memLines.push(...pendingLines(true))
  const key = await macKeyOf($)
  if (!key) return 'no memory signing key'
  const mems: MemRow[] = []
  let unsigned = 0
  for (const line of memLines) {
    if (!line.trim()) continue
    let o: any
    try { o = JSON.parse(line) } catch { continue }
    if (!o || o.event !== 'memory' || Number(o.level ?? 1) !== 1 || o.sid !== sessionId) continue
    if (typeof o.text !== 'string' || !o.text.trim() || typeof o.from_uuid !== 'string' || typeof o.to_uuid !== 'string') continue
    if (typeof o.mac !== 'string' || o.mac !== (await hmacHex(key, macMessage(o)))) { unsigned++; continue }
    mems.push({
      t: Number(o.t) || 0, text: o.text, from_uuid: o.from_uuid, to_uuid: o.to_uuid, t_from: Number(o.t_from) || 0, t_to: Number(o.t_to) || 0,
      first_text_snippet: typeof o.first_text_snippet === 'string' ? o.first_text_snippet : undefined,
      last_text_snippet: typeof o.last_text_snippet === 'string' ? o.last_text_snippet : undefined,
      last_assistant_text_hash: typeof o.last_assistant_text_hash === 'string' ? o.last_assistant_text_hash : undefined,
      source_hash: typeof o.source_hash === 'string' ? o.source_hash : undefined,
      ordinal: Number.isInteger(o.ordinal) ? o.ordinal : undefined,
      answer_chars: Number.isFinite(o.answer_chars) ? o.answer_chars : undefined,
    })
  }
  if (!mems.length) return unsigned ? `${unsigned} memories failed their signature check` : 'no memories for this session yet'
  if (!rowFiles.length) return 'no mirror file for this session'
  const r = await rgRun($, ['--no-config', '--no-ignore', '--hidden', '--sort', 'path', '--only-matching', '--no-filename', '--no-line-number', '--no-heading', '--color', 'never', '-e', MIRROR_ROW_HEAD, '-e', MIRROR_EVENT_HEAD, '--', ...rowFiles.map(f => f.path)], RECALL_RUN_MS)
  if (r.exitCode > 1) return `mirror not read (${r.stderr.trim().split('\n')[0]?.slice(0, 200) || `exit ${r.exitCode}`})`
  if (r.isStdoutTruncated) return 'mirror too large to read in one pass'
  const seq: SeqRow[] = []
  let freshMarked = false
  let compacted = false
  let counted = 0
  const take = (line: string) => {
    const ev = MIRROR_EVENT_HEAD_JS.exec(line)
    if (ev) {
      if (ev[1] === 'compaction') compacted = true
      else if (counted === 0) freshMarked = true
      return
    }
    const x = MIRROR_ROW_HEAD_JS.exec(line)
    if (!x || x[1] !== sessionId) return
    seq.push({ uuid: x[2]!, door: x[3]!, chronicle: x[4] })
    if (x[3] === 'compaction') compacted = true
    else if (!x[4]) counted++
  }
  for (const line of r.stdout.split('\n')) if (line) take(line)
  for (const line of pendingLines(false)) take(line)

  // The rows planAssembly compares against the context: the second counted row, and the row after each memory.
  const { order, pos } = countedOrder(seq)
  const wanted = new Set<string>()
  if (order[1] !== undefined) wanted.add(order[1])
  for (const m of mems) {
    const p = pos.get(m.to_uuid)
    if (p !== undefined && order[p + 1] !== undefined) wanted.add(order[p + 1]!)
  }
  const anchors = new Map<string, string>()
  const see = (line: string) => {
    let o: any
    try { o = JSON.parse(line) } catch { return }
    if (o && typeof o.uuid === 'string' && wanted.has(o.uuid) && o.sid === sessionId && !anchors.has(o.uuid)) anchors.set(o.uuid, anchorOf(o))
  }
  if (wanted.size) {
    const argv = ['--no-config', '--no-ignore', '--hidden', '--fixed-strings', '--no-filename', '--no-line-number', '--no-heading', '--color', 'never']
    for (const u of [...wanted].slice(-400)) argv.push('-e', `"uuid":"${u}"`)
    const a = await rgRun($, [...argv, '--', ...rowFiles.map(f => f.path)], RECALL_RUN_MS)
    if (a.exitCode <= 1) for (const line of a.stdout.split('\n')) if (line) see(line)
  }
  for (const line of pendingLines(false)) see(line)
  return { mems, seq, facts: { freshMarked, compacted, anchors } }
}

// Which compactions may assemble: chronicle.json memories.assemble (false, "manual" the default, true or "auto").
export function assemblyAllowed(setting: unknown, trigger: string): string | undefined {
  if (setting === false) return 'memories.assemble is off'
  const auto = setting === true || setting === 'auto'
  if (!auto && trigger !== 'manual') return `assembly runs only for a manual /compact for now (this one is ${trigger})`
  return undefined
}

// The plan for this compaction, or why not. Never throws.
async function assemblyFor($: EngineInterface, cfg: Cfg, msgs: readonly SessionMessage[], cut: number, window: number, sessionId: string, trigger: string): Promise<AssemblyPlan | string> {
  try {
    if (!cfg.memoriesEnabled) return 'memories are off (memories.enabled)'
    const off = assemblyAllowed(cfg.assembleWithMemories, trigger)
    if (off) return off
    if (!sessionId || sessionId === 'unknown') return 'no session id'
    const ev = await memoryEvidence($, cfg.archiveRoot.replace(/\/+$/, ''), sessionId)
    if (typeof ev === 'string') return ev
    return planAssembly(msgs, cut, ev.mems, ev.seq, {
      gapRawMax: posInt(cfg.gapRawMaxTokens, DEFAULT_GAP_RAW_MAX),
      blockMax: window > 0 ? Math.floor(window * BLOCK_SHARE_OF_WINDOW) : BLOCK_MAX_NO_WINDOW,
      facts: ev.facts,
    })
  } catch (err) {
    const line = `assembly not planned (${why(err)}); the split compaction runs instead`
    try { $.ui.log(`chronicle: ${line}`, { to: 'debug' }) } catch {}
    if (!assembleWarned) { assembleWarned = true; try { $.ui.log(`chronicle: ${line}`) } catch {} }
    return 'error'
  }
}

// ---------- the fresh-start mark ----------
// {event:"session_fresh"} in a session's mirror file, ahead of its first row: the mirror saw that session begin.
// Written at session.start when the session has no messages yet, and before the first row after a /clear. Fresh-mode
// assembly requires it, so a mirror that began mid-session (a reload, a moved archive, lost rows) never passes as whole.

let pendingFresh: string | undefined // the id a /clear ended: the first row under another id opens a fresh session

function pushFresh(t: number, id: string, how: string) {
  push({ day: dayOf(t), sid: id, line: JSON.stringify({ t, event: 'session_fresh', sid: id, how }) + '\n' }, true)
}

async function markFreshAtStart($: EngineInterface): Promise<void> {
  const msgs = await $.session.messages()
  if (!Array.isArray(msgs) || msgs.length !== 0) return
  const id = String(await $.session.id())
  if (!id || id === 'unknown') return
  const t = await $.clock.now()
  lastT = Math.max(lastT, t)
  pushFresh(t, id, 'start')
}

// ---------- hooks ----------

let rgChecked = false
async function checkRg($: EngineInterface): Promise<void> {
  if (rgChecked || isWin) return // Windows searches the mirror through $.fs: no ripgrep to look for
  rgChecked = true
  try { await rgRun($, ['--version'], 5000) } catch (err) {
    try { $.ui.log(`chronicle: ${err instanceof RgMissing ? err.message : `could not run ripgrep (${why(err)})`}`) } catch {}
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    exitingSid = undefined
    const cfg = await cfgOf($)
    ensureTimer($, true)
    // Awaited here so recall is listed by turn one. A failure costs the tool, never the session. With no archive
    // command only the session fields are offered, and the description says so.
    const spec = recallSpec(cfg.archiveCommand.length > 0)
    try { await $.tool.register({ name: 'recall', description: spec.description, inputSchema: spec.inputSchema }) } catch (err) { warnOnce($, `recall tool not registered (${why(err)})`) }
    // Session recall and assembly shell out to ripgrep: say so once, at the start, if it is missing.
    try { await checkRg($) } catch {}
    // A session with no messages yet is one the mirror sees from its very first row: say so, so assembly may later
    // trust that the mirror starts where the session starts. A reload or a resume (messages already there) never marks.
    if (cfg.mirrorEnabled && cfg.memoriesEnabled) { try { await markFreshAtStart($) } catch {} }
    if (cfg.squishEnabled) {
      try { await $.command.register({ name: 'squish', description: "Compact now, using chronicle's steered compaction (the same as /compact). /squish [focus] · /squish status · /squish auto on|off", argumentHint: '[focus | status | auto on|off]' }) } catch (err) { warnOnce($, `/squish not registered (${why(err)})`) }
    }
    return next(e)
  })

  // Answers for itself: a plugin tool has nothing beneath it. Never throws; a failure is the text the assistant reads.
  // Without an archive command the default source is the session record, and asking for the archive says how to set one up.
  // No matcher: a name the engine's generated tool types do not list yet (this plugin's own tool, before it first
  // loads) sends the type checker into an exponential search, so the hook tests the name itself.
  on('tool.call', async ($, e, next) => {
    if (String(e.tool) !== RECALL_TOOL) return next(e)
    try {
      const input = e as unknown as RecallInput
      const cfg = await cfgOf($)
      const source = argStr(input.source) ?? (cfg.archiveCommand.length ? 'archive' : 'session')
      if (source === 'session') return { result: await recall($, input) }
      if (source !== 'archive') return { result: `recall: source "${source.slice(0, 40)}" is not archive or session.` }
      return { result: await recallArchive($, input) }
    } catch (err) {
      warnOnce($, `recall failed (${why(err)})`)
      return { result: `recall failed: ${why(err)}` }
    }
  })

  on('session.append', async ($, e, next) => {
    if (e.agentId) return next(e)
    try { await cfgOf($) } catch {} // the zone and the labels are known before the first row is stamped
    // Stamp before the row is stored (one clock call; the id is cached).
    let t = lastT
    let rowSid = sid ?? 'unknown'
    try {
      t = await $.clock.now()
      lastT = t
      if (!sid) sid = String(await $.session.id())
      rowSid = sid
    } catch {}
    // The first row after a /clear opens a session the mirror sees from its start: mark it before the row.
    try { if (pendingFresh && rowSid !== 'unknown' && rowSid !== pendingFresh) { pushFresh(t, rowSid, 'clear'); pendingFresh = undefined } } catch {}
    const stored = await next(e)
    // TODO: if the engine ever re-appends kept tail rows after a compaction, the mirror would hold them twice;
    //                dedupe on uuid then (a bounded recent-uuid set here, or at read time). Not observed in testing.
    try { mirror($, e, stored, t, rowSid) } catch {}
    try { if (e.door === 'compaction') noteCompaction() } catch {}
    return stored
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      turnPrompts.clear()
      try { flushSoon($) } catch {}
      // Seals a due memory chunk on this turn boundary and starts its formation in the background.
      try { await memoryTurnEnd($, e) } catch (err) { warnMem($, `memory bookkeeping failed at a turn end (${why(err)})`) }
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    // Rows already carry their own session id; the next row belongs to whatever session comes after this one.
    sid = undefined
    turnPrompts.clear()
    const reason = String(e.reason), ending = String(e.sessionId)
    if (reason === 'clear') pendingFresh = ending
    memoryEndNotes(reason, ending)
    // The rows matter more than the chunk state: the final flush and the state save run side by side under the
    // exit's one short bound, so a slow store cannot eat the last exchange.
    let ms = 1000
    try {
      const left = next.budget.remainingMs
      if (Number.isFinite(left)) ms = Math.max(200, Math.min(1000, left - 300))
    } catch {}
    try { await Promise.allSettled([scheduleFlush($, ms), memoryEndSave($, ending)]) } catch {}
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    if (e.agentId) return next(e)
    // A precomputed result would be a split of a conversation that keeps growing. Assemble only at compaction itself.
    if (e.trigger === 'precompute') return { skip: 'chronicle assembles at compaction time' }

    // Plan. Anything failing here goes to native compaction, steered if the config was read.
    let steer: string | undefined
    let older: SessionMessage[] | undefined
    let tail: SessionMessage[] = []
    let cfgSeen: Cfg | undefined // for the assembly below
    let windowSeen = 0
    let cutSeen = -1
    try {
      const cfg = await cfgOf($)
      cfgSeen = cfg
      steer = steerOf(cfg) + (e.instructions ? `\n\nAlso, from this compaction's own instructions: ${e.instructions}` : '')
      let window = 0
      try { window = (await $.session.usage()).context.window } catch {}
      windowSeen = window
      const cap = Math.min(cfg.maxTailTokens, window > 0 ? Math.floor(window * TAIL_SHARE_OF_WINDOW) : Infinity)
      const target = Math.min(cfg.tailTokens, cap)
      const cut = splitPoint(e.messages, target, cap)
      cutSeen = cut
      if (cut >= cfg.minOlderMessages) {
        older = e.messages.slice(0, cut)
        // Kept tail messages other than prompts are rebuilt without their handles (keptForm): a tool-result row
        // under its old handle keeps its OLD parentUuid (the resume-relink bug), and any assistant row under its old
        // handle keeps its OLD message.id, which a resume under 5 MiB merges with the original (doubled text and
        // thinking). Cost: rebuilt rows lose thinking and their structured toolUseResult record; the text the model
        // read is kept. A tool loop still open at the end keeps its assistant handles (its thinking is needed).
        tail = keptMessages(e.messages.slice(cut), turnOpen(e.messages))
      }
    } catch {}
    if (!older) return next(steer ? { ...e, instructions: steer } : e)

    // Stamp the log row now, so nothing after the summarizer needs a $ round trip.
    let t = lastT
    let rowSid = sid ?? 'unknown'
    try { t = await $.clock.now(); if (!sid) sid = String(await $.session.id()); rowSid = sid } catch {}

    // Assemble from the assistant's own memories when they provably cover the older part. Before any next(): a failure
    // anywhere in here falls through to the split compaction below, untouched.
    let plan: AssemblyPlan | string = 'not tried'
    if (cfgSeen && cutSeen > 0) plan = await assemblyFor($, cfgSeen, e.messages, cutSeen, windowSeen, rowSid, String(e.trigger))
    try {
      // Planning reads the archive; if it ate most of this hook's own time, the split is the safer finish.
      if (typeof plan !== 'string' && next.budget.remainingMs < MIN_BUDGET_AFTER_PLAN_MS) plan = `only ${Math.round(next.budget.remainingMs)} ms of hook time left after planning`
    } catch {}
    if (typeof plan === 'string') {
      try { $.ui.log(`chronicle: compaction not assembled (${plan}); split compaction`, { to: 'debug' }) } catch {}
    } else if (plan.gapRaw) {
      // No model call at all. Built whole before it is returned; if building throws, the split compaction runs.
      let built: SessionCompactResult | undefined
      try {
        built = { messages: [
          { role: 'user', text: plan.blockText, toolUses: [] },
          { role: 'assistant', text: MEMORY_ACK, toolUses: [] },
          ...keptMessages(plan.gap, false),
          ...tail,
        ] }
      } catch {}
      if (built) {
        try {
          push({ day: dayOf(t), sid: rowSid, line: JSON.stringify({ t, event: 'compaction', mode: 'assembled', trigger: e.trigger, context: plan.mode, memories: plan.memories.length, memories_tokens: plan.blockTokens, dropped: plan.k + 1, gap: plan.gap.length, gap_tokens: plan.gapTokens, kept_raw: tail.length, session: rowSid }) + '\n' })
          flushSoon($)
        } catch {}
        try { noteCompaction() } catch {}
        return built
      }
    } else {
      // A large gap: one summarizer call over the gap alone. The same single-call rules as the split below.
      let gapRes: SessionCompactResult
      try {
        gapRes = await next({ ...e, messages: plan.gap, instructions: steer + GAP_NOTE + TAIL_NOTE })
      } catch (err) {
        if (next.signal.aborted) return { skip: 'chronicle: compaction abandoned' }
        return next({ ...e, instructions: steer })
      }
      // From here: no second next, no throw.
      if (typeof gapRes.skip === 'string' || !Array.isArray(gapRes.messages)) return gapRes
      // Both token counts would measure only the gap's slice: left absent (allowed) rather than mislead.
      const { tokensAfter: _gapOnly, tokensBefore: _gapBefore, ...gapRest } = gapRes
      const gapOut = { ...gapRest, messages: [
        { role: 'user' as const, text: plan.blockText, toolUses: [] },
        { role: 'assistant' as const, text: MEMORY_ACK, toolUses: [] },
        ...coreKept(gapRes.messages),
        ...tail,
      ] }
      try {
        const summary = redact(String(gapRes.messages[0]?.text ?? '').split('\nIf you need specific details from before compaction')[0]!).slice(0, SUMMARY_IN_LOG_CHARS) || undefined
        push({ day: dayOf(t), sid: rowSid, line: JSON.stringify({ t, event: 'compaction', mode: 'assembled-gap', trigger: e.trigger, context: plan.mode, memories: plan.memories.length, memories_tokens: plan.blockTokens, dropped: plan.k + 1, gap: plan.gap.length, gap_tokens: plan.gapTokens, kept_raw: tail.length, summarized: plan.gap.length, core_kept: Math.max(0, gapRes.messages.length - 1), session: rowSid, summary }) + '\n' })
        flushSoon($)
      } catch {}
      try { noteCompaction() } catch {}
      return gapOut
    }

    // The one summarizer call. Only a failure here may fall back, and never once the dispatch was abandoned.
    let res: SessionCompactResult
    try {
      res = await next({ ...e, messages: older, instructions: steer + TAIL_NOTE })
    } catch (err) {
      if (next.signal.aborted) return { skip: 'chronicle: compaction abandoned' }
      return next({ ...e, instructions: steer })
    }

    // From here: no second next, no throw. Hand the result up as soon as it is built.
    if (typeof res.skip === 'string' || !Array.isArray(res.messages)) return res
    // Both token counts would measure only summary + older; leave them absent (allowed) rather than mislead.
    const { tokensAfter: _summaryOnly, tokensBefore: _olderOnly, ...rest } = res
    // Core's kept messages get the same rebuild as the raw tail (coreKept): a resume under 5 MiB cannot merge them.
    const out = { ...rest, messages: [...coreKept(res.messages), ...tail] }
    try {
      const summary = redact(String(res.messages[0]?.text ?? '').split('\nIf you need specific details from before compaction')[0]!).slice(0, SUMMARY_IN_LOG_CHARS) || undefined
      push({ day: dayOf(t), sid: rowSid, line: JSON.stringify({ t, event: 'compaction', trigger: e.trigger, kept_raw: tail.length, summarized: older.length, core_kept: Math.max(0, res.messages.length - 1), session: rowSid, summary }) + '\n' })
      flushSoon($)
    } catch {}
    try { noteCompaction() } catch {}
    return out
  })

  // ---------- /squish: compaction by hand, and the fill beside every message ----------
  // /squish runs /compact itself, so the trigger stays 'manual': the steered split and the memory assembly above both
  // run exactly as they do for a typed /compact. Not awaited, and started after the hook has answered.
  on('command.run', { command: 'squish' }, async ($, e) => {
    const args = String(e.args ?? '').trim()
    let u = ''
    try { const l = (await cfgOf($)).contextLine; u = await fillLine($, l.warnAt, l.urgentAt) } catch {}
    if (/^(status|\?)$/i.test(args)) return { text: u || 'context fill unknown' }
    const m = /^auto\s+(on|off)$/i.exec(args)
    if (m) {
      const want = (m[1] ?? "").toLowerCase() === 'on'
      try { await $.config.set({ key: 'autoCompact', value: want }); return { text: `Auto-compact is ${want ? 'on' : 'off'}.${want ? '' : ' Squishing is by hand now: /squish.'} ${u}`.trim() } }
      catch (err) { return { text: `Could not switch auto-compact (${why(err)}); use /config. ${u}`.trim() } }
    }
    // Started from a timer, after this hook has answered: the engine refuses a command.run raised from inside a
    // command.run hook (it would wait on the turn the hook is holding).
    const runCompact = () => { void Promise.resolve($.command.run({ command: 'compact', args })).catch(err => warnOnce($, `/squish could not run /compact (${why(err)})`)) }
    try { $.clock.after(1, runCompact) } catch { runCompact() }
    return { text: `Squishing… ${u}`.trim() }
  })

  on('prompt.submit', async ($, e, next) => {
    const kind = (e as any).origin?.kind
    if (!(kind === undefined || kind === 'composer' || kind === 'bridge')) return next(e)
    try {
      const l = (await cfgOf($)).contextLine
      if (!l.enabled) return next(e)
      const line = `[context · ${await fillLine($, l.warnAt, l.urgentAt)}]`
      return next({ ...e, context: [...(((e as any).context as string[]) ?? []), line] })
    } catch { return next(e) }
  })
}

// How full the window is, the way the status line has it. From warnAt (75 by default): squish soon. From urgentAt (90): squish now.
export async function fillLine($: EngineInterface, warnAt = 75, urgentAt = 90): Promise<string> {
  const c = (await $.session.usage()).context
  const k = (n: number) => (n >= 1e6 ? `${Math.round(n / 1e5) / 10}M` : `${Math.round(n / 1000)}k`)
  if (!c.tokens || !c.window) return `${c.window ? k(c.window) + ' window, ' : ''}not measured yet`
  const p = c.percent ?? Math.round((c.tokens / c.window) * 100)
  const flag = p >= urgentAt ? ' · SQUISH NOW' : p >= warnAt ? ' · squish soon' : ''
  return `${k(c.tokens)} of ${k(c.window)} (${p}%)${flag}`
}
