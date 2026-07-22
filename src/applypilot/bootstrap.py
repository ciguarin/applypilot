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
    # Registering the task can use pwsh if available -- that's just a normal
    # subprocess call, made right now, in a real interactive context.
    ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
    # But the task's own -Execute target must be Windows PowerShell 5.1, not pwsh:
    # pwsh.exe is known to hang indefinitely with zero output when launched BY Task
    # Scheduler on some Windows 11 machines (it tries to route through the Windows
    # Terminal host with no interactive session available to host it). powershell.exe
    # predates that integration and doesn't hit it -- apply_daemon.ps1 has no PS7-only
    # syntax, so 5.1 is a safe target. -WindowStyle Hidden is also omitted below --
    # it's one of the reported triggers for the same hang class.
    task_ps = shutil.which("powershell") or "powershell.exe"
    d = str(app_dir).replace("'", "''")
    script = f"""
$D = '{d}'
$ps = '{task_ps}'
$action = New-ScheduledTaskAction `
    -Execute $ps `
    -Argument "-NonInteractive -File `"$D\\apply_daemon.ps1`"" `
    -WorkingDirectory $D
$trigger1 = New-ScheduledTaskTrigger -Daily -At "08:00"
$trigger2 = New-ScheduledTaskTrigger -Daily -At "20:00"
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -WakeToRun
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


def daemon_status() -> dict:
    """Query the scheduled daemon's current state, cross-platform.

    Returns:
        {"registered": bool, "enabled": bool, "next_run": str, "last_run": str, "detail": str}
    """
    if sys.platform == "win32":
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", "ApplyPilot.Apply", "/FO", "LIST", "/V"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"registered": False, "enabled": False, "next_run": "", "last_run": "",
                    "detail": "Not in Task Scheduler — re-run the installer or `applypilot repair`"}

        def _field(name: str, default: str) -> str:
            return next(
                (line.split(":", 1)[1].strip() for line in r.stdout.splitlines() if line.strip().startswith(name)),
                default,
            )

        state = _field("Scheduled Task State", "Unknown")
        enabled = state.lower() == "enabled"
        return {
            "registered": True,
            "enabled": enabled,
            "next_run": _field("Next Run Time", "scheduled"),
            "last_run": _field("Last Run Time", "never"),
            "detail": f"Task Scheduler — state: {state}",
        }

    elif sys.platform == "darwin":
        r = subprocess.run(["launchctl", "list", "com.applypilot.apply"], capture_output=True, text=True)
        plist = Path.home() / "Library/LaunchAgents/com.applypilot.apply.plist"
        if r.returncode == 0:
            return {"registered": True, "enabled": True, "next_run": "08:00 / 20:00 daily", "last_run": "",
                    "detail": "LaunchAgent loaded"}
        elif plist.exists():
            return {"registered": True, "enabled": False, "next_run": "", "last_run": "",
                    "detail": f"plist exists but not loaded — run: applypilot daemon enable"}
        else:
            return {"registered": False, "enabled": False, "next_run": "", "last_run": "",
                    "detail": "Not installed — re-run the installer"}

    return {"registered": False, "enabled": False, "next_run": "", "last_run": "",
            "detail": "Daemon control not supported on this platform"}


def enable_daemon() -> bool:
    """Enable (or re-register, if missing) the scheduled daemon."""
    if sys.platform == "win32":
        r = subprocess.run(["schtasks", "/Change", "/TN", "ApplyPilot.Apply", "/ENABLE"], capture_output=True)
        if r.returncode == 0:
            return True
        # Not registered at all yet -- register() ends the task enabled by default.
        return register_daemon(_NullConsole())
    elif sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.applypilot.apply.plist"
        if not plist.exists():
            return register_daemon(_NullConsole())
        r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
        return r.returncode == 0
    return False


def disable_daemon() -> bool:
    """Disable the scheduled daemon without removing its registration."""
    if sys.platform == "win32":
        r = subprocess.run(["schtasks", "/Change", "/TN", "ApplyPilot.Apply", "/DISABLE"], capture_output=True)
        return r.returncode == 0
    elif sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.applypilot.apply.plist"
        if not plist.exists():
            return False
        r = subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        return r.returncode == 0
    return False


def run_daemon_now() -> bool:
    """Trigger the scheduled daemon's task immediately, out of band from its schedule."""
    if sys.platform == "win32":
        r = subprocess.run(["schtasks", "/Run", "/TN", "ApplyPilot.Apply"], capture_output=True)
        return r.returncode == 0
    elif sys.platform == "darwin":
        r = subprocess.run(["launchctl", "start", "com.applypilot.apply"], capture_output=True)
        return r.returncode == 0
    return False


class _NullConsole:
    """Swallow console.print() calls from register_daemon() when called
    from a non-interactive context (enable_daemon() re-registering a
    missing task) where a Rich console isn't available."""
    def print(self, *args, **kwargs) -> None:
        pass


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
