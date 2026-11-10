"""Calls that must (and must not) count as secret-bearing. The same list sits between the SECRET-CASES markers in
claude-code/chronicle/tests/redact.test.ts: ``test_secret_cases_match_the_mod`` fails when the two drift apart."""

from __future__ import annotations

SECRET_CALLS: list[tuple[str, dict]] = [
    *[("Read", {"file_path": p}) for p in (
        "/app/.env", "/app/.env.local", "/app/.env.production.local", "/app/.env.bak", "/x/.envrc", "/x/prod.env",
        "/home/user/.npmrc", "/home/user/.pypirc", "/home/user/.netrc", "/home/user/.pgpass", "/home/user/.git-credentials",
        "/home/user/.docker/config.json", "/home/user/.kube/config", "/x/secrets.yml", "/x/secrets.json", "/x/secret.toml",
        "/x/cert.p12", "/x/cert.pfx", "/home/user/.vault-token", "/infra/terraform.tfstate",
        "/infra/terraform.tfstate.backup", "/proc/self/environ", "/proc/1234/cmdline", "/home/user/.ssh/id_rsa",
        "/home/user/.ssh/id_ed25519", "/home/user/.ssh/id_ecdsa", "/home/user/.ssh/id_dsa", "/home/user/.aws/credentials",
        "/x/key.pem", "/x/private/notes.md", "/x/credentials.json", "/x/.mcp.json", "/x/.dev.vars",
        "/gcp/my-service-account.json", "/x/terraform.tfvars", "/etc/shadow")],
    ("Write", {"file_path": "/app/.env", "content": "API_KEY=zz"}),
    ("Edit", {"file_path": "/app/.env", "old_string": "A=1", "new_string": "A=2"}),
    *[("Bash", {"command": c}) for c in (
        "env", "printenv", "ps aux", "cd x && env | sort", "sudo -E printenv HOME", "sudo env", "sudo -u root env",
        "pm2 env 0", "export", "export -p", "declare -x", "set", "cat .env", "cat .env.production", "cat .env*",
        "cat '.env'", "cat ./.env.local", "cat *.env", "cat ~/.ssh/id_ed25519", "echo $(env)", "x=`printenv`",
        "/usr/bin/printenv", "/usr/bin/env", "exec env", "time env", "command env", "nohup env", "FOO=1 env",
        "FOO=1 BAR=2 printenv", "bash -c env", "sh -c 'printenv'", "bash -c \"cd x && env\"", "sudo bash -c 'env'",
        "env -0", "docker compose config", "docker-compose config", "docker inspect web",
        "docker exec web env", "systemctl show -p Environment myservice", "systemctl cat myservice", "ssh-add -L",
        "python3 -c 'import os; print(os.environ)'", "node -e 'console.log(process.env)'", "cat /proc/self/environ",
        "xargs -0 < /proc/self/environ", "tr '\\0' '\\n' < /proc/1/environ", "cat /proc/1/cmdline",
        "cat ~/.npmrc", "cat ~/.git-credentials", "cat .envrc", "cat terraform.tfstate", "cat ~/.docker/config.json",
        "cat ~/.kube/config", "history", "kubectl get secret db -o yaml", "gh auth token",
        "aws configure get aws_secret_access_key", "grep -r password .", "rg 'API_KEY' src", "grep -rn token config/")],
    ("Grep", {"pattern": "password", "path": "/srv"}),
    ("Grep", {"pattern": "API_KEY=\\w+"}),
    ("Grep", {"pattern": "x", "path": "/app/.env"}),
    ("Grep", {"pattern": "foo", "glob": "*.env"}),
    ("Grep", {"pattern": "BEGIN [A-Z ]*PRIVATE KEY"}),
    ("Glob", {"pattern": "**/.env*"}),
    ("Glob", {"pattern": "**/*.pem"}),
    ("Glob", {"pattern": "**/secrets.*"}),
    ("Glob", {"pattern": "**/*", "path": "/home/user/.ssh/id_rsa"}),
    # --- Windows: file names with backslashes, and PowerShell / cmd commands (the tool is `PowerShell` or `Bash`) ---
    *[("Read", {"file_path": p}) for p in (
        "C:\\proj\\.env", "C:\\proj\\secrets.yml", "C:\\Users\\me\\.ssh\\id_rsa", "C:\\Users\\me\\.aws\\credentials",
        "C:\\Users\\me\\.npmrc", "C:\\Users\\me\\.git-credentials", "C:\\Users\\me\\.docker\\config.json",
        "C:\\Users\\me\\.kube\\config", "D:\\x\\private\\notes.md", "C:\\Users\\me\\NTUSER.DAT", "C:\\Windows\\System32\\config\\SAM",
        "C:\\Users\\me\\.azure\\accessTokens.json", "C:\\Users\\me\\AppData\\Roaming\\GitHub CLI\\gh\\hosts.yml",
        "C:\\Users\\me\\AppData\\Roaming\\Microsoft\\Credentials\\ABCDEF0123",
        "C:\\Users\\me\\AppData\\Roaming\\Microsoft\\Windows\\PowerShell\\PSReadLine\\ConsoleHost_history.txt")],
    *[("PowerShell", {"command": c}) for c in (
        "Get-ChildItem Env:", "gci env:", "GCI ENV: | Sort-Object Name", "dir env:", "ls Env:\\", "Get-Item -Path Env:*",
        "Get-Content env:OPENAI_API_KEY", "Write-Host $env:GITHUB_TOKEN", "echo ${env:DB_PASSWORD}",
        "[Environment]::GetEnvironmentVariable('PATH')", "[System.Environment]::GetEnvironmentVariables()",
        "[Environment]::GetEnvironmentVariables('User') | Format-List",
        "Get-CimInstance Win32_Process | Select-Object Name, CommandLine",
        "Get-WmiObject -Class Win32_Process -Filter \"name='node.exe'\"",
        "Get-CimInstance -ClassName win32_service | select Name, PathName, StartName",
        "Get-Process | Select-Object Name, CommandLine", "gps node | select Id,CommandLine",
        "Get-ItemProperty -Path 'HKCU:\\Environment'", "Get-StoredCredential -Target git", "Get-Secret -Name token -AsPlainText",
        "$c.GetNetworkCredential().Password", "$s | ConvertFrom-SecureString", "Get-Content .\\.env.local",
        "gc C:\\Users\\me\\.ssh\\id_ed25519", "cat $HOME\\.aws\\credentials", "Get-ChildItem -Recurse | Select-String password",
        "sls -Path . -Pattern 'api_key' -Recurse", "powershell -NoProfile -Command \"gci env:\"")],
    *[("Bash", {"command": c}) for c in (
        "wmic process get commandline", "wmic process where name=\"node.exe\" get commandline,processid",
        "C:\\Windows\\System32\\wbem\\WMIC.exe process list full", "wmic environment get name,variablevalue",
        "cmdkey /list", "cmdkey.exe /list:git:https://github.com", "vaultcmd /listcreds:\"Windows Credentials\"",
        "reg query HKCU\\Environment", "reg query \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment\"",
        "reg query HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon", "reg query HKCU\\Software\\Acme /v ApiToken",
        "reg.exe query HKCU\\Software /s /f password", "cmd /c set", "cmd.exe /c \"set\"", "cmd /c set API", "set AWS",
        "echo %GITHUB_TOKEN%", "echo %API_KEY%", "powershell.exe -c \"Get-ChildItem Env:\"",
        "pwsh -NoProfile -Command 'Get-CimInstance Win32_Process'",
        "powershell -NoProfile -EncodedCommand RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQAgAEUAbgB2ADoA",
        "powershell -enc WwBFAG4AdgBpAHIAbwBuAG0AZQBuAHQAXQA6ADoARwBlAHQARQBuAHYAaQByAG8AbgBtAGUAbgB0AFYAYQByAGkAYQBiAGwAZQBzACgAKQA=",
        "netsh wlan show profile name=\"Home\" key=clear", "type .env", "findstr /s /i password *.config",
        "C:\\Windows\\System32\\findstr.exe /s token *.json")],
]

ORDINARY_CALLS: list[tuple[str, object]] = [
    *[("Read", {"file_path": p}) for p in (
        "/app/src/environment.ts", "/app/README.md", "/app/keys.md", "/home/user/.ssh/id_rsa.pub", "/app/src/secretary.ts",
        "/app/idle.txt", "/app/src/envelope.py", "/app/docs/tokens-of-appreciation.md")],
    *[("Bash", {"command": c}) for c in (
        "ls -la", "set -e", "/usr/bin/env python3 run.py", "git status", "envsubst < a", "echo environment",
        "grep -rn TODO src", "docker compose up -d", "systemctl status nginx", "pm2 list", "echo hello",
        "python3 manage.py test", "echo $(date)", "cd /srv && make build", "FOO=1 make test", "bash -c 'ls -la'",
        "sudo apt-get update", "time make", "git log --oneline")],
    ("Edit", {"file_path": "/a/b.py", "old_string": "x"}),
    ("Grep", {"pattern": "TODO", "path": "/app/src"}),
    ("Grep", {"pattern": "def main", "glob": "*.py"}),
    ("Glob", {"pattern": "**/*.ts"}),
    ("Bash", {}), ("Read", None), ("Read", "string"),
    # --- Windows ---
    *[("Read", {"file_path": p}) for p in (
        "C:\\Users\\me\\proj\\src\\environment.ts", "C:\\Users\\me\\.ssh\\id_rsa.pub", "C:\\Users\\me\\.docker\\daemon.json",
        "C:\\Users\\me\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\x.lnk")],
    *[("PowerShell", {"command": c}) for c in (
        "Get-ChildItem -Recurse -Filter *.ts", "ls src", "dir C:\\work", "Get-Content README.md", "cd $env:USERPROFILE",
        "Write-Host $env:PATH", "$env:NODE_ENV = 'test'; npm test", "Get-Process node",
        "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 Name, CPU", "Get-CimInstance Win32_OperatingSystem",
        "Get-CimInstance Win32_LogicalDisk", "Select-String -Path *.md -Pattern 'TODO'", "Get-ChildItem | Select-String foo",
        "powershell -NoProfile -Command \"Get-Date\"", "powershell -EncodedCommand RwBlAHQALQBEAGEAdABlAA==")],
    *[("Bash", {"command": c}) for c in (
        "wmic os get caption", "tasklist", "set NODE_ENV=production", "cmd /c dir", "cmd.exe /c echo hi",
        "reg query HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run", "reg add HKCU\\Software\\Acme /v Theme /d dark",
        "cmdkey /add:host /user:u", "echo %PATH%", "echo %USERPROFILE%", "netsh wlan show interfaces", "findstr /s TODO *.ts")],
]
