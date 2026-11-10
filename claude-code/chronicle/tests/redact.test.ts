import { test, expect } from 'claude-code/testing'
import { redact, secretBearing, blockText, trimQuotes, queuedPrompt, b64utf16le } from '../hooks/register'

// Every key below is obviously fake and built from repeated characters.
const A20 = 'A'.repeat(20)

// One secret that must go, and one near miss that must stay, per pattern.
const TABLE: { name: string; secret: string; gone: string; miss: string }[] = [
  { name: 'private key block', secret: '-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----', gone: 'MIIfake', miss: '-----BEGIN CERTIFICATE-----\nMIIpublic\n-----END CERTIFICATE-----' },
  { name: 'private key with no end', secret: '-----BEGIN PRIVATE KEY-----\nMIIcut', gone: 'MIIcut', miss: 'a private key is a file' },
  { name: 'sk- key', secret: `sk-ant-${A20}`, gone: A20, miss: 'sk-short' },
  { name: 'github token', secret: `ghp_${'a1'.repeat(18)}`, gone: 'a1a1a1', miss: 'ghp_short' },
  { name: 'github fine-grained token', secret: `github_pat_${'B2'.repeat(25)}`, gone: 'B2B2B2', miss: 'github_pat_short' },
  { name: 'aws key', secret: 'AKIA' + 'Q'.repeat(16), gone: 'QQQQQQQQ', miss: 'AKIA123' },
  { name: 'google key', secret: 'AIza' + 'z'.repeat(35), gone: 'zzzzzzzz', miss: 'AIza-too-short' },
  { name: 'slack token', secret: 'xox' + 'b-1234567890-abcdef', gone: '1234567890', miss: 'xox-nothing' },
  { name: 'jwt', secret: 'eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJlMTIzNDU2', gone: 'c2lnbmF0dXJl', miss: 'eyJ.short.x' },
  { name: 'bearer token', secret: 'Authorization: Bearer abcdefghijklmnopqrstuvwx', gone: 'abcdefghijkl', miss: 'bearer of bad news' },
  { name: 'postgres password', secret: 'postgres://user:hunter2hunter2@db.example.test/app', gone: 'hunter2hunter2', miss: 'postgres://db.example.test/app' },
  { name: 'secret path', secret: 'https://x.example.test/mcp/abcdefghijklmnop1234', gone: 'abcdefghijklmnop1234', miss: 'https://x.example.test/mcp/short' },
  { name: 'secret query parameter', secret: 'https://x.example.test/a?token=abc123def456', gone: 'abc123def456', miss: 'https://x.example.test/a?page=2' },
  { name: 'env-style assignment', secret: 'export MY_API_KEY=supersecretvalue', gone: 'supersecretvalue', miss: 'export MY_NAME=someone' },
  { name: 'key: value', secret: 'password: "correct-horse-battery"', gone: 'correct-horse-battery', miss: 'password: abc' },
]

for (const t of TABLE) {
  test(`redact: ${t.name} is replaced`, () => {
    const out = redact(`before ${t.secret} after`)
    expect(out).toContain('before')
    expect(out).not.toContain(t.gone)
    expect(out).toContain('[redacted')
  })
  test(`redact: near miss for ${t.name} is left alone`, () => {
    expect(redact(t.miss)).toBe(t.miss)
  })
}

test('redact: runs over the whole string, so a key a later cut would split is already gone', () => {
  const text = `${'x'.repeat(30)} sk-${'k'.repeat(40)}`
  expect(redact(text).slice(0, 60)).not.toContain('kkkk')
})

test('redact: very long input is capped, and a non-string is tolerated', () => {
  expect(redact('a'.repeat(600_000)).length).toBe(512 * 1024)
  expect(redact(undefined as unknown as string)).toBe('')
})

test('blockText: nested tool input is redacted too', () => {
  const r = blockText([{ type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'curl -H "x-api-key: abcdefghijklmnop" https://x.example.test' } }])
  expect(r.tools[0]).not.toContain('abcdefghijklmnop')
})

test('secretBearing: files that carry secrets', () => {
  for (const path of ['/app/.env', '/app/.env.local', '/home/u/.ssh/id_rsa', '/x/key.pem', '/x/private/notes.md', '/x/credentials.json', '/x/.mcp.json', '/x/.dev.vars']) {
    expect(secretBearing('Read', { file_path: path })).toBe(true)
  }
  for (const path of ['/app/src/environment.ts', '/app/README.md', '/app/keys.md']) {
    expect(secretBearing('Read', { file_path: path })).toBe(false)
  }
})

test('secretBearing: commands that print the environment', () => {
  for (const c of ['env', 'printenv', 'ps aux', 'pm2 env 0', 'set', 'export', 'echo hi; env', 'cat .env', 'sudo env']) {
    expect(secretBearing('Bash', { command: c })).toBe(true)
  }
  for (const c of ['set -e', 'ls -la', 'echo environment', 'git status', 'envsubst < a']) {
    expect(secretBearing('Bash', { command: c })).toBe(false)
  }
})

test('blockText: the result of a secret-bearing call is withheld whole, others are kept', () => {
  const call = blockText([{ type: 'tool_use', id: 'u1', name: 'Bash', input: { command: 'printenv' } }, { type: 'tool_use', id: 'u2', name: 'Bash', input: { command: 'ls' } }])
  expect(call.tools.length).toBe(2)
  const res = blockText([
    { type: 'tool_result', tool_use_id: 'u1', content: 'PATH=/usr/bin\nTHING=value' },
    { type: 'tool_result', tool_use_id: 'u2', content: 'file.txt' },
  ])
  expect(res.results).toEqual(['[withheld: secret-bearing source]', 'file.txt'])
})

test('blockText: recall output is not mirrored, under the current or a legacy tool name', () => {
  blockText([{ type: 'tool_use', id: 'r1', name: 'mcp__chronicle__recall', input: { query: 'x' } }])
  const res = blockText([{ type: 'tool_result', tool_use_id: 'r1', content: 'recall x (all sessions): 1 hit' }])
  expect(res.results).toEqual(['[recall output: not mirrored, it quotes the archive]'])
})

// ---- coverage shared with tests/redaction_cases.py in the Python package: the two lists stay equivalent ----
// COVERAGE-BEGIN
const SHAPES: [string, string, string][] = [
  ["stripe sk_live", "key is sk_live_" + "a1B2".repeat(6), "a1B2a1B2"],
  ["stripe rk_live", "rk_live_" + "Q".repeat(24), "QQQQQQQQ"],
  ["stripe sk_test", "sk_test_" + "z9".repeat(12), "z9z9z9"],
  ["npm token", "//registry.npmjs.org/:_authToken=npm_" + "a1".repeat(18), "a1a1a1"],
  ["huggingface token", "HF hf_" + "aB".repeat(17), "aBaBaB"],
  ["gitlab token", "glpat-" + "x".repeat(20), "xxxxxxxx"],
  ["sendgrid key", "SG." + "a".repeat(22) + "." + "b".repeat(43), "bbbbbbbb"],
  ["aws access key", "AKIA" + "Q".repeat(16), "QQQQQQQQ"],
  ["aws session key", "ASIA" + "A".repeat(16), "AAAAAAAA"],
  ["aws secret assignment", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "bPxRfiCY"],
  ["aws secret upper", "AWS_SECRET_ACCESS_KEY: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "bPxRfiCY"],
  ["aws secret spaced", "secret key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "bPxRfiCY"],
  ["azure account key", "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==;EndpointSuffix=core", "AAAAAAAA"],
  ["authorization token", "curl -H \"Authorization: Token abcdef0123456789abcdef\" x", "abcdef0123"],
  ["authorization basic", "Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpw"],
  ["authorization bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwx", "abcdefghijkl"],
  ["authorization bare", "authorization: 8f14e45fceea167a5a36dedd4bea2543", "8f14e45f"],
  ["curl -u", "curl -u admin:SuperSecretPass123 https://x.example.test", "SuperSecretPass123"],
  ["curl --user", "curl --user admin:SuperSecretPass123 https://x.example.test", "SuperSecretPass123"],
  ["sshpass", "sshpass -p 'MyPassw0rdXYZ' ssh a@b.example.test", "MyPassw0rdXYZ"],
  ["sshpass bare", "sshpass -p hunter2hunter ssh a@b.example.test", "hunter2hunter"],
  ["mysql -p", "mysql -u root -pSecretPW123 db", "SecretPW123"],
  ["mysqldump -p", "mysqldump -h h -u u -pSecretPW123 db > x.sql", "SecretPW123"],
  ["redis-cli -a", "redis-cli -h h -a SecretPW123 ping", "SecretPW123"],
  ["--password flag", "tool --password hunter2hunter login", "hunter2hunter"],
  ["--token= flag", "tool --token=abcd1234efgh login", "abcd1234efgh"],
  ["https userinfo", "https://user:p4ssw0rd@example.test/x", "p4ssw0rd"],
  ["ftp userinfo", "ftp://user:p4ssw0rd@example.test/x", "p4ssw0rd"],
  ["ssh userinfo", "ssh://git:p4ssw0rd@example.test/x", "p4ssw0rd"],
  ["userinfo with @", "postgresql://u:p@ss@word@db.example.test:5432/app", "p@ss"],
  ["postgres", "postgres://user:hunter2hunter2@db.example.test/app", "hunter2hunter2"],
  ["mongodb+srv", "mongodb+srv://u:pw1234@c.example.test", "pw1234"],
  ["slack webhook", "https://hooks.slack.com/services/T00000000/B00000000/" + "X".repeat(24), "XXXXXXXX"],
  ["discord webhook", "https://discord.com/api/webhooks/123456789012345678/" + "a".repeat(60), "aaaaaaaa"],
  ["discord webhook app", "https://discordapp.com/api/v10/webhooks/123456789012345678/" + "Ab1".repeat(20), "Ab1Ab1Ab1"],
  ["discord bot token", "MTIzNDU2Nzg5MDEyMzQ1Njc4" + ".GabcDE." + "a".repeat(38), "aaaaaaaa"],
  ["twilio sid", "sid ACa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1", "a1a1a1"],
  ["twilio auth", "TWILIO_AUTH_TOKEN=0123456789abcdef0123456789abcdef", "0123456789abcdef"],
  ["pgp key block", "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQdGBF\n-----END PGP PRIVATE KEY BLOCK-----", "lQdGBF"],
  ["pem key", "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----", "MIIfake"],
  ["openssh key", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz\n-----END OPENSSH PRIVATE KEY-----", "b3BlbnNz"],
  ["key with no end", "-----BEGIN PRIVATE KEY-----\nMIIcut and more after", "MIIcut"],
  ["key in json", "{\"k\":\"-----BEGIN RSA PRIVATE KEY-----\\nMIIEabc\\n-----END RSA PRIVATE KEY-----\"}", "MIIEabc"],
  ["generic password colon", "password: correcthorsebattery", "correcthorsebattery"],
  ["generic password equals", "password=abc12345", "abc12345"],
  ["generic secret json", "{\"client_secret\": \"s3cr3tv4lue99\"}", "s3cr3tv4lue99"],
  ["generic token upper", "MY_API_TOKEN = 'tok3nvalue99'", "tok3nvalue99"],
  ["generic api key", "apiKey: 'abcd1234wxyz'", "abcd1234wxyz"],
  ["generic access key", "access_key=ABCD1234wxyz", "ABCD1234wxyz"],
  ["generic passwd inline", "set DB_PASSWD=hunter2hunter and go", "hunter2hunter"],
  ["env line", "export MY_API_KEY=supersecretvalue", "supersecretvalue"],
  ["prose password is", "my password is hunter2hunter2", "hunter2hunter2"],
  ["prose password was for", "the password for admin was Tr0ub4dor", "Tr0ub4dor"],
  ["prose quoted", "password is 'correct-horse'", "correct-horse"],
  ["high entropy", "here: " + "aZ3".repeat(14), "aZ3aZ3aZ3"],
  ["db-pass flag", "psql --db-pass=hunter2xyz -h db", "hunter2xyz"],
  ["admin-password flag", "tool --admin-password s3cretvalue", "s3cretvalue"],
  ["pgpass line", "x\ndb.example.test:5432:appdb:appuser:Zk9mQ2\ny", "Zk9mQ2"],
  ["short password", "password=Zq9x", "Zq9x"],
  ["short password colon", "passwd: ab12cd", "ab12cd"],
  ["short pwd json", '{"pwd": "x1y2z3"}', "x1y2z3"],
  ["high entropy base64", "blob Zm9vYmFyQmF6MTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1O end", "Zm9vYmFy"],
  ["sk key", "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "aaaaaaaa"],
  ["sk underscore key", "sk-proj-aaaaaaaaaa_bbbbbbbbbb", "aaaaaaaa"],
  ["github token", "ghp_" + "a1".repeat(18), "a1a1a1"],
  ["jwt", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJlMTIzNDU2", "c2lnbmF0dXJl"],
  ["secret path", "https://x.example.test/mcp/abcdefghijklmnop1234", "abcdefghijklmnop1234"],
  ["secret query", "https://x.example.test/a?token=abc123def456", "abc123def456"],
  ["win set", "set OPENAI_API_KEY=Zk9mQ2xLp7", "Zk9mQ2xLp7"],
  ["win set short value", "set MYTOKEN=z9y8", "z9y8"],
  ["win set bare name", "set PASSWORD=hunter2", "hunter2"],
  ["win set quoted", "set \"DB_PASSWORD=hunter2\" && echo", "hunter2"],
  ["win setx", "setx API_TOKEN \"val-ue-123\" /M", "val-ue-123"],
  ["win setx bare", "setx GITHUB_TOKEN abcd1234", "abcd1234"],
  ["powershell env assignment", "$env:GITHUB_TOKEN = \"ghx12\"", "ghx12"],
  ["powershell env single quotes", "$env:DB_PASSWORD='p1q2'", "p1q2"],
  ["powershell env braces", "${env:MY_SECRET} = 's3cr3t'", "s3cr3t"],
  ["powershell SetEnvironmentVariable", "[Environment]::SetEnvironmentVariable(\"API_KEY\",\"v4lu3x\",\"User\")", "v4lu3x"],
  ["powershell -Password", "Connect.ps1 -Password hunter2x -Verbose", "hunter2x"],
  ["powershell -Token colon", "tool.exe -Token:abcd1234", "abcd1234"],
  ["powershell -ApiKey quoted", "Invoke-X -ApiKey \"k3y v4lue\"", "v4lue"],
  ["powershell -ClientSecret", "Get-Token -ClientSecret=abcd1234", "abcd1234"],
  ["process command line", "\"C:\\Program Files\\App\\app.exe\" -Password:pw12345 -port 80", "pw12345"],
  ["cmdkey /pass", "cmdkey /add:host /user:me /pass:S3cretPw", "S3cretPw"],
  ["schtasks /RP", "schtasks /Create /RU me /RP Passw0rdX /TN t", "Passw0rdX"],
  ["net use", "net use \\\\srv\\share Pa55w0rd /user:bob", "Pa55w0rd"],
  ["net use with drive", "net use Z: \\\\srv\\share Pa55w0rd /user:bob", "Pa55w0rd"],
  ["net user", "net user bob P4ssw0rd /add", "P4ssw0rd"],
  ["sqlcmd -P", "sqlcmd -S srv -U sa -P S3cretPw -Q x", "S3cretPw"],
  ["psexec -p", "psexec \\\\host -u a -p pw123abc cmd", "pw123abc"],
  ["plink -pw", "plink -ssh me@host -pw secretpw1", "secretpw1"],
  ["ConvertTo-SecureString", "ConvertTo-SecureString \"plainpw1\" -AsPlainText -Force", "plainpw1"],
  ["gci env table", "x\nOPENAI_API_KEY                 abc123def\ny", "abc123def"],
  ["gci env table bare name", "x\nTOKEN      t0k3nv\ny", "t0k3nv"],
  ["gci env list", "x\nName  : MY_SECRET\nValue : abc123\ny", "abc123"],
  ["wmic environment", "x\nName=API_KEY\r\nVariableValue=zz99\ny", "zz99"],
  ["reg query value", "x\n    OPENAI_API_KEY    REG_SZ    rv4lu3\ny", "rv4lu3"],
  ["reg query binary", "x\n    Password    REG_BINARY    4a4b4c\ny", "4a4b4c"],
  ["reg add", "reg add HKCU\\Software\\X /v ApiToken /t REG_SZ /d tok3nvalue /f", "tok3nvalue"],
]
const SURVIVORS: string[] = [
  "Just a normal sentence about the token economy and how a password reset email works.",
  "commit 3f786850e387550fdab836ed7e6dc881de23001b merged into main",
  "short sha 3f78685 and a UUID 550e8400-e29b-41d4-a716-446655440000 are fine",
  "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 of the empty file",
  "UPPER HEX E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855 too",
  "see /home/user/projects/resonant-archive/resonant_archive/redact.py for the patterns",
  "C:\\Users\\someone\\Documents\\notes\\2026-10-16.md",
  "https://github.com/example/repo/pull/123 and https://example.test/docs/mcp/overview",
  "https://example.test:8443/a/b?page=2&sort=name",
  "git clone git@github.com:example/repo.git && git push -u origin HEAD:main",
  "ssh://git@example.test/repo.git",
  "postgresql://db.example.test/app",
  "mysql -u root -P 3306 -p mydb",
  "docker run -it ubuntu bash",
  "interface Props { token: string; password: string; secret: Buffer }",
  "password=${DB_PASSWORD} and token: <your-token-here>",
  "password: abc",
  "password = None and password: string",
  "psql --no-password mydatabase",
  "def login(password: str, user: str): pass",
  "a 39 character identifier " + "AbCd1234".repeat(4) + "AbCd123",
  "the secret sauce recipe and a token of appreciation",
  "the token is expired and the password is required",
  "AKIA123 xox-nothing sk-short ghp_short SG.too.short hf_short npm_short glpat-short",
  "DefaultEndpointsProtocol=https;AccountName=storage1;EndpointSuffix=core.windows.net",
  "A very ordinary Paragraph with Capitals, numbers 12345 and punctuation; nothing else to see.",
  "The identifier ThisIsACamelCaseIdentifier is only 28 chars",
  "-----BEGIN CERTIFICATE-----\nMIIpublic\n-----END CERTIFICATE-----",
  "a private key is a file you keep safe",
  "Authorization header is required",
  "version v2.0.0, release 2026-10-16, snowflake 1234567890123456789",
  "set NODE_ENV=production && npm test",
  "setx PATH \"C:\\tools;%PATH%\" /M",
  "$env:PATH = \"C:\\x;\" + $env:PATH",
  "$env:NODE_ENV = 'test'",
  "msbuild app.sln /p:Configuration=Release /p:Platform=x64",
  "Connect.ps1 -Password $pw -Verbose",
  "Connect.ps1 -Password (Read-Host -AsSecureString) -Verbose",
  "Get-Thing -Passphrase -Force",
  "net user bob /domain",
  "net use X: \\\\srv\\share *",
  "sqlcmd -S srv -Q \"select 1\"",
  "x\nPATH                           C:\\Windows\\System32\ny",
  "x\nName  : PATH\nValue : C:\\bin\ny",
  "x\n    Path    REG_EXPAND_SZ    C:\\bin\ny",
  "reg add HKCU\\Software\\Acme /v Theme /d dark",
  "cmdkey /add:host /user:me",
  "Set-Location C:\\Users\\someone\\key-notes\\passwords-guide",
]
// COVERAGE-END

for (const [name, text, gone] of SHAPES) {
  test(`redact shape: ${name}`, () => {
    const out = redact(`before ${text} after`)
    expect(out).not.toContain(gone)
    expect(out).toContain('[redacted')
    expect(redact(out)).toBe(out)
  })
}

test('redact: ordinary text, commit ids, UUIDs, paths and plain URLs survive', () => {
  for (const t of SURVIVORS) expect(redact(t)).toBe(t)
})

const SLOW_INPUTS: Record<string, string> = {
  'newlines': '\n',
  'repeated --user=x': '--user=x',
  'unfinished cli-credentials': '-u ' + 'a'.repeat(40),
  'blank env lines': ' \t\n',
  'repeated unfinished env key': 'export A_KEY=\n',
  // Windows rules: a prefix the rule can start on, over and over, and never finish
  'repeated set': 'set A_KEY=', 'repeated setx': 'setx A_KEY "', 'repeated $env': '$env:A_KEY =', 'repeated -Password': '-Password ',
  'repeated net use': 'net use ', 'repeated net user': 'net user a ', 'repeated sqlcmd': 'sqlcmd -x ', 'repeated psexec': 'psexec -x ',
  'repeated ConvertTo': 'ConvertTo-SecureString "', 'repeated env table': 'A_KEY  \n', 'repeated env list': 'Name : A_KEY\n',
  'repeated reg row': '    A_KEY  REG_SZ \n', 'repeated reg add': 'reg add /v a_key ', 'long upper run': 'A'.repeat(1000) + '  ',
  'spaces': ' ',
}
for (const [name, frag] of Object.entries(SLOW_INPUTS)) {
  test(`redact: 500 KB of ${name} is scanned in under a second`, () => {
    const text = frag.repeat(Math.ceil(500_000 / frag.length)).slice(0, 500_000)
    const t0 = Date.now()
    redact(text)
    expect(Date.now() - t0).toBeLessThan(1000)
  })
}

test('redact: 20k BEGIN lines are scanned in linear time', () => {
  const t0 = Date.now()
  const out = redact('-----BEGIN PRIVATE KEY-----\n'.repeat(20_000))
  expect(Date.now() - t0).toBeLessThan(1000)
  expect(out).toBe('[redacted:private-key]')
  const pairs = redact('-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\nkeep me\n'.repeat(5000))
  expect(pairs.split('[redacted:private-key]').length - 1).toBe(5000)
  expect(pairs).not.toContain('abc')
})

test('secretBearing: a command substitution that prints the environment is caught', () => {
  expect(secretBearing('Bash', { command: 'echo $(env)' })).toBe(true)
  expect(secretBearing('Bash', { command: 'x=`printenv`' })).toBe(true)
  expect(secretBearing('Bash', { command: 'echo $(date)' })).toBe(false)
})

// SECRET-CASES-BEGIN (generated from tests/secret_cases.py; test_secret_cases_match_the_mod keeps the two equal)
const SECRET_CALLS: [string, unknown][] = [
  ["Read", {"file_path": "/app/.env"}],
  ["Read", {"file_path": "/app/.env.local"}],
  ["Read", {"file_path": "/app/.env.production.local"}],
  ["Read", {"file_path": "/app/.env.bak"}],
  ["Read", {"file_path": "/x/.envrc"}],
  ["Read", {"file_path": "/x/prod.env"}],
  ["Read", {"file_path": "/home/user/.npmrc"}],
  ["Read", {"file_path": "/home/user/.pypirc"}],
  ["Read", {"file_path": "/home/user/.netrc"}],
  ["Read", {"file_path": "/home/user/.pgpass"}],
  ["Read", {"file_path": "/home/user/.git-credentials"}],
  ["Read", {"file_path": "/home/user/.docker/config.json"}],
  ["Read", {"file_path": "/home/user/.kube/config"}],
  ["Read", {"file_path": "/x/secrets.yml"}],
  ["Read", {"file_path": "/x/secrets.json"}],
  ["Read", {"file_path": "/x/secret.toml"}],
  ["Read", {"file_path": "/x/cert.p12"}],
  ["Read", {"file_path": "/x/cert.pfx"}],
  ["Read", {"file_path": "/home/user/.vault-token"}],
  ["Read", {"file_path": "/infra/terraform.tfstate"}],
  ["Read", {"file_path": "/infra/terraform.tfstate.backup"}],
  ["Read", {"file_path": "/proc/self/environ"}],
  ["Read", {"file_path": "/proc/1234/cmdline"}],
  ["Read", {"file_path": "/home/user/.ssh/id_rsa"}],
  ["Read", {"file_path": "/home/user/.ssh/id_ed25519"}],
  ["Read", {"file_path": "/home/user/.ssh/id_ecdsa"}],
  ["Read", {"file_path": "/home/user/.ssh/id_dsa"}],
  ["Read", {"file_path": "/home/user/.aws/credentials"}],
  ["Read", {"file_path": "/x/key.pem"}],
  ["Read", {"file_path": "/x/private/notes.md"}],
  ["Read", {"file_path": "/x/credentials.json"}],
  ["Read", {"file_path": "/x/.mcp.json"}],
  ["Read", {"file_path": "/x/.dev.vars"}],
  ["Read", {"file_path": "/gcp/my-service-account.json"}],
  ["Read", {"file_path": "/x/terraform.tfvars"}],
  ["Read", {"file_path": "/etc/shadow"}],
  ["Write", {"file_path": "/app/.env", "content": "API_KEY=zz"}],
  ["Edit", {"file_path": "/app/.env", "old_string": "A=1", "new_string": "A=2"}],
  ["Bash", {"command": "env"}],
  ["Bash", {"command": "printenv"}],
  ["Bash", {"command": "ps aux"}],
  ["Bash", {"command": "cd x && env | sort"}],
  ["Bash", {"command": "sudo -E printenv HOME"}],
  ["Bash", {"command": "sudo env"}],
  ["Bash", {"command": "sudo -u root env"}],
  ["Bash", {"command": "pm2 env 0"}],
  ["Bash", {"command": "export"}],
  ["Bash", {"command": "export -p"}],
  ["Bash", {"command": "declare -x"}],
  ["Bash", {"command": "set"}],
  ["Bash", {"command": "cat .env"}],
  ["Bash", {"command": "cat .env.production"}],
  ["Bash", {"command": "cat .env*"}],
  ["Bash", {"command": "cat '.env'"}],
  ["Bash", {"command": "cat ./.env.local"}],
  ["Bash", {"command": "cat *.env"}],
  ["Bash", {"command": "cat ~/.ssh/id_ed25519"}],
  ["Bash", {"command": "echo $(env)"}],
  ["Bash", {"command": "x=`printenv`"}],
  ["Bash", {"command": "/usr/bin/printenv"}],
  ["Bash", {"command": "/usr/bin/env"}],
  ["Bash", {"command": "exec env"}],
  ["Bash", {"command": "time env"}],
  ["Bash", {"command": "command env"}],
  ["Bash", {"command": "nohup env"}],
  ["Bash", {"command": "FOO=1 env"}],
  ["Bash", {"command": "FOO=1 BAR=2 printenv"}],
  ["Bash", {"command": "bash -c env"}],
  ["Bash", {"command": "sh -c 'printenv'"}],
  ["Bash", {"command": "bash -c \"cd x && env\""}],
  ["Bash", {"command": "sudo bash -c 'env'"}],
  ["Bash", {"command": "env -0"}],
  ["Bash", {"command": "docker compose config"}],
  ["Bash", {"command": "docker-compose config"}],
  ["Bash", {"command": "docker inspect web"}],
  ["Bash", {"command": "docker exec web env"}],
  ["Bash", {"command": "systemctl show -p Environment myservice"}],
  ["Bash", {"command": "systemctl cat myservice"}],
  ["Bash", {"command": "ssh-add -L"}],
  ["Bash", {"command": "python3 -c 'import os; print(os.environ)'"}],
  ["Bash", {"command": "node -e 'console.log(process.env)'"}],
  ["Bash", {"command": "cat /proc/self/environ"}],
  ["Bash", {"command": "xargs -0 < /proc/self/environ"}],
  ["Bash", {"command": "tr '\\0' '\\n' < /proc/1/environ"}],
  ["Bash", {"command": "cat /proc/1/cmdline"}],
  ["Bash", {"command": "cat ~/.npmrc"}],
  ["Bash", {"command": "cat ~/.git-credentials"}],
  ["Bash", {"command": "cat .envrc"}],
  ["Bash", {"command": "cat terraform.tfstate"}],
  ["Bash", {"command": "cat ~/.docker/config.json"}],
  ["Bash", {"command": "cat ~/.kube/config"}],
  ["Bash", {"command": "history"}],
  ["Bash", {"command": "kubectl get secret db -o yaml"}],
  ["Bash", {"command": "gh auth token"}],
  ["Bash", {"command": "aws configure get aws_secret_access_key"}],
  ["Bash", {"command": "grep -r password ."}],
  ["Bash", {"command": "rg 'API_KEY' src"}],
  ["Bash", {"command": "grep -rn token config/"}],
  ["Grep", {"pattern": "password", "path": "/srv"}],
  ["Grep", {"pattern": "API_KEY=\\w+"}],
  ["Grep", {"pattern": "x", "path": "/app/.env"}],
  ["Grep", {"pattern": "foo", "glob": "*.env"}],
  ["Grep", {"pattern": "BEGIN [A-Z ]*PRIVATE KEY"}],
  ["Glob", {"pattern": "**/.env*"}],
  ["Glob", {"pattern": "**/*.pem"}],
  ["Glob", {"pattern": "**/secrets.*"}],
  ["Glob", {"pattern": "**/*", "path": "/home/user/.ssh/id_rsa"}],
  ["Read", {"file_path": "C:\\proj\\.env"}],
  ["Read", {"file_path": "C:\\proj\\secrets.yml"}],
  ["Read", {"file_path": "C:\\Users\\me\\.ssh\\id_rsa"}],
  ["Read", {"file_path": "C:\\Users\\me\\.aws\\credentials"}],
  ["Read", {"file_path": "C:\\Users\\me\\.npmrc"}],
  ["Read", {"file_path": "C:\\Users\\me\\.git-credentials"}],
  ["Read", {"file_path": "C:\\Users\\me\\.docker\\config.json"}],
  ["Read", {"file_path": "C:\\Users\\me\\.kube\\config"}],
  ["Read", {"file_path": "D:\\x\\private\\notes.md"}],
  ["Read", {"file_path": "C:\\Users\\me\\NTUSER.DAT"}],
  ["Read", {"file_path": "C:\\Windows\\System32\\config\\SAM"}],
  ["Read", {"file_path": "C:\\Users\\me\\.azure\\accessTokens.json"}],
  ["Read", {"file_path": "C:\\Users\\me\\AppData\\Roaming\\GitHub CLI\\gh\\hosts.yml"}],
  ["Read", {"file_path": "C:\\Users\\me\\AppData\\Roaming\\Microsoft\\Credentials\\ABCDEF0123"}],
  ["Read", {"file_path": "C:\\Users\\me\\AppData\\Roaming\\Microsoft\\Windows\\PowerShell\\PSReadLine\\ConsoleHost_history.txt"}],
  ["PowerShell", {"command": "Get-ChildItem Env:"}],
  ["PowerShell", {"command": "gci env:"}],
  ["PowerShell", {"command": "GCI ENV: | Sort-Object Name"}],
  ["PowerShell", {"command": "dir env:"}],
  ["PowerShell", {"command": "ls Env:\\"}],
  ["PowerShell", {"command": "Get-Item -Path Env:*"}],
  ["PowerShell", {"command": "Get-Content env:OPENAI_API_KEY"}],
  ["PowerShell", {"command": "Write-Host $env:GITHUB_TOKEN"}],
  ["PowerShell", {"command": "echo ${env:DB_PASSWORD}"}],
  ["PowerShell", {"command": "[Environment]::GetEnvironmentVariable('PATH')"}],
  ["PowerShell", {"command": "[System.Environment]::GetEnvironmentVariables()"}],
  ["PowerShell", {"command": "[Environment]::GetEnvironmentVariables('User') | Format-List"}],
  ["PowerShell", {"command": "Get-CimInstance Win32_Process | Select-Object Name, CommandLine"}],
  ["PowerShell", {"command": "Get-WmiObject -Class Win32_Process -Filter \"name='node.exe'\""}],
  ["PowerShell", {"command": "Get-CimInstance -ClassName win32_service | select Name, PathName, StartName"}],
  ["PowerShell", {"command": "Get-Process | Select-Object Name, CommandLine"}],
  ["PowerShell", {"command": "gps node | select Id,CommandLine"}],
  ["PowerShell", {"command": "Get-ItemProperty -Path 'HKCU:\\Environment'"}],
  ["PowerShell", {"command": "Get-StoredCredential -Target git"}],
  ["PowerShell", {"command": "Get-Secret -Name token -AsPlainText"}],
  ["PowerShell", {"command": "$c.GetNetworkCredential().Password"}],
  ["PowerShell", {"command": "$s | ConvertFrom-SecureString"}],
  ["PowerShell", {"command": "Get-Content .\\.env.local"}],
  ["PowerShell", {"command": "gc C:\\Users\\me\\.ssh\\id_ed25519"}],
  ["PowerShell", {"command": "cat $HOME\\.aws\\credentials"}],
  ["PowerShell", {"command": "Get-ChildItem -Recurse | Select-String password"}],
  ["PowerShell", {"command": "sls -Path . -Pattern 'api_key' -Recurse"}],
  ["PowerShell", {"command": "powershell -NoProfile -Command \"gci env:\""}],
  ["Bash", {"command": "wmic process get commandline"}],
  ["Bash", {"command": "wmic process where name=\"node.exe\" get commandline,processid"}],
  ["Bash", {"command": "C:\\Windows\\System32\\wbem\\WMIC.exe process list full"}],
  ["Bash", {"command": "wmic environment get name,variablevalue"}],
  ["Bash", {"command": "cmdkey /list"}],
  ["Bash", {"command": "cmdkey.exe /list:git:https://github.com"}],
  ["Bash", {"command": "vaultcmd /listcreds:\"Windows Credentials\""}],
  ["Bash", {"command": "reg query HKCU\\Environment"}],
  ["Bash", {"command": "reg query \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment\""}],
  ["Bash", {"command": "reg query HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon"}],
  ["Bash", {"command": "reg query HKCU\\Software\\Acme /v ApiToken"}],
  ["Bash", {"command": "reg.exe query HKCU\\Software /s /f password"}],
  ["Bash", {"command": "cmd /c set"}],
  ["Bash", {"command": "cmd.exe /c \"set\""}],
  ["Bash", {"command": "cmd /c set API"}],
  ["Bash", {"command": "set AWS"}],
  ["Bash", {"command": "echo %GITHUB_TOKEN%"}],
  ["Bash", {"command": "echo %API_KEY%"}],
  ["Bash", {"command": "powershell.exe -c \"Get-ChildItem Env:\""}],
  ["Bash", {"command": "pwsh -NoProfile -Command 'Get-CimInstance Win32_Process'"}],
  ["Bash", {"command": "powershell -NoProfile -EncodedCommand RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -enc WwBFAG4AdgBpAHIAbwBuAG0AZQBuAHQAXQA6ADoARwBlAHQARQBuAHYAaQByAG8AbgBtAGUAbgB0AFYAYQByAGkAYQBiAGwAZQBzACgAKQA="}],
  ["Bash", {"command": "netsh wlan show profile name=\"Home\" key=clear"}],
  ["Bash", {"command": "type .env"}],
  ["Bash", {"command": "findstr /s /i password *.config"}],
  ["Bash", {"command": "C:\\Windows\\System32\\findstr.exe /s token *.json"}],
  ["Bash", {"command": "powershell -e RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -ec RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -en RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -enc RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -enco RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -encodedcomman RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -EncodedCommand RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -enc:RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -EncodedCommand:RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell /enc RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell /e:RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "POWERSHELL.EXE -ENC RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "pwsh -NoProfile -ec RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -NoProfile -ExecutionPolicy Bypass -en RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA"}],
  ["Bash", {"command": "powershell -enc \"RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA\""}],
  ["Bash", {"command": "powershell -ExecutionPolicy Bypass -Command \"printenv\""}],
  ["Bash", {"command": "pwsh -NoLogo -ExecutionPolicy Bypass -Command \"compgen -v\""}],
  ["Bash", {"command": "powershell -ExecutionPolicy Bypass -c printenv"}],
  ["Bash", {"command": "powershell -NoProfile -WindowStyle Hidden -com env"}],
  ["Bash", {"command": "powershell /command env"}],
  ["Bash", {"command": "powershell -Command:printenv"}],
  ["Bash", {"command": "powershell -NoLogo -InputFormat Text -OutputFormat Text -Command \"gci env:\""}],
  ["Bash", {"command": "powershell -enc !!!not-base64!!!"}],
  ["Bash", {"command": "powershell -enc QQ=="}],
  ["Bash", {"command": "powershell -enc"}],
  ["Bash", {"command": "powershell -EncodedCommand:"}],
  ["Bash", {"command": "powershell \"unterminated -Command Get-Date"}],
  ["Bash", {"command": "powershell -Command"}],
]
const ORDINARY_CALLS: [string, unknown][] = [
  ["Read", {"file_path": "/app/src/environment.ts"}],
  ["Read", {"file_path": "/app/README.md"}],
  ["Read", {"file_path": "/app/keys.md"}],
  ["Read", {"file_path": "/home/user/.ssh/id_rsa.pub"}],
  ["Read", {"file_path": "/app/src/secretary.ts"}],
  ["Read", {"file_path": "/app/idle.txt"}],
  ["Read", {"file_path": "/app/src/envelope.py"}],
  ["Read", {"file_path": "/app/docs/tokens-of-appreciation.md"}],
  ["Bash", {"command": "ls -la"}],
  ["Bash", {"command": "set -e"}],
  ["Bash", {"command": "/usr/bin/env python3 run.py"}],
  ["Bash", {"command": "git status"}],
  ["Bash", {"command": "envsubst < a"}],
  ["Bash", {"command": "echo environment"}],
  ["Bash", {"command": "grep -rn TODO src"}],
  ["Bash", {"command": "docker compose up -d"}],
  ["Bash", {"command": "systemctl status nginx"}],
  ["Bash", {"command": "pm2 list"}],
  ["Bash", {"command": "echo hello"}],
  ["Bash", {"command": "python3 manage.py test"}],
  ["Bash", {"command": "echo $(date)"}],
  ["Bash", {"command": "cd /srv && make build"}],
  ["Bash", {"command": "FOO=1 make test"}],
  ["Bash", {"command": "bash -c 'ls -la'"}],
  ["Bash", {"command": "sudo apt-get update"}],
  ["Bash", {"command": "time make"}],
  ["Bash", {"command": "git log --oneline"}],
  ["Edit", {"file_path": "/a/b.py", "old_string": "x"}],
  ["Grep", {"pattern": "TODO", "path": "/app/src"}],
  ["Grep", {"pattern": "def main", "glob": "*.py"}],
  ["Glob", {"pattern": "**/*.ts"}],
  ["Bash", {}],
  ["Read", null],
  ["Read", "string"],
  ["Read", {"file_path": "C:\\Users\\me\\proj\\src\\environment.ts"}],
  ["Read", {"file_path": "C:\\Users\\me\\.ssh\\id_rsa.pub"}],
  ["Read", {"file_path": "C:\\Users\\me\\.docker\\daemon.json"}],
  ["Read", {"file_path": "C:\\Users\\me\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\x.lnk"}],
  ["PowerShell", {"command": "Get-ChildItem -Recurse -Filter *.ts"}],
  ["PowerShell", {"command": "ls src"}],
  ["PowerShell", {"command": "dir C:\\work"}],
  ["PowerShell", {"command": "Get-Content README.md"}],
  ["PowerShell", {"command": "cd $env:USERPROFILE"}],
  ["PowerShell", {"command": "Write-Host $env:PATH"}],
  ["PowerShell", {"command": "$env:NODE_ENV = 'test'; npm test"}],
  ["PowerShell", {"command": "Get-Process node"}],
  ["PowerShell", {"command": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 Name, CPU"}],
  ["PowerShell", {"command": "Get-CimInstance Win32_OperatingSystem"}],
  ["PowerShell", {"command": "Get-CimInstance Win32_LogicalDisk"}],
  ["PowerShell", {"command": "Select-String -Path *.md -Pattern 'TODO'"}],
  ["PowerShell", {"command": "Get-ChildItem | Select-String foo"}],
  ["PowerShell", {"command": "powershell -NoProfile -Command \"Get-Date\""}],
  ["PowerShell", {"command": "powershell -EncodedCommand RwBlAHQALQBEAGEAdABlAA=="}],
  ["Bash", {"command": "wmic os get caption"}],
  ["Bash", {"command": "tasklist"}],
  ["Bash", {"command": "set NODE_ENV=production"}],
  ["Bash", {"command": "cmd /c dir"}],
  ["Bash", {"command": "cmd.exe /c echo hi"}],
  ["Bash", {"command": "reg query HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"}],
  ["Bash", {"command": "reg add HKCU\\Software\\Acme /v Theme /d dark"}],
  ["Bash", {"command": "cmdkey /add:host /user:u"}],
  ["Bash", {"command": "echo %PATH%"}],
  ["Bash", {"command": "echo %USERPROFILE%"}],
  ["Bash", {"command": "netsh wlan show interfaces"}],
  ["Bash", {"command": "findstr /s TODO *.ts"}],
  ["Bash", {"command": "powershell -ExecutionPolicy Bypass -Command \"Get-Date\""}],
  ["Bash", {"command": "powershell -NoLogo -c \"ls src\""}],
  ["Bash", {"command": "pwsh -enc:RwBlAHQALQBEAGEAdABlAA=="}],
  ["Bash", {"command": "powershell /c \"dir\""}],
  ["Bash", {"command": "powershell -File build.ps1 -Verbose"}],
  ["Bash", {"command": "powershell -NoProfile -ExecutionPolicy Bypass -File x.ps1"}],
  ["Bash", {"command": "powershell -en RwBlAHQALQBEAGEAdABlAA=="}],
  ["Bash", {"command": "powershell -NoProfile -EncodedCommand RwBlAHQALQBEAGEAdABlAA=="}],
]
// SECRET-CASES-END

test('secretBearing: every call in the shared list that carries a secret is caught', () => {
  expect(SECRET_CALLS.filter(([n, i]) => !secretBearing(n, i))).toEqual([])
})

test('secretBearing: ordinary calls in the shared list pass', () => {
  expect(ORDINARY_CALLS.filter(([n, i]) => secretBearing(n, i))).toEqual([])
})

// A secret-bearing call keeps its tool name and the file (or program), never the content that carries the secret.
const SECRET_VALUE = 'hunter2hunter2zq'
test('blockText: a Write to .env keeps the path only', () => {
  const r = blockText([{ type: 'tool_use', id: 'w1', name: 'Write', input: { file_path: '/app/.env', content: `DB_NOTE=${SECRET_VALUE}\nX=1` } }])
  expect(r.tools).toEqual(['Write(/app/.env)'])
})

test('blockText: echo ... >> .env keeps the program and the file, not the value', () => {
  const r = blockText([{ type: 'tool_use', id: 'w2', name: 'Bash', input: { command: `echo plain${SECRET_VALUE} >> .env` } }])
  expect(r.tools[0]).not.toContain(SECRET_VALUE)
  expect(r.tools[0]).toBe('Bash(echo [arguments withheld] .env)')
})

test('blockText: a heredoc into .env keeps nothing of the body', () => {
  const r = blockText([{ type: 'tool_use', id: 'w3', name: 'Bash', input: { command: `cat > .env <<'EOF'\nTOKEN_X=${SECRET_VALUE}\nEOF` } }])
  expect(r.tools[0]).not.toContain(SECRET_VALUE)
  expect(r.tools[0]).toContain('cat')
})

test('blockText: an Edit of .env keeps the path, not old_string or new_string', () => {
  const r = blockText([{ type: 'tool_use', id: 'w4', name: 'Edit', input: { file_path: '/app/.env', old_string: `A=old${SECRET_VALUE}`, new_string: `A=new${SECRET_VALUE}` } }])
  expect(r.tools).toEqual(['Edit(/app/.env)'])
})

test('blockText: a search for a secret keeps no pattern', () => {
  const r = blockText([{ type: 'tool_use', id: 'w5', name: 'Grep', input: { pattern: `password=${SECRET_VALUE}`, path: '/srv' } }])
  expect(r.tools).toEqual(['Grep(/srv)'])
})

test('blockText: an ordinary call keeps its whole (redacted) input', () => {
  const r = blockText([{ type: 'tool_use', id: 'w6', name: 'Edit', input: { file_path: '/app/main.py', old_string: 'a', new_string: 'b' } }])
  expect(r.tools[0]).toContain('new_string')
})

// Shapes that made an earlier version of a rule quadratic: each must be scanned in well under a second.
const HOSTILE: Record<string, string> = {
  quotes: "'".repeat(200_000) + 'env', 'service account': '/' + 'service-account-'.repeat(15_000) + '!',
  tfstate: 'terraform.tfstate'.repeat(15_000) + '!', begin: 'BEGIN '.repeat(50_000), 'env dots': '.env'.repeat(60_000) + '!',
  'sudo flags': 'sudo ' + '-a '.repeat(80_000), assignments: 'a=b '.repeat(60_000) + 'env', proc: '/proc/x'.repeat(40_000),
  // Windows rules
  'get-process': 'get-process x '.repeat(40_000), gps: 'gps '.repeat(120_000), 'reg query': 'reg query '.repeat(50_000),
  'cmd flags': 'cmd ' + '/a '.repeat(80_000), 'powershell flags': 'powershell ' + '-a '.repeat(80_000), gci: 'gci '.repeat(120_000),
  percent: '%A_KEY'.repeat(70_000), 'env colon': '$env:'.repeat(90_000), wmic: 'wmic process '.repeat(30_000),
  encoded: 'powershell -enc ' + 'A'.repeat(400_000), 'many encoded': 'powershell -enc ' + 'AAAAAAAAAAAAAAAAAAAA '.repeat(20_000),
  netsh: 'netsh wlan show profile '.repeat(20_000), hklm: 'HKLM:\\'.repeat(70_000), 'cmd.exe': 'cmd.exe '.repeat(60_000),
}
for (const [name, v] of Object.entries(HOSTILE)) {
  test(`secretBearing is linear on hostile input: ${name}`, () => {
    const t0 = Date.now()
    secretBearing('Bash', { command: v }); secretBearing('Read', { file_path: v }); secretBearing('Grep', { pattern: v })
    expect(Date.now() - t0).toBeLessThan(3000)
  })
}

// 2.0.1: the quotes around a `sh -c` body were trimmed with a regex ending in `$`, quadratic on a long run of quotes or
// spaces inside the body (2 s at 50,000, 40 s at 200,000). Each 500 KB shape must now be scanned in under a second.
const N = 500_000
const HOSTILE_SHELL_C: Record<string, string> = {
  'double quotes': 'bash -c "x' + '"'.repeat(N) + 'y"', 'single quotes': "sh -c 'x" + "'".repeat(N) + "y'",
  'mixed quotes': 'bash -c "x' + `"'`.repeat(N / 2) + 'y"', spaces: 'bash -c "x' + ' '.repeat(N) + 'y"',
  'quotes and spaces': "zsh -c 'x" + "' \t".repeat(N / 4) + "y'", 'unicode spaces': 'bash -c "x' + '　 '.repeat(N / 2) + 'y"',
  sudo: 'sudo -E bash -c "x' + '"'.repeat(N) + 'y"', nested: `bash -c "sh -c 'x` + "'".repeat(N) + `y'"`,
  flags: 'bash -l -e -c "x' + '"'.repeat(N) + 'y"', 'no closing': 'bash -c "x' + '"'.repeat(N) + 'y',
}
for (const [name, v] of Object.entries(HOSTILE_SHELL_C)) {
  test(`a sh -c body is trimmed in linear time: ${name}`, () => {
    const t0 = Date.now()
    expect(secretBearing('Bash', { command: v })).toBe(false)
    expect(Date.now() - t0).toBeLessThan(1000)
  })
}

test('a hostile sh -c body is still read', () => {
  expect(secretBearing('Bash', { command: 'bash -c "' + '"'.repeat(N) + 'env' + '"'.repeat(N) + '"' })).toBe(true)
  expect(secretBearing('Bash', { command: "sh -c '" + "' ".repeat(N / 2) + "printenv'" })).toBe(true)
})

test('trimQuotes gives what the old regex gave', () => {
  const old = (s: string) => s.replace(/^["'\s]+|["'\s]+$/g, '')
  const normal = ['env', "'env'", '"cd x && env"', "  'ls -la'  ", `"'printenv'"`, "'", '""', '', ' \t\n', "'a' 'b'",
    '　env ', `x'y"z`, `'echo "hi there"'`, "'\t'cmd'\n'"]
  let seed = 201
  const rnd = (k: number) => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % k }
  const alphabet = `"' \t\nab;　\u0085﻿`
  for (let i = 0; i < 3000; i++) normal.push(Array.from({ length: rnd(12) }, () => alphabet[rnd(alphabet.length)]).join(''))
  for (const s of normal) expect(trimQuotes(s)).toBe(old(s))
})

// queuedPrompt had two more: `\s*<\/system-reminder>\s*$` and a lazy body before the engine's note (2 s and 4 s at 50,000
// spaces or newlines). The old function is kept here as the reference for the parity cases.
function oldQueuedPrompt(rendered: string): { text: string; user: boolean } {
  const s = String(rendered ?? '').replace(/^\s*<system-reminder>\s*/, '').replace(/\s*<\/system-reminder>\s*$/, '')
  const m = /^The user sent a new message while you were working:[ \t]*\n([\s\S]*?)(?:\n\s*This is how Claude Code surfaces messages the user sends mid-turn[\s\S]*)?$/.exec(s)
  if (m) return { text: m[1]!.trim(), user: true }
  return { text: s.trim(), user: false }
}
const HEAD = 'The user sent a new message while you were working:'
const NOTE = 'This is how Claude Code surfaces messages the user sends mid-turn'

test('queuedPrompt is linear on a long run of spaces or newlines', () => {
  for (const fill of [' ', '\n', '\n ', '\t']) {
    for (const v of [`<system-reminder>\n${HEAD}\nx${fill.repeat(N)}y`, `<system-reminder>\nx${fill.repeat(N)}y`,
      `${HEAD}\n${fill.repeat(N)}${NOTE}.\n</system-reminder>`, `${HEAD}\n${(NOTE + fill).repeat(N / 100)}`]) {
      const t0 = Date.now()
      queuedPrompt(v)
      expect(Date.now() - t0).toBeLessThan(1000)
    }
  }
})

test('queuedPrompt gives what the old function gave', () => {
  const words = ['please also check the logs', 'two\nlines', '  padded  ', '', 'it said: ' + NOTE + ' inline', 'a\n\n\nb']
  const cases: string[] = []
  for (const w of words) {
    for (const note of ['', `\n\n${NOTE}.\n`, `\n${NOTE}`, ` ${NOTE}.`, `\n \t\n${NOTE}. And more.\n${NOTE}.`]) {
      for (const [open, close] of [['<system-reminder>\n', '\n</system-reminder>'], ['', ''], ['  <system-reminder>', '</system-reminder>  \n'], ['', '</system-reminder> x']]) {
        cases.push(`${open}${HEAD}\n${w}${note}${close}`, `${open}${HEAD} \t\n${w}${note}${close}`, `${open}${w}${note}${close}`, `${open}${HEAD}${w}${close}`)
      }
    }
  }
  cases.push('', '<system-reminder>', '</system-reminder>', `${HEAD}\n`, `${HEAD}\n${NOTE}`, `${HEAD}\n\n${NOTE}`)
  for (const c of cases) expect(queuedPrompt(c)).toEqual(oldQueuedPrompt(c))
})

// ---- -EncodedCommand cannot be used to get past the gate (the same cases as tests/test_importer_safety.py) ----
const enc = (s: string): string => b64utf16le(s)

test('an encoded payload over the size limit is withheld, not skipped', () => {
  const padded = enc('Get-ChildItem Env:' + ' '.repeat(210_000))
  expect(padded.length).toBeGreaterThan(270_000)
  expect(secretBearing('Bash', { command: 'powershell -enc ' + padded })).toBe(true)
  expect(secretBearing('Bash', { command: 'powershell -enc ' + enc('Get-Date' + ' '.repeat(90_000)) })).toBe(false)
  expect(secretBearing('Bash', { command: 'powershell -enc ' + enc('x'.repeat(100_001)) })).toBe(true) // 200,002 bytes
})

test('a ninth encoded payload is withheld, not skipped', () => {
  const benign = enc('Get-Date')
  const eight = Array.from({ length: 8 }, () => `powershell -enc ${benign}`).join('; ')
  expect(secretBearing('Bash', { command: eight })).toBe(false)
  expect(secretBearing('Bash', { command: eight + '; powershell -enc ' + enc('Get-ChildItem Env:') })).toBe(true)
  expect(secretBearing('Bash', { command: eight + '; powershell -enc ' + benign })).toBe(true)
})

for (const flag of ['-e', '-ec', '-en', '-enc', '-enco', '-EncodedCommand', '/enc', '-ENC', '-enc:', '-EncodedCommand:', '/e:']) {
  test(`the encoded flag ${flag} is inspected`, () => {
    const glue = flag.endsWith(':') ? '' : ' '
    expect(secretBearing('Bash', { command: `powershell ${flag}${glue}${enc('Get-ChildItem Env:')}` })).toBe(true)
    expect(secretBearing('Bash', { command: `powershell ${flag}${glue}${enc('Get-Date')}` })).toBe(false)
  })
}

test('scripts inside scripts past the depth limit are withheld', () => {
  let script = 'Get-Date'
  for (let i = 0; i < 6; i++) script = 'powershell -enc ' + enc(script)
  expect(secretBearing('Bash', { command: script })).toBe(true)
})

for (const cmd of [
  'powershell -ExecutionPolicy Bypass -Command "printenv"',
  'pwsh -NoLogo -ExecutionPolicy Bypass -Command "compgen -v"',
  'powershell -NoProfile -WindowStyle Hidden -com "env"',
  'powershell /command printenv',
  'powershell -Command:printenv',
]) {
  test(`-Command is found after value-taking flags: ${cmd}`, () => {
    expect(secretBearing('Bash', { command: cmd })).toBe(true)
  })
}
