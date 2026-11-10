import { test, expect } from 'claude-code/testing'
import { normalizeConfig, validZone, DEFAULT_STEER, DEFAULT_MEMORY_VOICE, recallSpec } from '../hooks/register'
import { rig, settle } from './harness'

const HOME = '/home/tester'

// What must never appear in a default text. No private name is listed here (this repository is public): scripts/lint.sh
// checks the whole tree against the terms file named by PRIVATE_STRIP_TERMS_FILE. These are the shapes that never
// belong in a default text whoever wrote it: a home directory, an email address, a URL with a password in it.
const NOT_IN_DEFAULTS: RegExp[] = [/\/home\/[a-z]/i, /\/Users\/[a-z]/i, /[\w.+-]+@[\w-]+\.[a-z]{2,}/i, /:\/\/[^\s/]*:[^\s/]*@/]

// A copy of chronicle.example.json. scripts/lint.sh fails when the two differ.
// EXAMPLE-BEGIN
const example = JSON.parse(`
{
  "archiveRoot": "~/.chronicle/archive",
  "timezone": "",
  "names": {
    "user": "You",
    "agent": "Assistant"
  },
  "mirror": {
    "enabled": true
  },
  "compaction": {
    "tailTokens": 60000,
    "maxTailTokens": 120000,
    "minOlderMessages": 6,
    "voice": "",
    "steer": ""
  },
  "memories": {
    "enabled": false,
    "chunkMinTokens": 15000,
    "chunkMaxTokens": 25000,
    "voice": "",
    "assemble": "manual",
    "gapRawMaxTokens": 12000
  },
  "squish": {
    "enabled": true
  },
  "contextLine": {
    "enabled": true,
    "warnAt": 75,
    "urgentAt": 90
  },
  "tools": {
    "rgPath": "rg"
  },
  "archive": {
    "command": [],
    "timeoutMs": 30000
  }
}
`)
// EXAMPLE-END

test('no file at all: every default, memories off, no archive', () => {
  const c = normalizeConfig(undefined, HOME)
  expect(c.archiveRoot).toBe('/home/tester/.chronicle/archive')
  expect(c.memoriesEnabled).toBe(false)
  expect(c.mirrorEnabled).toBe(true)
  expect(c.squishEnabled).toBe(true)
  expect(c.contextLine).toEqual({ enabled: true, warnAt: 75, urgentAt: 90 })
  expect(c.archiveCommand).toEqual([])
  expect(c.rgPath).toBe('rg')
  expect(c.names).toEqual({ user: 'You', agent: 'Assistant' })
  expect(c.assembleWithMemories).toBe('manual')
  expect(c.tailTokens).toBe(60_000)
  expect(c.maxTailTokens).toBe(120_000)
  expect(c.ignored).toEqual([])
})

test('the shipped example file means exactly the defaults', () => {
  const fromExample = normalizeConfig(example, HOME)
  const defaults = normalizeConfig({}, HOME)
  expect(fromExample).toEqual(defaults)
})

test('wrong types fall back to the default, and unknown keys are listed', () => {
  const c = normalizeConfig({ tailTokens: 'lots', mirror: { enabled: 'yes' }, memories: { enabled: 1 }, contextLine: { warnAt: 500 }, nonsense: 1, archive: { command: 'resonant-archive' } }, HOME)
  expect(c.tailTokens).toBe(60_000)
  expect(c.mirrorEnabled).toBe(true)
  expect(c.memoriesEnabled).toBe(false)
  expect(c.contextLine.warnAt).toBe(75)
  expect(c.archiveCommand).toEqual([])
  expect(c.ignored).toEqual(['nonsense'])
  expect(normalizeConfig('a string', HOME).tailTokens).toBe(60_000)
  expect(normalizeConfig([1, 2], HOME).tailTokens).toBe(60_000)
})

test('the older flat keys are still read, and the nested ones win', () => {
  const old = normalizeConfig({ archiveRoot: '/data/a', tailTokens: 1000, maxTailTokens: 2000, minOlderMessages: 3, steer: 'My steer.', chunkMinTokens: 500, chunkMaxTokens: 900, memoryVoice: 'Voice text.', assembleWithMemories: 'auto', gapRawMaxTokens: 77 }, HOME)
  expect(old).toMatchObject({ archiveRoot: '/data/a', tailTokens: 1000, maxTailTokens: 2000, minOlderMessages: 3, steer: 'My steer.', chunkMinTokens: 500, chunkMaxTokens: 900, memoryPrompt: 'Voice text.', assembleWithMemories: 'auto', gapRawMaxTokens: 77 })
  const both = normalizeConfig({ tailTokens: 1000, compaction: { tailTokens: 2000 } }, HOME)
  expect(both.tailTokens).toBe(2000)
})

test('an older file that tuned memories keeps them on; an explicit switch wins', () => {
  expect(normalizeConfig({ chunkMinTokens: 1000 }, HOME).memoriesEnabled).toBe(true)
  expect(normalizeConfig({ assembleWithMemories: 'manual' }, HOME).memoriesEnabled).toBe(true)
  expect(normalizeConfig({ assembleWithMemories: 'manual', memories: { enabled: false } }, HOME).memoriesEnabled).toBe(false)
  expect(normalizeConfig({ memories: { enabled: true } }, HOME).memoriesEnabled).toBe(true)
})

test('archiveRoot: ~ is expanded, a trailing slash dropped, and no HOME means no root', () => {
  expect(normalizeConfig({ archiveRoot: '~/x/y/' }, HOME).archiveRoot).toBe('/home/tester/x/y')
  expect(normalizeConfig({ archiveRoot: '~/x' }, undefined).archiveRoot).toBe('')
  expect(normalizeConfig({ archiveRoot: '/abs/path' }, undefined).archiveRoot).toBe('/abs/path')
})

test('archiveRoot on Windows: ~\\ is expanded and a trailing backslash dropped', () => {
  expect(normalizeConfig({ archiveRoot: '~\\mirror\\x\\' }, 'C:\\Users\\t\\', true).archiveRoot).toBe('C:\\Users\\t\\mirror\\x')
  expect(normalizeConfig({ archiveRoot: 'D:\\chronicle\\' }, 'C:\\Users\\t', true).archiveRoot).toBe('D:\\chronicle')
})

test('archiveRoot off Windows: a backslash is an ordinary character, nothing changes', () => {
  expect(normalizeConfig({ archiveRoot: '~\\mirror\\' }, '/home/tester').archiveRoot).toBe('~\\mirror\\')
  expect(normalizeConfig({ archiveRoot: '/srv/a\\' }, '/home/tester').archiveRoot).toBe('/srv/a\\')
  expect(normalizeConfig({ archiveRoot: '~/x/y/' }, '/home/tester\\').archiveRoot).toBe('/home/tester\\/x/y')
})

test('timezone: a valid name is kept, an invalid one ignored', () => {
  expect(validZone('Europe/Rome')).toBe(true)
  expect(validZone('Mars/Olympus')).toBe(false)
  expect(normalizeConfig({ timezone: 'Europe/Rome' }, HOME).timezone).toBe('Europe/Rome')
  expect(normalizeConfig({ timezone: 'Mars/Olympus' }, HOME).timezone).toBe('')
})

test('chunk sizes: the maximum is never below the minimum; the context line thresholds stay ordered', () => {
  const c = normalizeConfig({ memories: { chunkMinTokens: 30000, chunkMaxTokens: 10000 }, contextLine: { warnAt: 80, urgentAt: 60 } }, HOME)
  expect(c.chunkMaxTokens).toBe(30000)
  expect(c.contextLine.urgentAt).toBe(80)
})

test('legacyToolNames: only well-formed tool names are kept', () => {
  expect(normalizeConfig({ legacyToolNames: ['mcp__old-name__recall', 'rm -rf', 5] }, HOME).legacyToolNames).toEqual(['mcp__old-name__recall'])
})

test('the default texts are neutral: first person, the person I\'m working with, no names', () => {
  for (const text of [DEFAULT_STEER, DEFAULT_MEMORY_VOICE, JSON.stringify(recallSpec(true)), JSON.stringify(recallSpec(false))]) {
    for (const shape of NOT_IN_DEFAULTS) expect(text).not.toMatch(shape)
  }
  expect(DEFAULT_STEER).toContain("the person I'm working with")
  expect(DEFAULT_STEER).toContain('every exact identifier verbatim')
  expect(DEFAULT_STEER).toContain('Never copy credentials')
})

test('a broken chronicle.json does not stop the mod: defaults run and the mirror still writes', async ($, on) => {
  const r = rig(on, { config: '{ not json' })
  await $.session.start({ source: 'startup', cwd: '/work' } as any)
  await $.session.append({ message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'hi' }] }, door: 'prompt', origin: { kind: 'composer' }, uuid: 'f0000000-0000-4000-8000-000000000001' } as any)
  await r.clock.advance(31_000); await settle()
  expect(r.runs.some(x => String(x.argv[5] ?? '').startsWith('/home/tester/.chronicle/archive/'))).toBe(true)
})

test('names.user and names.agent label the rows recall shows', async ($, on) => {
  const line = JSON.stringify({ t: 1, sid: 's', uuid: 'aaaaaaaa-0000-4000-8000-000000000001', door: 'prompt', origin: 'composer', role: 'user', text: 'findme' })
  const line2 = JSON.stringify({ t: 2, sid: 's', uuid: 'aaaaaaaa-0000-4000-8000-000000000002', door: 'response', role: 'assistant', text: 'findme too' })
  rig(on, { config: { names: { user: 'Sam', agent: 'Aria' } }, proc: argv => (argv[0] === 'rg' ? (argv.includes('--files') ? { exitCode: 0, stdout: '/home/tester/.chronicle/archive/2026-01-05/s.jsonl\n' } : { exitCode: 0, stdout: `${line}\n${line2}\n` }) : undefined) })
  await $.session.start({ source: 'startup', cwd: '/work' } as any)
  const out: any = await ($ as any).tool.call({ tool: 'mcp__chronicle__recall', query: 'findme' })
  const text = String(out.text ?? out.result)
  expect(text).toContain('Sam')
  expect(text).toContain('Aria')
})
