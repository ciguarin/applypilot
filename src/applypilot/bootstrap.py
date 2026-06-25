"""Post-install bootstrap: jobspy deps and daemon registration.

Called by `applypilot repair`, end of `applypilot update`, and end of `applypilot init`.
Safe to run multiple times.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _tool_python() -> str | None:
    r = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    tool_dir = r.stdout.strip()
    if sys.platform == "win32":
        py = Path(tool_dir) / "applypilot" / "Scripts" / "python.exe"
    else:
        py = Path(tool_dir) / "applypilot" / "bin" / "python"
    return str(py) if py.exists() else None


def install_jobspy(console) -> bool:
    """Install jobspy into the tool venv. Returns True on success."""
    py = _tool_python()
    if not py:
        console.print("  [yellow]⚠ Cannot find tool venv Python — skipping jobspy install[/yellow]")
        return False

    # Skip if already installed
    check = subprocess.run([py, "-c", "import jobspy"], capture_output=True)
    if check.returncode == 0:
        console.print("  [green]✓ python-jobspy (already installed)[/green]")
        return True

    console.print("  Installing discovery dependencies...")
    r1 = subprocess.run([py, "-m", "pip", "install", "--no-deps", "python-jobspy"])
    r2 = subprocess.run([py, "-m", "pip", "install", "pydantic", "tls-client", "requests", "markdownify", "regex"])

    verify = subprocess.run([py, "-c", "import jobspy"], capture_output=True)
    if verify.returncode == 0:
        console.print("  [green]✓ python-jobspy[/green]")
        return True
    else:
        console.print("  [red]✗ jobspy install failed — discovery will be limited to GitHub README sources[/red]")
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
        console.print("  [green]✓ Daemon registered (runs at 08:00 and 20:00 daily)[/green]")
        return True
    else:
        console.print("  [yellow]⚠ Daemon registration failed[/yellow]")
        return False


def _register_daemon_macos(console, app_dir: Path) -> bool:
    import applypilot as _ap
    plist_template = Path(_ap.__file__).parent / "scripts" / "launchagents" / "com.applypilot.apply.plist.template"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_dest = launch_agents / "com.applypilot.apply.plist"

    if not plist_template.exists():
        console.print("  [yellow]⚠ plist template not found in package[/yellow]")
        return False

    launch_agents.mkdir(parents=True, exist_ok=True)
    content = plist_template.read_text().replace("__HOME__", str(Path.home()))
    plist_dest.write_text(content)

    subprocess.run(["launchctl", "unload", str(plist_dest)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_dest)], capture_output=True)

    r = subprocess.run(["launchctl", "list", "com.applypilot.apply"], capture_output=True)
    if r.returncode == 0:
        console.print("  [green]✓ Daemon loaded (runs at 08:00 and 20:00 daily)[/green]")
        return True
    else:
        console.print("  [yellow]⚠ Daemon not loaded — check Console.app for errors[/yellow]")
        return False
