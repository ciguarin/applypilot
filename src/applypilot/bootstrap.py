"""Post-install bootstrap: jobspy deps and daemon registration.

Called by `applypilot repair`, end of `applypilot update`, and end of `applypilot init`.
Safe to run multiple times.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _tool_python() -> str:
    # sys.prefix is the venv root -- works even when sys.executable is a uv shim on Windows.
    if sys.platform == "win32":
        return str(Path(sys.prefix) / "Scripts" / "python.exe")
    return str(Path(sys.prefix) / "bin" / "python")


def install_jobspy(console) -> bool:
    """Install jobspy into the running Python's venv. Returns True on success."""
    py = _tool_python()

    # Skip if already installed
    check = subprocess.run([py, "-c", "import jobspy"], capture_output=True)
    if check.returncode == 0:
        console.print("  [green]OK python-jobspy (already installed)[/green]")
        return True

    console.print("  Installing discovery dependencies...")
    r1 = subprocess.run(["uv", "pip", "install", "--no-deps", "python-jobspy", "--python", py])
    r2 = subprocess.run(["uv", "pip", "install", "pandas", "pydantic", "tls-client", "requests", "markdownify", "regex", "--python", py])

    if r1.returncode == 0 and r2.returncode == 0:
        console.print("  [green]OK python-jobspy[/green]")
        return True
    else:
        console.print("  [red]FAIL jobspy install failed -- discovery will be limited to GitHub README sources[/red]")
        return False


def install_playwright(console) -> bool:
    """Install Playwright browser (chromium headless shell). Returns True on success."""
    py = _tool_python()
    r = subprocess.run([py, "-m", "playwright", "install", "chromium"], capture_output=True)
    if r.returncode == 0:
        console.print("  OK Playwright chromium")
        return True
    else:
        console.print(f"  WARN Playwright install failed: {r.stderr.decode(errors='replace').strip()[:200]}")
        return False


def register_daemon(console) -> bool:
    """Register the background daemon. Returns True on success."""
    from applypilot.config import APP_DIR

    if sys.platform == "win32":
        return _register_daemon_windows(console, APP_DIR)
    elif sys.platform == "darwin":
        return _register_daemon_macos(console, APP_DIR)
    else:
        console.print("  [dim]Daemon auto-registration not supported on this platform.[/dim]")
        return False


def _register_daemon_windows(console, app_dir: Path) -> bool:
    import shutil
    ps = "pwsh.exe" if shutil.which("pwsh") else "powershell.exe"
    d = str(app_dir).replace("'", "''")
    script = f"""
$D = '{d}'
$ps = '{ps}'
$action = New-ScheduledTaskAction `
    -Execute $ps `
    -Argument "-NonInteractive -WindowStyle Hidden -File `"$D\\apply_daemon.ps1`"" `
    -WorkingDirectory $D
$trigger1 = New-ScheduledTaskTrigger -Daily -At "08:00"
$trigger2 = New-ScheduledTaskTrigger -Daily -At "20:00"
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable
Unregister-ScheduledTask -TaskName "ApplyPilot.Apply" -Confirm:$false -ErrorAction SilentlyContinue
$r = Register-ScheduledTask -TaskName "ApplyPilot.Apply" `
    -Action $action -Trigger @($trigger1, $trigger2) -Settings $settings `
    -ErrorAction SilentlyContinue
if ($r) {{ exit 0 }} else {{ exit 1 }}
"""
    result = subprocess.run([ps, "-NoProfile", "-Command", script], capture_output=True)
    if result.returncode == 0:
        console.print("  [green]OK Daemon registered (runs at 08:00 and 20:00 daily)[/green]")
        return True
    else:
        console.print("  [yellow]WARN Daemon registration failed[/yellow]")
        return False


def _register_daemon_macos(console, app_dir: Path) -> bool:
    import applypilot as _ap
    plist_template = Path(_ap.__file__).parent / "scripts" / "launchagents" / "com.applypilot.apply.plist.template"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_dest = launch_agents / "com.applypilot.apply.plist"

    if not plist_template.exists():
        console.print("  [yellow]WARN plist template not found in package[/yellow]")
        return False

    launch_agents.mkdir(parents=True, exist_ok=True)
    content = plist_template.read_text().replace("__HOME__", str(Path.home()))
    plist_dest.write_text(content)

    subprocess.run(["launchctl", "unload", str(plist_dest)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_dest)], capture_output=True)

    r = subprocess.run(["launchctl", "list", "com.applypilot.apply"], capture_output=True)
    if r.returncode == 0:
        console.print("  [green]OK Daemon loaded (runs at 08:00 and 20:00 daily)[/green]")
        return True
    else:
        console.print("  [yellow]WARN Daemon not loaded -- check Console.app for errors[/yellow]")
        return False
