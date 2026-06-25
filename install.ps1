# ApplyPilot setup script — Windows
# Usage (run in PowerShell):
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
#   irm https://raw.githubusercontent.com/ciguarin/applypilot/v1/install.ps1 | iex

$ErrorActionPreference = "Stop"
$APPLYPILOT_DIR = "$env:USERPROFILE\.applypilot"
$REPO = "https://github.com/ciguarin/applypilot"

Write-Host "=== ApplyPilot Setup ==="

# ── 1. uv ─────────────────────────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
}
Write-Host "OK uv $(uv --version)"

# ── 2. applypilot (install from git) ──────────────────────────────────────────
Write-Host "Installing applypilot..."
uv tool install "git+$REPO@v1" --force
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
Write-Host "OK applypilot $(applypilot --version 2>$null)"

$uvToolDir = & uv tool dir
$uvPython  = Join-Path $uvToolDir "applypilot\Scripts\python.exe"
$pkg = & $uvPython -c "import applypilot, os; print(os.path.dirname(applypilot.__file__))"

# ── 3. Node.js MCPs ───────────────────────────────────────────────────────────
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host "Pre-installing Node.js MCPs..."
    npm install -g --silent @playwright/mcp @codefuturist/email-mcp
    Write-Host "OK @playwright/mcp + @codefuturist/email-mcp"
} else {
    Write-Host "  npm not found — Node.js MCPs will download on first use"
    Write-Host "  Install Node.js from https://nodejs.org to pre-cache them"
}

# ── 4. Browser ────────────────────────────────────────────────────────────────
$browserPaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\BraveSoftware\Brave-Browser\Application\brave.exe"
)
$foundBrowser = $browserPaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($foundBrowser) {
    Write-Host "OK System browser detected: $(Split-Path $foundBrowser -Leaf)"
} elseif (Get-Command npx -ErrorAction SilentlyContinue) {
    Write-Host "No system browser found — downloading Playwright Chromium (~300MB)..."
    npx --yes playwright install chromium
    Write-Host "OK Playwright Chromium installed"
} else {
    Write-Host "  No browser found and npx unavailable — install Chrome or Node.js"
}

# ── 5. Data directory + config templates ──────────────────────────────────────
New-Item -ItemType Directory -Force -Path "$APPLYPILOT_DIR\logs" | Out-Null

if (-not (Test-Path "$APPLYPILOT_DIR\.env")) {
    Copy-Item "$pkg\config\.env.example" "$APPLYPILOT_DIR\.env"
}
if (-not (Test-Path "$APPLYPILOT_DIR\profile.json")) {
    Copy-Item "$pkg\config\profile.example.json" "$APPLYPILOT_DIR\profile.json"
}
if (-not (Test-Path "$APPLYPILOT_DIR\searches.yaml")) {
    Copy-Item "$pkg\config\searches.example.yaml" "$APPLYPILOT_DIR\searches.yaml"
}
Write-Host "OK Config templates ready"

# ── 6. Daemon script ──────────────────────────────────────────────────────────
# Write the daemon script (Windows PowerShell equivalent of apply_daemon.sh)
@"
`$ErrorActionPreference = 'SilentlyContinue'
`$db = "`$env:USERPROFILE\.applypilot\applypilot.db"
`$log = "`$env:USERPROFILE\.applypilot\logs\apply_daemon.log"

New-Item -ItemType Directory -Force -Path "`$env:USERPROFILE\.applypilot\logs" | Out-Null

Add-Content `$log ""
Add-Content `$log "======================================"
Add-Content `$log "`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -- daemon run start"
Add-Content `$log "======================================"

& "$uvToolDir\applypilot\Scripts\python.exe" -c "
import sqlite3
conn = sqlite3.connect(r'`$db')
reset = conn.execute(\"UPDATE jobs SET apply_status = NULL WHERE apply_status = 'in_progress'\").rowcount
conn.commit()
if reset: print(f'Reset {reset} stuck jobs')
" >> `$log 2>&1

Add-Content `$log "`$(Get-Date -Format 'HH:mm:ss') -- running pipeline..."
applypilot run >> `$log 2>&1

Add-Content `$log "`$(Get-Date -Format 'HH:mm:ss') -- running apply..."
applypilot apply --limit 15 --workers 2 --model haiku --headless >> `$log 2>&1

Add-Content `$log "`$(Get-Date -Format 'HH:mm:ss') -- done"
"@ | Set-Content "$APPLYPILOT_DIR\apply_daemon.ps1" -Encoding UTF8

# ── 7. Task Scheduler ─────────────────────────────────────────────────────────
$taskName = "ApplyPilot.Apply"

$psExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh.exe" } else { "powershell.exe" }
$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NonInteractive -WindowStyle Hidden -File `"$APPLYPILOT_DIR\apply_daemon.ps1`"" `
    -WorkingDirectory $APPLYPILOT_DIR

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 12) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest | Out-Null
Write-Host "OK Apply daemon scheduled (runs every 12h via Task Scheduler)"

Write-Host ""
Write-Host "=== Done! Run: applypilot init ==="
Write-Host ""
