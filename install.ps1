# ApplyPilot setup script: Windows
# Usage (run in PowerShell):
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
#   irm https://raw.githubusercontent.com/ciguarin/applypilot/main/install.ps1 | iex

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

# ── 3. Discovery dependencies (jobspy) ───────────────────────────────────────
Write-Host "Installing discovery dependencies..."
& $uvPython -m pip install --no-deps python-jobspy --quiet 2>$null
& $uvPython -m pip install pydantic tls-client requests markdownify regex --quiet 2>$null
Write-Host "OK python-jobspy installed"

# ── 4. Node.js MCPs ───────────────────────────────────────────────────────────
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host "Pre-installing Node.js MCPs..."
    npm install -g --silent @playwright/mcp @codefuturist/email-mcp
    Write-Host "OK @playwright/mcp + @codefuturist/email-mcp"
} else {
    Write-Host "  npm not found. Node.js MCPs will download on first use"
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
    Write-Host "No system browser found. Downloading Playwright Chromium (~300MB)..."
    npx --yes playwright install chromium
    Write-Host "OK Playwright Chromium installed"
} else {
    Write-Host "  No browser found and npx unavailable. Install Chrome or Node.js"
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
$uvApplyPilotExe = Join-Path $uvToolDir "applypilot\Scripts\applypilot.exe"

# Paths below are baked in as literals resolved right now (not `$env:USERPROFILE`
# lookups). Task Scheduler spawns this script in a fresh process tree that does
# not reliably inherit the interactive user's environment, so any dynamic env-var
# resolution here can silently produce a bad path with zero error output.
@"
`$ErrorActionPreference = 'SilentlyContinue'
`$log = "$APPLYPILOT_DIR\logs\apply_daemon.log"

New-Item -ItemType Directory -Force -Path "$APPLYPILOT_DIR\logs" | Out-Null

# -Encoding utf8 pinned throughout: Windows PowerShell 5.1's `>>` and Add-Content
# default to UTF-16, which garbles this log for anything reading it as UTF-8.

Add-Content `$log "" -Encoding utf8
Add-Content `$log "======================================" -Encoding utf8
Add-Content `$log "`$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -- daemon run start" -Encoding utf8
Add-Content `$log "======================================" -Encoding utf8

# Note: stuck 'in_progress' jobs from a previous crashed run are reset
# automatically by `applypilot apply` itself (launcher.main() calls
# reset_stuck_jobs() on startup) -- no separate step needed here.

Add-Content `$log "`$(Get-Date -Format 'HH:mm:ss') -- running pipeline..." -Encoding utf8
& "$uvApplyPilotExe" run 2>&1 | Out-File -FilePath `$log -Append -Encoding utf8

Add-Content `$log "`$(Get-Date -Format 'HH:mm:ss') -- running apply..." -Encoding utf8
& "$uvApplyPilotExe" apply --limit 15 --workers 2 --model haiku --headless 2>&1 | Out-File -FilePath `$log -Append -Encoding utf8

Add-Content `$log "`$(Get-Date -Format 'HH:mm:ss') -- done" -Encoding utf8
"@ | Set-Content "$APPLYPILOT_DIR\apply_daemon.ps1" -Encoding UTF8

# ── 7. Task Scheduler ─────────────────────────────────────────────────────────
$taskName = "ApplyPilot.Apply"
# Windows PowerShell 5.1 (not pwsh 7), deliberately: pwsh.exe is known to hang
# indefinitely with zero output when launched by Task Scheduler on some Windows 11
# machines (it tries to route through the Windows Terminal host with no interactive
# session available to host it). powershell.exe predates that integration and
# doesn't hit it. This script has no PS7-only syntax, so 5.1 is a safe target.
# -WindowStyle Hidden is also omitted, it's one of the reported triggers for the
# same hang class.
$psExe = (Get-Command powershell -ErrorAction SilentlyContinue).Source
if (-not $psExe) { $psExe = "powershell.exe" }

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NonInteractive -File `"$APPLYPILOT_DIR\apply_daemon.ps1`"" `
    -WorkingDirectory $APPLYPILOT_DIR

# Two daily triggers = every 12h; avoids RepetitionDuration XML bugs and elevation requirements
$trigger1 = New-ScheduledTaskTrigger -Daily -At "08:00"
$trigger2 = New-ScheduledTaskTrigger -Daily -At "20:00"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -WakeToRun

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$taskResult = Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($trigger1, $trigger2) `
    -Settings $settings `
    -ErrorAction SilentlyContinue

if ($taskResult) {
    Write-Host "OK Apply daemon scheduled (runs at 08:00 and 20:00 daily)"
} else {
    Write-Host "WARN: Daemon scheduling failed. Run 'applypilot doctor' to diagnose"
}

Write-Host ""
Write-Host "=== Done! Run: applypilot init ==="
Write-Host ""
