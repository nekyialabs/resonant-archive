// Shared by the tests: the world beneath the plugin (files, processes, usage, logs) held in memory.
import { mock } from 'claude-code/testing'

export type Proc = { exitCode: number; stdout?: string; stderr?: string; isStdoutTruncated?: boolean }
export type RigOptions = {
  config?: unknown // the chronicle.json content; undefined = no file
  files?: Record<string, string>
  now?: number
  home?: string
  env?: Record<string, string> // more of the environment beneath the plugin (HOME comes from `home`)
  sid?: string
  usage?: { tokens: number; window: number; percent?: number }
  // Answers a process.run; return undefined to fall through to the default (exit 0, nothing printed).
  proc?: (argv: readonly string[], stdin: string | undefined) => Proc | undefined | Promise<Proc | undefined>
}

// A test host on Windows hands the plugin's paths back with backslashes and a drive letter in front of a POSIX
// spelling (`/home/tester/x` arrives as `C:\home\tester\x`). Both spellings are the same file here: one key.
// A Windows path is not absolute on a POSIX host, so the engine may hand it over joined onto the cwd
// (/runner/work/…/C:/Users/x/…): drop everything before the drive letter, then the drive itself.
export const norm = (p: unknown): string =>
  String(p).replace(/\\/g, '/').replace(/^.*?\/(?=[A-Za-z]:\/)/, '').replace(/^[A-Za-z]:(?=\/)/, '')

export function rig(on: any, o: RigOptions = {}) {
  const files = new Map<string, string>(Object.entries(o.files ?? {}).map(([k, v]) => [norm(k), v]))
  const runs: { argv: readonly string[]; stdin?: string }[] = []
  const logs: string[] = []
  const compacts: string[] = []
  const configSets: { key: string; value: unknown }[] = []
  const registered: any[] = []
  const state = { usage: o.usage ?? { tokens: 0, window: 0 } as { tokens: number; window: number; percent?: number }, sid: o.sid ?? 'sess-1', failConfigSet: false, failCompact: false, failUsage: false }
  const clock = mock.clock(on, { now: o.now ?? Date.UTC(2026, 0, 5, 12, 0, 0) })
  const store = new Map<string, unknown>()
  on('store.get', async (_$: any, e: any) => ({ value: store.get(e.key) }))
  on('store.set', async (_$: any, e: any) => { store.set(e.key, e.value); return { value: undefined } })
  mock.env(on, { HOME: o.home ?? '/home/tester', ...(o.env ?? {}) })
  on('fs.read', async (_$: any, e: any) => {
    const path = norm(e.path)
    if (path.endsWith('/chronicle.json') && o.config !== undefined && !files.has(path)) return { value: typeof o.config === 'string' ? o.config : JSON.stringify(o.config) }
    const v = files.get(path)
    if (v === undefined) throw new Error(`ENOENT: no such file ${e.path}`)
    return { value: v }
  })
  on('process.run', async (_$: any, e: any) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin })
    const r = await o.proc?.(e.argv, e.init?.stdin)
    return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false, ...(r ?? {}) } }
  })
  on('session.usage', async () => {
    if (state.failUsage) throw new Error('usage unavailable')
    return { value: { startedAt: 0, rateLimits: [], context: { ...state.usage } } } as any
  })
  on('session.id', async () => ({ value: state.sid }))
  on('session.messages', async () => ({ value: [] }))
  on('ui.log', async (_$: any, e: any) => { logs.push(String(e.text ?? e.message ?? JSON.stringify(e))); return { value: undefined } })
  on('config.set', async (_$: any, e: any) => {
    if (state.failConfigSet) throw new Error('read-only')
    configSets.push({ key: e.key, value: e.value }); return { value: e.value }
  })
  on('tool.register', async (_$: any, e: any) => { registered.push(e); return { value: { tool: e.name } } })
  on('command.register', async (_$: any, e: any) => { registered.push({ command: e.name, ...e }); return { value: { command: e.name } } })
  on('session.start', async () => ({ cwd: '/work' }))
  // The rows appended: the kit's own session where it has one (2.1.299 on), else the row itself as stored.
  if (typeof (mock as any).session === 'function') (mock as any).session(on)
  else on('session.append', async (_$: any, e: any, next: any) => { try { return await next(e) } catch { return { message: e.message, uuid: e.uuid } } })
  on('session.end', async (_$: any, e: any) => ({ sessionId: e.sessionId }))
  on('turn.complete', async (_$: any, e: any) => ({ text: e.answer ?? '' }))
  on('command.run', { command: 'compact' }, async (_$: any, e: any) => {
    if (state.failCompact) throw new Error('compact refused')
    compacts.push(String(e.args)); return { text: 'compacted' }
  })
  return { files, runs, logs, compacts, configSets, registered, state, clock, store }
}

export const HOUR = 60 * 60 * 1000
export const usage = (tokens: number, window: number, percent?: number) => ({ tokens, window, percent })

// Lets work a hook started and did not await (a detached compact, a flush) finish.
export const settle = (ms = 40) => new Promise<void>(res => (globalThis as any).setTimeout(res, ms))

// $.command.run without the origin and presentation fields a real call site stamps (the kit fills them in).
export const cmd = ($: any, command: string, args = ''): Promise<{ text?: string }> => $.command.run({ command, args })
