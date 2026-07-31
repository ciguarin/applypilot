"""ApplyPilot CLI: the main entry point."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__
from applypilot.config import LOG_DIR

# Every logger.exception()/log.info() call in the package (24+ call sites)
# went to stderr only, which isn't captured for background daemon/worker
# runs -- crashes in code paths like the deterministic Workday fast path
# were silently discarded with no trace anywhere on disk. A file handler
# fixes this for every current and future call site at once, instead of
# patching each one individually with its own add_event() call.
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_DIR / "applypilot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
    ],
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
# Force plain-text mode over SSH / non-TTY (avoids Rich's Win32 console renderer
# which crashes on Unicode when stdout is a pipe rather than a real console).
from applypilot.config import env_key_set, make_console
console = make_console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot: AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    reset_stuck: bool = typer.Option(False, "--reset-stuck", help="Clear jobs stuck 'in_progress' from a crashed or killed run."),
    sweep_inbox: bool = typer.Option(False, "--sweep-inbox", help="Archive stray OTP/verification emails from the inbox and exit."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if reset_stuck:
        from applypilot.apply.launcher import reset_stuck_jobs
        count = reset_stuck_jobs()
        console.print(f"[green]Reset {count} stuck job(s).[/green]")
        return

    if sweep_inbox:
        from applypilot.apply.email_verify import sweep_verification_emails
        swept = sweep_verification_emails()
        console.print(f"[green]Archived {len(swept)} verification email(s).[/green]")
        for m in swept:
            console.print(f"  [dim]{m['subject'][:60]} | {m['from'][:40]}[/dim]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status(
    by_platform: bool = typer.Option(False, "--by-platform", help="Break down the apply-ready queue by ATS platform (Workday, Greenhouse, LinkedIn, etc.)."),
) -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats
    from applypilot.apply.history import count_applications

    stats = get_stats()
    # jobs.applied_at is disposable working state -- a DB reset during
    # testing can erase it while the actual applications it recorded still
    # happened. applications_log.jsonl is the durable record of what was
    # actually confirmed, so it's the number reported here.
    stats["applied"] = count_applications()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    # ATS platform breakdown (opt-in -- classifying every row is only worth
    # the query when someone's actually asking "what's actually applyable
    # here", e.g. deciding whether the Workday fast path is worth building
    # out further, or whether the queue is mostly a platform we can't drive
    # deterministically yet)
    if by_platform:
        from collections import Counter
        from applypilot.database import get_connection as _get_conn
        from applypilot.apply.platforms import classify_platform

        conn = _get_conn()
        rows = conn.execute(
            "SELECT application_url, url FROM jobs "
            "WHERE tailored_resume_path IS NOT NULL "
            "AND (apply_status IS NULL OR apply_status = 'failed')"
        ).fetchall()
        counts = Counter(classify_platform(row[0] or row[1]) for row in rows)

        platform_table = Table(title="\nApply-Ready Queue by Platform", show_header=True, header_style="bold blue")
        platform_table.add_column("Platform")
        platform_table.add_column("Count", justify="right")
        for platform, count in counts.most_common():
            platform_table.add_row(platform, str(count))
        console.print(platform_table)

    # Manual apply queue
    from applypilot.database import get_connection
    conn = get_connection()
    manual_rows = conn.execute("""
        SELECT title, url, application_url, apply_status, fit_score
        FROM jobs
        WHERE apply_status IN ('skip', 'manual', 'failed')
          AND tailored_resume_path IS NOT NULL
        ORDER BY fit_score DESC, title
    """).fetchall()
    if manual_rows:
        manual_table = Table(title=f"\nManual Apply Queue ({len(manual_rows)} jobs, resume tailored, apply yourself)",
                             show_header=True, header_style="bold red")
        manual_table.add_column("Score", width=5, justify="center")
        manual_table.add_column("Job")
        manual_table.add_column("Reason", width=10)
        manual_table.add_column("URL")
        for row in manual_rows:
            apply_url = row[2] or row[1] or ""
            score = str(row[4]) if row[4] else "?"
            status = row[3] or ""
            if "linkedin.com" in apply_url:
                reason = "LinkedIn"
            elif status == "failed":
                reason = "failed"
            else:
                reason = "manual"
            manual_table.add_row(f"{score}/10", row[0] or "", reason, apply_url[:70])
        console.print(manual_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from pathlib import Path
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found. Plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config. Run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError as e:
        results.append(("python-jobspy", warn_mark, f"not importable: {e}. Run: applypilot repair"))

    # --- Tier 2 checks ---
    import os
    has_gemini = env_key_set("GEMINI_API_KEY")
    has_openai = env_key_set("OPENAI_API_KEY")
    has_local = env_key_set("LLM_URL")
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    if env_key_set("CAPSOLVER_API_KEY"):
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Daemon status ---
    from applypilot.bootstrap import daemon_status
    dstat = daemon_status()
    if not dstat["registered"]:
        results.append(("Daemon", fail_mark, dstat["detail"]))
    elif not dstat["enabled"]:
        results.append(("Daemon", warn_mark, f"{dstat['detail']}. Run: applypilot daemon enable"))
    else:
        results.append(("Daemon", ok_mark, f"{dstat['detail']}. Next: {dstat['next_run']} / last: {dstat['last_run']}"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} ({TIER_LABELS[tier]})[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


@app.command()
def repair() -> None:
    """Re-run post-install setup: discovery deps and daemon registration."""
    from applypilot.bootstrap import install_jobspy, install_playwright, register_daemon

    console.print("\n[bold]ApplyPilot Repair[/bold]\n")
    install_jobspy(console)
    install_playwright(console)
    register_daemon(console)
    console.print()


@app.command()
def update() -> None:
    """Pull the latest version from GitHub and reinstall."""
    import subprocess, sys, importlib.metadata as _meta
    # Detect editable install: dist-info will have a direct_url.json with "editable": true
    try:
        import importlib.metadata as _m
        dist = _m.distribution("applypilot")
        direct_url = dist.read_text("direct_url.json")
        if direct_url and '"editable":true' in direct_url.replace(" ", ""):
            console.print("[yellow]Dev install detected. You're running an editable install.[/yellow]")
            console.print("[dim]Your changes are already live. Push to git to publish; don't run update.[/dim]")
            raise typer.Exit(0)
    except _m.PackageNotFoundError:
        pass
    console.print("[bold]Updating ApplyPilot...[/bold]")

    if sys.platform == "win32":
        # Over SSH (no TTY) we can update inline. The exe lock only applies to
        # interactive terminals where the user's shell holds the file open.
        # In a TTY session, spawn a detached console so the lock clears when the
        # current terminal exits.
        is_tty = sys.stdin.isatty()
        if is_tty:
            script = (
                "$exe = (Get-Command applypilot).Source; "
                "Write-Host 'Waiting for applypilot to release its file lock...'; "
                "while ($true) { "
                "  try { $s = [IO.File]::Open($exe,'Open','Read','None'); $s.Close(); break } "
                "  catch { Start-Sleep 1 } "
                "}; "
                "Write-Host 'Installing update...'; "
                "uv tool install git+https://github.com/ciguarin/applypilot@main --force --refresh; "
                "Write-Host 'Running post-install repair...'; "
                "applypilot repair; "
                "Write-Host ''; Write-Host 'Done, close this window.'; "
                "Read-Host"
            )
            ps = "pwsh.exe" if subprocess.run(["where", "pwsh"], capture_output=True).returncode == 0 else "powershell.exe"
            subprocess.Popen([ps, "-NoProfile", "-Command", script], creationflags=0x00000010)  # CREATE_NEW_CONSOLE
            console.print("[green]Updater launched in a new window.[/green]")
            console.print("[dim]Close this terminal. The updater will complete once the lock clears.[/dim]")
            raise typer.Exit(0)
        # Non-TTY (SSH): update inline, exe lock is not held by this session

    result = subprocess.run(
        ["uv", "tool", "install", "git+https://github.com/ciguarin/applypilot@main", "--force", "--refresh"],
        capture_output=False,
    )
    if result.returncode != 0:
        console.print("[red]Update failed.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Updated to latest v1.[/green]")
    from applypilot.bootstrap import install_jobspy, install_playwright, register_daemon
    install_jobspy(console)
    install_playwright(console)
    register_daemon(console)
    console.print("[dim]Restart your terminal or run 'hash -r' to pick up any changes.[/dim]")


daemon_app = typer.Typer(help="Control the scheduled background daemon (Task Scheduler / LaunchAgent).")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("status")
def daemon_status_cmd() -> None:
    """Show whether the daemon is registered, enabled, and when it last/next ran."""
    from applypilot.bootstrap import daemon_status as _status

    st = _status()
    console.print("\n[bold]ApplyPilot Daemon[/bold]\n")
    if not st["registered"]:
        console.print(f"  [red]Not registered.[/red] {st['detail']}")
    elif not st["enabled"]:
        console.print(f"  [yellow]Registered but disabled.[/yellow] {st['detail']}")
        console.print("  [dim]Run: applypilot daemon enable[/dim]")
    else:
        console.print(f"  [green]Enabled.[/green] {st['detail']}")
        console.print(f"  Next run: {st['next_run']}")
        console.print(f"  Last run: {st['last_run']}")
    console.print()


@daemon_app.command("enable")
def daemon_enable_cmd() -> None:
    """Enable the scheduled daemon (registers it fresh if it's missing entirely)."""
    from applypilot.bootstrap import enable_daemon

    if enable_daemon():
        console.print("[green]Daemon enabled.[/green]")
    else:
        console.print("[red]Failed to enable daemon.[/red] Try: applypilot repair")
        raise typer.Exit(1)


@daemon_app.command("disable")
def daemon_disable_cmd() -> None:
    """Disable the scheduled daemon without removing its registration."""
    from applypilot.bootstrap import disable_daemon

    if disable_daemon():
        console.print("[green]Daemon disabled.[/green] Run 'applypilot daemon enable' to resume.")
    else:
        console.print("[red]Failed to disable daemon.[/red] It may not be registered. Check: applypilot daemon status")
        raise typer.Exit(1)


@daemon_app.command("run-now")
def daemon_run_now_cmd() -> None:
    """Trigger the scheduled daemon's run immediately, outside its normal schedule."""
    from applypilot.bootstrap import run_daemon_now

    if run_daemon_now():
        console.print("[green]Daemon run triggered.[/green] Check ~/.applypilot/logs/apply_daemon.log for progress.")
    else:
        console.print("[red]Failed to trigger daemon run.[/red] Check: applypilot daemon status")
        raise typer.Exit(1)


config_app = typer.Typer(
    help="View and change settings without re-running init.",
    invoke_without_command=True,
)
app.add_typer(config_app, name="config")


@config_app.callback()
def config_main(ctx: typer.Context) -> None:
    """Interactive settings, or run a subcommand directly."""
    if ctx.invoked_subcommand is None:
        from applypilot.config import load_env, ensure_dirs
        from applypilot.config_tui import run_settings_tui
        load_env()
        ensure_dirs()
        run_settings_tui()


@config_app.command("show")
def config_show() -> None:
    """Show all current settings."""
    import json, os
    from applypilot.config import load_env, PROFILE_PATH, SEARCH_CONFIG_PATH, ENV_PATH

    load_env()

    console.print("\n[bold]ApplyPilot Settings[/bold]\n")

    # Profile
    try:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        p = profile.get("personal", {})
        name = f"{p.get('preferred_name', p.get('first_name', '?'))} {p.get('last_name', '')}".strip()
        console.print(f"  [cyan]name[/cyan]          {name}")
        console.print(f"  [cyan]email[/cyan]         {p.get('email', 'not set')}")
        console.print(f"  [cyan]linkedin[/cyan]      {p.get('linkedin_url', 'not set')}")
        console.print(f"  [cyan]github[/cyan]        {p.get('github_url', 'not set')}")
        cities = profile.get("preferred_cities", ["toronto", "remote"])
        console.print(f"  [cyan]cities[/cyan]        {', '.join(cities)}")
    except (FileNotFoundError, json.JSONDecodeError):
        console.print("  [red]profile.json not found. Run applypilot init[/red]")

    console.print()

    # Searches
    try:
        import yaml
        cfg = yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))
        queries = [q["query"] for q in cfg.get("queries", [])]
        defaults = cfg.get("defaults", {})
        console.print(f"  [cyan]queries[/cyan]       {', '.join(queries)}")
        console.print(f"  [cyan]sites[/cyan]         {', '.join(cfg.get('sites', ['indeed', 'linkedin']))}")
        console.print(f"  [cyan]hours_old[/cyan]     {defaults.get('hours_old', 168)}")
        console.print(f"  [cyan]results/site[/cyan]  {defaults.get('results_per_site', 50)}")
    except (FileNotFoundError, Exception):
        console.print("  [red]searches.yaml not found. Run applypilot init[/red]")

    console.print()

    # LLM / API
    model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
    if env_key_set("GEMINI_API_KEY"):
        provider = "gemini"
    elif env_key_set("OPENAI_API_KEY"):
        provider = "openai"
    elif env_key_set("LLM_URL"):
        provider = os.environ.get("LLM_URL")
    else:
        provider = "not set"
    min_score = os.environ.get("APPLYPILOT_MIN_SCORE", "7")
    console.print(f"  [cyan]llm[/cyan]           {provider} / {model}")
    console.print(f"  [cyan]min score[/cyan]     {min_score}")
    console.print()


@config_app.command("cities")
def config_cities() -> None:
    """Change your target cities."""
    import json
    from applypilot.config import PROFILE_PATH, SEARCH_CONFIG_PATH
    from applypilot.wizard.init import CANADIAN_CITIES, _setup_searches

    try:
        profile = json.loads(PROFILE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        console.print("[red]Profile not found. Run applypilot init first.[/red]")
        raise typer.Exit(1)

    current = profile.get("preferred_cities", ["toronto", "remote"])
    console.print(f"\nCurrent cities: [cyan]{', '.join(current)}[/cyan]\n")
    _setup_searches(profile)

    PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    console.print("[green]Cities and search config updated.[/green]")


@config_app.command("queries")
def config_queries() -> None:
    """Change your search queries (job titles)."""
    import yaml
    from applypilot.config import SEARCH_CONFIG_PATH
    from rich.prompt import Prompt

    try:
        cfg = yaml.safe_load(SEARCH_CONFIG_PATH.read_text())
    except FileNotFoundError:
        console.print("[red]searches.yaml not found. Run applypilot init first.[/red]")
        raise typer.Exit(1)

    current = [q["query"] for q in cfg.get("queries", [])]
    console.print(f"\nCurrent queries: [cyan]{', '.join(current)}[/cyan]\n")

    raw = Prompt.ask("New queries (comma-separated)")
    roles = [r.strip() for r in raw.split(",") if r.strip()]
    if not roles:
        console.print("[yellow]No changes made.[/yellow]")
        return

    cfg["queries"] = [{"query": r, "tier": min(i + 1, 3)} for i, r in enumerate(roles)]
    SEARCH_CONFIG_PATH.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
    console.print(f"[green]Queries updated: {', '.join(roles)}[/green]")


@config_app.command("model")
def config_model() -> None:
    """Change the LLM model."""
    import re
    from applypilot.config import ENV_PATH, load_env
    from rich.prompt import Prompt

    load_env()
    import os
    current = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
    console.print(f"\nCurrent model: [cyan]{current}[/cyan]\n")

    model = Prompt.ask("New model (e.g. gemini-2.0-flash, gpt-4o-mini, claude-haiku-4-5-20251001)")
    if not model.strip():
        console.print("[yellow]No changes made.[/yellow]")
        return

    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if "LLM_MODEL=" in env_text:
        env_text = re.sub(r"LLM_MODEL=.*", f"LLM_MODEL={model.strip()}", env_text)
    else:
        env_text += f"\nLLM_MODEL={model.strip()}\n"
    ENV_PATH.write_text(env_text, encoding="utf-8")
    console.print(f"[green]Model updated to {model.strip()}[/green]")


@config_app.command("score")
def config_score() -> None:
    """Set the minimum fit score threshold (persisted to .env)."""
    import re
    from applypilot.config import ENV_PATH, load_env
    from rich.prompt import Prompt

    load_env()
    import os
    current = os.environ.get("APPLYPILOT_MIN_SCORE", "7")
    console.print(f"\nCurrent minimum score: [cyan]{current}[/cyan]\n")

    raw = Prompt.ask("New minimum score (1-10)")
    try:
        threshold = int(raw.strip())
        if not 1 <= threshold <= 10:
            raise ValueError
    except ValueError:
        console.print("[red]Score must be an integer between 1 and 10.[/red]")
        raise typer.Exit(1)

    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if "APPLYPILOT_MIN_SCORE=" in env_text:
        env_text = re.sub(r"APPLYPILOT_MIN_SCORE=.*", f"APPLYPILOT_MIN_SCORE={threshold}", env_text)
    else:
        env_text += f"\nAPPLYPILOT_MIN_SCORE={threshold}\n"
    ENV_PATH.write_text(env_text, encoding="utf-8")
    console.print(f"[green]Min score set to {threshold}[/green]")
    console.print(f"[dim]You can still override per-run: applypilot run --min-score {threshold}[/dim]")


@config_app.command("profile")
def config_profile() -> None:
    """Update your personal info (name, email, phone, LinkedIn, GitHub)."""
    import json
    from applypilot.config import PROFILE_PATH
    from rich.prompt import Prompt

    try:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        console.print("[red]Profile not found. Run applypilot init first.[/red]")
        raise typer.Exit(1)

    p = profile.setdefault("personal", {})

    console.print("\n[bold]Personal Info[/bold] [dim](press Enter to keep current value)[/dim]\n")

    fields = [
        ("preferred_name",  "Preferred name",  p.get("preferred_name", "")),
        ("email",           "Email",            p.get("email", "")),
        ("phone",           "Phone",            p.get("phone", "")),
        ("city",            "City",             p.get("city", "")),
        ("linkedin_url",    "LinkedIn URL",     p.get("linkedin_url", "")),
        ("github_url",      "GitHub URL",       p.get("github_url", "")),
        ("portfolio_url",   "Portfolio URL",    p.get("portfolio_url", "")),
    ]

    changed = False
    for key, label, current in fields:
        display = f"[cyan]{current}[/cyan]" if current else "[dim]not set[/dim]"
        val = Prompt.ask(f"  {label} ({display})", default=current)
        if val != current:
            p[key] = val
            changed = True

    # Universal job-site password
    console.print("\n[bold]Job Site Password[/bold] [dim](used when creating/logging into ATS accounts)[/dim]\n")
    current_pw = p.get("password", "")
    display_pw = "[cyan]****[/cyan]" if current_pw else "[dim]not set[/dim]"
    new_pw = Prompt.ask(f"  Password ({display_pw})", default=current_pw, password=True)
    if new_pw != current_pw:
        p["password"] = new_pw
        changed = True

    # Google SSO credentials
    console.print("\n[bold]Google Account[/bold] [dim](for sites configured to use Google SSO login)[/dim]\n")
    g = profile.setdefault("google_account", {})
    current_gemail = g.get("email", "")
    display_gemail = f"[cyan]{current_gemail}[/cyan]" if current_gemail else "[dim]not set[/dim]"
    new_gemail = Prompt.ask(f"  Google email ({display_gemail})", default=current_gemail)
    if new_gemail != current_gemail:
        g["email"] = new_gemail
        changed = True

    current_gpw = g.get("password", "")
    display_gpw = "[cyan]****[/cyan]" if current_gpw else "[dim]not set[/dim]"
    new_gpw = Prompt.ask(f"  Google password ({display_gpw})", default=current_gpw, password=True)
    if new_gpw != current_gpw:
        g["password"] = new_gpw
        changed = True

    if changed:
        PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print("\n[green]Profile updated.[/green]")
    else:
        console.print("\n[dim]No changes made.[/dim]")


@config_app.command("resume")
def config_resume() -> None:
    """Open your resume for editing."""
    import os, subprocess
    from applypilot.config import APP_DIR

    resume_path = APP_DIR / "resume.txt"
    if not resume_path.exists():
        console.print("[red]resume.txt not found. Run applypilot init first.[/red]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "nano")
    console.print(f"Opening resume in [cyan]{editor}[/cyan]...")
    subprocess.run([editor, str(resume_path)])
    console.print("[green]Resume saved.[/green]")


@config_app.command("searches")
def config_searches() -> None:
    """Update search settings (hours lookback, results per site, sites)."""
    import yaml, re
    from applypilot.config import SEARCH_CONFIG_PATH
    from rich.prompt import Prompt

    try:
        cfg = yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        console.print("[red]searches.yaml not found. Run applypilot init first.[/red]")
        raise typer.Exit(1)

    defaults = cfg.setdefault("defaults", {})
    console.print("\n[bold]Search Settings[/bold] [dim](press Enter to keep current value)[/dim]\n")

    # hours_old
    current_hours = str(defaults.get("hours_old", 168))
    raw = Prompt.ask(f"  Hours lookback [cyan]{current_hours}[/cyan] (168 = 1 week)")
    if raw.strip() and raw.strip() != current_hours:
        try:
            defaults["hours_old"] = int(raw.strip())
        except ValueError:
            console.print("[yellow]Invalid number, keeping current.[/yellow]")

    # results_per_site
    current_rps = str(defaults.get("results_per_site", 50))
    raw = Prompt.ask(f"  Results per site [cyan]{current_rps}[/cyan]")
    if raw.strip() and raw.strip() != current_rps:
        try:
            defaults["results_per_site"] = int(raw.strip())
        except ValueError:
            console.print("[yellow]Invalid number, keeping current.[/yellow]")

    # sites
    current_sites = ", ".join(cfg.get("sites", ["indeed", "linkedin"]))
    raw = Prompt.ask(f"  Sites [cyan]{current_sites}[/cyan] (comma-separated: indeed, linkedin, glassdoor)")
    if raw.strip() and raw.strip() != current_sites:
        cfg["sites"] = [s.strip() for s in raw.split(",") if s.strip()]

    SEARCH_CONFIG_PATH.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    console.print("\n[green]Search settings updated.[/green]")


@config_app.command("api")
def config_api() -> None:
    """Update your API keys and LLM provider."""
    import re, os
    from applypilot.config import ENV_PATH, load_env
    from rich.prompt import Prompt

    load_env()
    console.print("\n[bold]API Keys[/bold] [dim](press Enter to keep current value)[/dim]\n")

    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""

    def _get_env(key: str) -> str:
        return os.environ.get(key, "")

    def _update_env(text: str, key: str, val: str) -> str:
        if f"{key}=" in text:
            return re.sub(rf"{key}=.*", f"{key}={val}", text)
        return text + f"\n{key}={val}\n"

    fields = [
        ("GEMINI_API_KEY",  "Gemini API key  (gemini.google.com)"),
        ("OPENAI_API_KEY",  "OpenAI API key  (platform.openai.com)"),
        ("LLM_URL",         "OpenRouter/custom base URL  (e.g. https://openrouter.ai/api/v1)"),
        ("LLM_API_KEY",     "API key for custom URL"),
        ("LLM_MODEL",       "Model override  (e.g. google/gemini-2.5-flash-lite)"),
        ("CAPSOLVER_API_KEY", "CapSolver key  (for CAPTCHA solving during apply)"),
    ]

    changed = False
    for key, label in fields:
        current = _get_env(key)
        masked = f"{current[:6]}…" if len(current) > 8 else ("[dim]not set[/dim]" if not current else current)
        val = Prompt.ask(f"  {label} ({masked})", default=current, password=("KEY" in key or "key" in key.lower()))
        if val != current:
            env_text = _update_env(env_text, key, val)
            changed = True

    if changed:
        ENV_PATH.write_text(env_text, encoding="utf-8")
        console.print("\n[green]API keys updated.[/green]")
    else:
        console.print("\n[dim]No changes made.[/dim]")


@config_app.command("blocked")
def config_blocked(
    add_site: Optional[str] = typer.Option(None, "--add-site", help="Block a site by its `site` column value (e.g. 'linkedin')."),
    add_pattern: Optional[str] = typer.Option(None, "--add-pattern", help="Block URLs matching a SQL LIKE pattern (e.g. '%example.com%')."),
    remove_site: Optional[str] = typer.Option(None, "--remove-site", help="Unblock a previously user-added site."),
    remove_pattern: Optional[str] = typer.Option(None, "--remove-pattern", help="Unblock a previously user-added URL pattern."),
) -> None:
    """View or edit blocked sites/URL patterns for the apply stage.

    Package-shipped defaults (glassdoor, LinkedIn, etc.) live in the
    installed package and can't be edited here -- only removed sites/patterns
    you've added yourself can be removed. Your additions are stored in
    ~/.applypilot/blocked_sites.yaml, separate from the package config, so
    they survive `applypilot update`.
    """
    from applypilot.config import load_sites_config, load_user_blocked_sites, save_user_blocked_sites

    user_blocked = load_user_blocked_sites()
    user_sites = list(user_blocked["sites"])
    user_patterns = list(user_blocked["url_patterns"])
    changed = False

    if add_site:
        if add_site not in user_sites:
            user_sites.append(add_site)
            changed = True
            console.print(f"[green]Added site to blocklist: {add_site}[/green]")
        else:
            console.print(f"[dim]Already blocked: {add_site}[/dim]")

    if add_pattern:
        if add_pattern not in user_patterns:
            user_patterns.append(add_pattern)
            changed = True
            console.print(f"[green]Added URL pattern to blocklist: {add_pattern}[/green]")
        else:
            console.print(f"[dim]Already blocked: {add_pattern}[/dim]")

    if remove_site:
        if remove_site in user_sites:
            user_sites.remove(remove_site)
            changed = True
            console.print(f"[green]Removed site from blocklist: {remove_site}[/green]")
        else:
            console.print(f"[yellow]Not in your blocklist (may be a package default -- can't remove those): {remove_site}[/yellow]")

    if remove_pattern:
        if remove_pattern in user_patterns:
            user_patterns.remove(remove_pattern)
            changed = True
            console.print(f"[green]Removed URL pattern from blocklist: {remove_pattern}[/green]")
        else:
            console.print(f"[yellow]Not in your blocklist (may be a package default -- can't remove those): {remove_pattern}[/yellow]")

    if changed:
        save_user_blocked_sites(user_sites, user_patterns)

    # Always show the full effective list (package defaults + user additions)
    pkg_blocked = load_sites_config().get("blocked", {})
    console.print("\n[bold]Package defaults[/bold] [dim](edit via the repo, not here)[/dim]")
    for s in pkg_blocked.get("sites", []):
        console.print(f"  site     {s}")
    for p in pkg_blocked.get("url_patterns", []):
        console.print(f"  pattern  {p}")

    console.print("\n[bold]Your additions[/bold] [dim](~/.applypilot/blocked_sites.yaml)[/dim]")
    if not user_sites and not user_patterns:
        console.print("  [dim]none[/dim]")
    for s in user_sites:
        console.print(f"  site     {s}")
    for p in user_patterns:
        console.print(f"  pattern  {p}")
    console.print()


@config_app.command("sources")
def config_sources(
    add: Optional[str] = typer.Option(None, "--add", help="Add a source by 'owner/repo'. Uses the generic parser."),
    name: Optional[str] = typer.Option(None, "--name", help="Display name for --add (defaults to the repo path)."),
    path: str = typer.Option("README.md", "--path", help="File to poll within the repo, for --add."),
    branch: str = typer.Option("main", "--branch", help="Branch to poll, for --add."),
    remove: Optional[str] = typer.Option(None, "--remove", help="Remove a previously user-added source by its key."),
    enable: Optional[str] = typer.Option(None, "--enable", help="Enable a source (built-in or user-added) by key."),
    disable: Optional[str] = typer.Option(None, "--disable", help="Disable a source (built-in or user-added) by key."),
) -> None:
    """View or edit GitHub README discovery sources.

    The two built-in sources (negarprh, hanzili) use bespoke parsers tailored
    to each README's exact format. Sources you add yourself go through a
    generic parser that auto-detects columns from the header row -- works for
    most curated internship-list READMEs (a markdown table with company/
    title/location/apply columns in some order), but won't handle a genuinely
    unusual format the way a bespoke parser would.
    """
    import re as _re

    from applypilot.config import load_user_github_sources, save_user_github_sources
    from applypilot.discovery.github_readme import DEFAULT_SOURCES, source_urls_from_repo, _fetch

    user_cfg = load_user_github_sources()
    builtin_disabled = list(user_cfg["builtin_disabled"])
    user_sources = list(user_cfg["user_sources"])
    builtin_keys = {s["key"] for s in DEFAULT_SOURCES}
    user_keys = {s["key"] for s in user_sources}
    changed = False

    if add:
        key = _re.sub(r"[^a-z0-9]+", "-", add.lower()).strip("-")
        if key in builtin_keys or key in user_keys:
            console.print(f"[yellow]Already have a source with key '{key}'.[/yellow]")
        else:
            readme_url, _ = source_urls_from_repo(add, path, branch)
            if _fetch(readme_url) is None:
                console.print(f"[yellow]Warning: couldn't fetch {readme_url} -- adding anyway, check the repo/path/branch.[/yellow]")
            user_sources.append({
                "key": key, "name": name or add, "repo": add,
                "path": path, "branch": branch, "enabled": True,
            })
            changed = True
            console.print(f"[green]Added source '{key}' ({add}), generic parser.[/green]")

    if remove:
        if remove in user_keys:
            user_sources = [s for s in user_sources if s["key"] != remove]
            changed = True
            console.print(f"[green]Removed source: {remove}[/green]")
        elif remove in builtin_keys:
            console.print(f"[yellow]'{remove}' is a built-in source -- use --disable instead of --remove.[/yellow]")
        else:
            console.print(f"[yellow]No source with key: {remove}[/yellow]")

    for toggle_key, want_enabled in ((enable, True), (disable, False)):
        if not toggle_key:
            continue
        if toggle_key in builtin_keys:
            if want_enabled:
                builtin_disabled = [k for k in builtin_disabled if k != toggle_key]
            elif toggle_key not in builtin_disabled:
                builtin_disabled.append(toggle_key)
            changed = True
            console.print(f"[green]{'Enabled' if want_enabled else 'Disabled'}: {toggle_key}[/green]")
        elif toggle_key in user_keys:
            for s in user_sources:
                if s["key"] == toggle_key:
                    s["enabled"] = want_enabled
            changed = True
            console.print(f"[green]{'Enabled' if want_enabled else 'Disabled'}: {toggle_key}[/green]")
        else:
            console.print(f"[yellow]No source with key: {toggle_key}[/yellow]")

    if changed:
        save_user_github_sources(builtin_disabled, user_sources)

    console.print("\n[bold]Built-in[/bold] [dim](tailored parsers)[/dim]")
    for s in DEFAULT_SOURCES:
        state = "[dim]disabled[/dim]" if s["key"] in builtin_disabled else "[green]enabled[/green]"
        console.print(f"  {state:20} {s['key']:20} {s['name']}")

    console.print("\n[bold]Your additions[/bold] [dim](~/.applypilot/github_sources.yaml, generic parser)[/dim]")
    if not user_sources:
        console.print("  [dim]none[/dim]")
    for s in user_sources:
        state = "[green]enabled[/green]" if s.get("enabled", True) else "[dim]disabled[/dim]"
        console.print(f"  {state:20} {s['key']:20} {s['name']} [dim]({s['repo']})[/dim]")
    console.print()


if __name__ == "__main__":
    app()
