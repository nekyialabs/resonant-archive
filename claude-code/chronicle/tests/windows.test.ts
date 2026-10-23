// The Windows host: no sh, cat or rg. The mirror is appended through PowerShell (an encoded script, stdin copied as
// bytes) and the archive is listed, searched and read through $.fs. The other tests run the POSIX path untouched.
import { test, expect } from 'claude-code/testing'
import { rig, settle, norm } from './harness'
import { b64utf16le } from '../hooks/register'

let n = 0
const uuid = () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`
const row = (text: string, role: 'user' | 'assistant' = 'user') => ({
  message: { type: role, role, content: [{ type: 'text', text }] },
  door: role === 'user' ? 'prompt' : 'response',
  origin: role === 'user' ? { kind: 'composer' } : { kind: 'model', model: 'm' },
  uuid: uuid(),
}) as any
const start = ($: any) => $.session.start({ source: 'startup', cwd: 'C:\\work' } as any)

const HOME = 'C:\\Users\\tester'
const ROOT = `${HOME}/.chronicle/archive`

// Decodes what -EncodedCommand carries, so a test can read the script.
function decode(b64: string): string {
  const T = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  const bytes: number[] = []
  let bits = 0, acc = 0
  for (const ch of b64) {
    if (ch === '=') break
    acc = (acc << 6) | T.indexOf(ch); bits += 6
    if (bits >= 8) { bits -= 8; bytes.push((acc >> bits) & 0xff) }
  }
  let s = ''
  for (let i = 0; i + 1 < bytes.length; i += 2) s += String.fromCharCode(bytes[i]! | (bytes[i + 1]! << 8))
  return s
}

// A rig on Windows: HOME unset, USERPROFILE and OS set, and $.fs.list/exists/write answered from the same map.
function winRig(on: any, o: Parameters<typeof rig>[1] = {}) {
  const r = rig(on, { home: HOME, env: { OS: 'Windows_NT', USERPROFILE: HOME }, ...o })
  const writes: { path: string; text: string }[] = []
  on('fs.exists', async (_$: any, e: any) => ({ value: r.files.has(norm(e.path)) }))
  on('fs.write', async (_$: any, e: any) => { r.files.set(norm(e.path), e.text); writes.push({ path: norm(e.path), text: e.text }); return { value: undefined } })
  on('fs.list', async (_$: any, e: any) => {
    const dir = norm(e.path).replace(/\/+$/, '')
    const seen = new Map<string, 'file' | 'dir'>()
    for (const p of r.files.keys()) {
      if (!p.startsWith(`${dir}/`)) continue
      const rest = p.slice(dir.length + 1).split('/')
      seen.set(rest[0]!, rest.length === 1 ? 'file' : 'dir')
    }
    if (!seen.size && dir !== norm(ROOT)) throw new Error(`ENOENT: no such directory ${dir}`)
    return { value: [...seen].map(([name, kind]) => ({ name, kind, size: 0, mtimeMs: 0, isLink: false })) }
  })
  return { ...r, writes }
}

test('b64utf16le encodes the way PowerShell -EncodedCommand expects', () => {
  // "hi" as UTF-16LE is 68 00 69 00 → aABpAA==
  expect(b64utf16le('hi')).toBe('aABpAA==')
  expect(b64utf16le('')).toBe('')
  expect(b64utf16le('é')).toBe('6QA=')
})

test('on Windows a row is appended through PowerShell, no sh', async ($, on) => {
  const r = winRig(on, { config: { timezone: 'UTC' }, now: Date.UTC(2026, 0, 5, 12, 0, 0) })
  await start($)
  await $.session.append(row("hello there, it's me"))
  await r.clock.advance(31_000); await settle()
  expect(r.runs.some(x => x.argv[0] === 'sh')).toBe(false)
  const ps = r.runs.filter(x => x.argv[0] === 'powershell.exe')
  expect(ps.length).toBe(1)
  const argv = ps[0]!.argv
  expect(argv).toContain('-NoProfile')
  expect(argv).toContain('-EncodedCommand')
  const script = decode(String(argv[argv.indexOf('-EncodedCommand') + 1]))
  expect(script).toContain(`CreateDirectory('${ROOT}/2026-01-05')`)
  expect(script).toContain(`Open('${ROOT}/2026-01-05/sess-1.jsonl', [System.IO.FileMode]::Append`)
  const rows = String(ps[0]!.stdin ?? '').split('\n').filter(Boolean).map(l => JSON.parse(l))
  expect(rows[0]).toMatchObject({ sid: 'sess-1', door: 'prompt', role: 'user', text: "hello there, it's me" })
})

test('on Windows the zone marker is written through $.fs, once', async ($, on) => {
  const r = winRig(on, { config: { timezone: 'Pacific/Auckland' }, now: Date.UTC(2026, 0, 5, 12, 0, 0) })
  await start($)
  await $.session.append(row('one'))
  await r.clock.advance(31_000); await settle()
  await $.session.append(row('two'))
  await r.clock.advance(31_000); await settle()
  const markers = r.writes.filter(w => w.path === norm(`${ROOT}/.chronicle.json`))
  expect(markers.length).toBe(1)
  expect(JSON.parse(markers[0]!.text)).toMatchObject({ timezone: 'Pacific/Auckland' })
})

test('on Windows recall lists and searches the mirror through $.fs, no rg', async ($, on) => {
  const line = (t: number, text: string, u: string) => JSON.stringify({ t, sid: 'sess-0', uuid: u, door: 'prompt', origin: { kind: 'composer' }, role: 'user', text }) + '\n'
  const r = winRig(on, {
    config: { timezone: 'UTC' },
    files: {
      [`${ROOT}/.chronicle.json`]: JSON.stringify({ timezone: 'UTC', format: 1 }),
      [`${ROOT}/2026-01-04/sess-0.jsonl`]: line(Date.UTC(2026, 0, 4, 9), 'the hibiscus bloomed red', 'aaaaaaaa-0000-4000-8000-000000000001') + line(Date.UTC(2026, 0, 4, 10), 'tea on the balcony', 'aaaaaaaa-0000-4000-8000-000000000002'),
    },
  })
  await start($)
  const textOf = (o: any): string => String(o?.text ?? o?.result ?? '')
  const out = textOf(await $.tool.call({ tool: 'mcp__chronicle__recall', query: 'Hibiscus' }))
  expect(r.runs.map(x => x.argv.join(' ')).filter(a => /^(rg|cat|sh)\b/.test(a))).toEqual([])
  expect(out).toContain('hibiscus bloomed red')
  expect(out).not.toContain('balcony')
  const around = textOf(await $.tool.call({ tool: 'mcp__chronicle__recall', around: 'aaaaaaaa-0000-4000-8000-000000000001' }))
  expect(around).toContain('balcony')
})
