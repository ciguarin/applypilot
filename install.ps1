# ApplyPilot setup script — Windows
# Run from PowerShell (as your normal user, no admin required):
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
#   & "$env:USERPROFILE\.applypilot\install.ps1"

$ErrorActionPreference = "Stop"
$APPLYPILOT_DIR = "$env:USERPROFILE\.applypilot"

Write-Host "=== ApplyPilot Setup ==="

# ── 1. uv ─────────────────────────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
}
Write-Host "OK uv $(uv --version)"

# ── 2. applypilot ─────────────────────────────────────────────────────────────
Write-Host "Installing applypilot..."
uv tool install applypilot
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
Write-Host "OK applypilot $(applypilot --version 2>$null)"

# ── 3. patches ────────────────────────────────────────────────────────────────
Write-Host "Applying patches..."
$uvToolDir = & uv tool dir
$uvPython  = Join-Path $uvToolDir "applypilot\Scripts\python.exe"
$dst = & $uvPython -c "import applypilot, os; print(os.path.dirname(applypilot.__file__))"
$src = Join-Path $APPLYPILOT_DIR "patches"

@(
    "scoring\validator.py",
    "scoring\tailor.py",
    "scoring\pdf.py",
    "cli.py",
    "apply\prompt.py",
    "apply\launcher.py",
    "wizard\init.py",
    "discovery\github_readme.py"
) | ForEach-Object {
    Copy-Item -Force (Join-Path $src $_) (Join-Path $dst $_)
}
Write-Host "OK Patches applied to $dst"

# ── 4. Python extras ──────────────────────────────────────────────────────────
Write-Host "Installing Python extras..."
& uv pip install --python $uvPython --quiet pypdf
Write-Host "OK pypdf (PDF-to-text conversion)"

# ── 5. Node.js MCPs ───────────────────────────────────────────────────────────
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host "Pre-installing Node.js MCPs..."
    npm install -g --silent @playwright/mcp @codefuturist/email-mcp
    Write-Host "OK @playwright/mcp + @codefuturist/email-mcp"
} else {
    Write-Host "  npm not found — Node.js MCPs will download on first use"
    Write-Host "  Install Node.js from https://nodejs.org to pre-cache them"
}

# ── 6. Browser ────────────────────────────────────────────────────────────────
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

# ── 7. Config templates (only if not already present) ────────────────────────
if (-not (Test-Path "$APPLYPILOT_DIR\.env")) {
    Copy-Item "$APPLYPILOT_DIR\.env.example" "$APPLYPILOT_DIR\.env"
}
if (-not (Test-Path "$APPLYPILOT_DIR\profile.json")) {
    Copy-Item "$APPLYPILOT_DIR\config\profile.example.json" "$APPLYPILOT_DIR\profile.json"
}
if (-not (Test-Path "$APPLYPILOT_DIR\searches.yaml")) {
    Copy-Item "$APPLYPILOT_DIR\config\searches.example.yaml" "$APPLYPILOT_DIR\searches.yaml"
}
Write-Host "OK Config templates ready"

# ── 8. Task Scheduler (12h apply daemon) ─────────────────────────────────────
$taskName = "ApplyPilot.Apply"
$logDir   = "$APPLYPILOT_DIR\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Write the daemon script (equivalent of apply_daemon.sh)
@"
`$ErrorActionPreference = 'SilentlyContinue'
`$db = "`$env:USERPROFILE\.applypilot\applypilot.db"

python -c "
import sqlite3
conn = sqlite3.connect(r'`$db')
reset = conn.execute(\"UPDATE jobs SET apply_status = NULL WHERE apply_status = 'in_progress'\").rowcount
conn.commit()
if reset: print(f'Reset {reset} stuck jobs')
" 2>`$null

applypilot apply --limit 15 --workers 2 --model haiku --headless --max-turns 30 ``
    >> "`$env:USERPROFILE\.applypilot\logs\apply_daemon.log" 2>&1
"@ | Set-Content "$APPLYPILOT_DIR\apply_daemon.ps1" -Encoding UTF8

$action = New-ScheduledTaskAction `
    -Execute "pwsh.exe" `
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
