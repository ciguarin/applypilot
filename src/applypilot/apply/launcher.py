"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)
from applypilot.apply.platforms import workday as workday_mod

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

# Pinned exact versions for both `npx -y`-fetched MCP servers -- unpinned
# `npx -y @pkg/name` re-resolves to whatever is currently published on every
# single agent invocation. For @playwright/mcp (Microsoft-maintained) that's
# mostly a reproducibility concern; for @codefuturist/email-mcp (a small,
# single-maintainer third-party package that receives real IMAP/SMTP
# credentials via env vars) it's a real supply-chain exposure -- an update
# could ship different code with no review before it's given the user's
# email password. Bump deliberately, not implicitly.
_PLAYWRIGHT_MCP_VERSION = "0.0.78"
_EMAIL_MCP_VERSION = "0.2.3"


def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    config.load_env()
    mcp: dict = {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "-y",
                    f"@playwright/mcp@{_PLAYWRIGHT_MCP_VERSION}",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
        }
    }
    # IMAP login must be the Apple ID, not a custom-domain alias -- iCloud's
    # IMAP server rejects the alias even though mail addressed to it lands
    # in the same inbox (see apply/email_verify.py for the same fix).
    email_login = os.environ.get("EMAIL_IMAP_USERNAME") or os.environ.get("EMAIL_ADDRESS", "")
    email_pass = os.environ.get("EMAIL_PASSWORD", "")
    if email_login and email_pass:
        imap_host = os.environ.get("EMAIL_IMAP_HOST", "imap.mail.me.com")
        smtp_host = os.environ.get("EMAIL_SMTP_HOST", "smtp.mail.me.com")
        mcp["mcpServers"]["email"] = {
            "command": "npx",
            "args": ["-y", f"@codefuturist/email-mcp@{_EMAIL_MCP_VERSION}"],
            "env": {
                "MCP_EMAIL_ADDRESS":      email_login,
                "MCP_EMAIL_PASSWORD":     email_pass,
                "MCP_EMAIL_IMAP_HOST":    imap_host,
                "MCP_EMAIL_SMTP_HOST":    smtp_host,
                "MCP_EMAIL_ACCOUNT_NAME": "default",
                "MCP_EMAIL_READ_ONLY":    "false",
            },
        }
    return mcp


def resolve_apply_url(job: dict) -> str:
    """Resolve the job's application URL against its base URL.

    application_url from discovery is often a relative path or bare
    fragment (e.g. "/company/id/application" or "#job-form") -- not a
    navigable URL on its own.
    """
    app_url = job.get("application_url")
    base_url = job["url"]
    if not app_url:
        return base_url
    if app_url.startswith("http://") or app_url.startswith("https://"):
        return app_url
    return urljoin(base_url, app_url)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    current_signature = capability_signature()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            # Exact match first. The old query OR'd this together with a
            # query-string-stripped LIKE fallback in one shot -- fine for
            # path-identified ATS URLs, but for query-string-identified ones
            # (e.g. Indeed's ?jk=<id>) stripping the query string turns the
            # LIKE into "any job on this host+path", and since SQL doesn't
            # prioritize OR branches, a real bug: a live retry against a
            # specific ?jk=<id> silently matched and re-applied to a
            # different, unrelated job at the same host instead (confirmed
            # live -- burned two agent runs re-applying to an already-known
            # -expired job while the intended job sat untouched).
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path,
                       apply_attempts
                FROM jobs
                WHERE (url = ? OR application_url = ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                LIMIT 1
            """, (target_url, target_url)).fetchone()
            if row is None:
                like = f"%{target_url.split('?')[0].rstrip('/')}%"
                row = conn.execute("""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path,
                           apply_attempts
                    FROM jobs
                    WHERE (application_url LIKE ? OR url LIKE ?)
                      AND tailored_resume_path IS NOT NULL
                      AND (apply_status IS NULL OR apply_status != 'in_progress')
                    LIMIT 1
                """, (like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            # Internship postings older than this are almost always closed by the
            # time an agent gets to them (observed: 55% of apply attempts on
            # untouched backlog were dead listings, avg. 16 days stale) -- matches
            # the existing 14-day TTL convention used for unscored-job pruning.
            stale_cutoff = (
                datetime.now(timezone.utc) - timedelta(days=14)
            ).isoformat()
            params: list = [
                config.DEFAULTS["max_apply_attempts"], current_signature, min_score, stale_cutoff,
            ]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                # Check the real apply destination (application_url, falling
                # back to url), not just the source/tracker link -- a job
                # sourced from a non-blocked aggregator (e.g. a GitHub-listed
                # internship) can still resolve to a blocked ATS like
                # LinkedIn once enriched, and that must be caught too.
                url_clauses = " ".join(
                    "AND COALESCE(NULLIF(application_url, ''), url) NOT LIKE ?"
                    for _ in blocked_patterns
                )
                params.extend(blocked_patterns)
            row = conn.execute(f"""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path,
                       apply_attempts
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status = 'failed')
                  AND (
                    apply_attempts IS NULL
                    OR apply_attempts < ?
                    OR apply_failed_signature IS NULL
                    OR apply_failed_signature != ?
                  )
                  AND fit_score >= ?
                  AND discovered_at >= ?
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, discovered_at DESC
                LIMIT 1
            """, params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = resolve_apply_url(dict(row))
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        # A job only reaches here with attempts >= max via the signature-
        # mismatch path (conditions changed since it was marked permanent)
        # -- give it a full fresh retry budget rather than leaving it at 99,
        # so normal retry-counting behaves sanely if it fails again for a
        # genuinely different (non-permanent) reason next time.
        was_permanently_stuck = (
            row["apply_attempts"] is not None
            and row["apply_attempts"] >= config.DEFAULTS["max_apply_attempts"]
        )
        reset_clause = ", apply_attempts = 0" if was_permanently_stuck else ""
        if was_permanently_stuck:
            logger.info(
                "Re-attempting %s after a permanent failure -- capability signature changed (%s)",
                row["url"][:80], current_signature,
            )
        conn.execute(f"""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
                           {reset_clause}
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           apply_failed_signature = NULL
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           apply_failed_signature = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, capability_signature(), url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_failed_signature = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL,
                           apply_failed_signature = ?
            WHERE url = ?
        """, (reason or "manual", capability_signature(), url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


def reset_stuck_jobs() -> int:
    """Clear 'in_progress' jobs left behind by a crashed or killed run.

    reset_failed() deliberately excludes 'in_progress' (a job actively being
    worked on right now shouldn't be reset out from under a running worker),
    but a job stuck in that state from a *previous* process that died
    ungracefully needs the same recovery. Called automatically at the start
    of main() so a crash never needs manual SQL to recover from -- this used
    to be a raw sqlite3 one-liner duplicated in both platforms' daemon
    scripts.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("UPDATE jobs SET apply_status = NULL WHERE apply_status = 'in_progress'")
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _try_deterministic_fast_path(job: dict, port: int, resume_pdf: Path | None,
                                  worker_id: int, dry_run: bool) -> tuple[str, int] | None:
    """Attempt a no-LLM apply path for platforms we can drive deterministically.

    Returns (status, duration_ms) on a definitive outcome (applied or a real
    failure), or None to fall back to the full Claude Code agent for this job.
    Connects to the same Chrome instance (via CDP) that chrome.py already
    launched for this worker -- no separate browser, no extra process.
    """
    apply_url = resolve_apply_url(job)
    if not workday_mod.is_workday(apply_url):
        return None
    if not resume_pdf or not resume_pdf.exists():
        return None

    from playwright.sync_api import sync_playwright

    start = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            profile = config.load_profile()
            result = workday_mod.apply_via_workday(
                page, job, profile, resume_pdf, dry_run=dry_run,
            )
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] Workday fast-path: {result.status} ({result.reason or 'ok'})")
        return result.status, duration_ms
    except workday_mod.NeedsAgent as e:
        logger.info("[workday] fast-path bailed, falling back to agent: %s", e.reason)
        add_event(f"[W{worker_id}] Workday fast-path bailed: {e.reason[:50]}")
        return None
    except Exception:
        logger.exception("[workday] fast-path crashed, falling back to agent")
        return None


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
    """Apply to one job -- deterministic fast path first, Claude Code agent as fallback.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    resume_pdf_path = Path(resume_path).with_suffix(".pdf") if resume_path else None
    fast_result = _try_deterministic_fast_path(job, port, resume_pdf_path, worker_id, dry_run)
    if fast_result is not None:
        return fast_result

    # Build the prompt (inject worker_id so prompt can use worker-local file paths)
    job["_worker_id"] = worker_id
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        # ToolSearch is NOT blocked here on purpose (confirmed live via real
        # transcripts): this CLI version defers MCP tool schemas behind
        # ToolSearch, and when a session gets deferred, blocking ToolSearch
        # makes the browser/email tools permanently uncallable for that run
        # -- the exact "I don't have access to browser automation tools"
        # failure seen repeatedly across jobs. Blocking it adds no safety
        # margin anyway: Bash and the dangerous email actions are already
        # blocked by name below, so ToolSearch can't be used to reach
        # anything that isn't already gated.
        "--disallowedTools", "Bash,mcp__email__list_accounts,mcp__email__send_email,mcp__email__delete_email,mcp__email__create_draft",
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    worker_dir = reset_worker_dir(worker_id)

    # Copy resume/cover letter into worker dir so Playwright MCP can access them
    import shutil as _shutil
    for file_key in ("tailored_resume_path", "cover_letter_path"):
        src = job.get(file_key, "")
        if src:
            for ext in (".pdf", ".txt"):
                p = Path(src).with_suffix(ext)
                if p.exists():
                    _shutil.copy(str(p), str(worker_dir / p.name))

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {resolve_apply_url(job)}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8", buffering=1) as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__email__", "email:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        if "you've hit your session limit" in output.lower() or "hit your session limit" in output.lower():
            add_event(f"[W{worker_id}] SESSION LIMIT — stopping")
            update_state(worker_id, status="session_limit", last_action="Claude session limit hit")
            return "session_limit", duration_ms

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        # Match RESULT:APPLIED and RESULT: APPLIED (with or without a space
        # after the colon) -- model phrasing isn't perfectly deterministic,
        # and a strict no-space match previously mislabeled a real,
        # confirmed application submission as "no_result_line" because the
        # model wrote "RESULT: APPLIED" once.
        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if re.search(rf"RESULT:\s*{result_status}\b", output):
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if re.search(r"RESULT:\s*FAILED", output):
            for out_line in output.split("\n"):
                match = re.search(r"RESULT:\s*FAILED\s*:?\s*(.*)", out_line)
                if match:
                    reason = match.group(1).strip() or "unknown"
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    # Case-fold before the membership check -- confirmed live:
                    # the model wrote "RESULT:FAILED:CAPTCHA" (uppercase reason)
                    # instead of the documented standalone "RESULT:CAPTCHA", so
                    # this branch never matched, the failure fell through to a
                    # plain "failed:CAPTCHA", and _is_permanent_failure() (which
                    # checks PERMANENT_FAILURES, an all-lowercase set) never
                    # recognized it as permanent -- losing the capability
                    # -signature-based smart retry for a real CAPTCHA failure.
                    if reason.lower() in PROMOTE_TO_STATUS:
                        reason = reason.lower()
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        # A run can complete the real submission but never print the
        # required RESULT: marker -- confirmed live: a job's actual
        # transcript showed a genuine "Thank You / your application was
        # submitted successfully" confirmation page, yet fell all the way
        # through to failed:no_result_line and became eligible for an
        # auto-retry that would have filed a real duplicate application.
        # An unambiguous on-page success phrase is treated as APPLIED here
        # rather than silently miscounting a real submission as a failure.
        if re.search(r"application (was|has been) (successfully )?submitted|submitted successfully",
                     output, re.I):
            add_event(f"[W{worker_id}] APPLIED (inferred, no RESULT line) ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status="applied", last_action=f"APPLIED inferred ({elapsed}s)")
            return "applied", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)
        # The MCP config file holds the IMAP/SMTP password in plaintext --
        # it's only needed for the subprocess's own lifetime, don't leave a
        # real credential sitting on disk indefinitely once the job is done.
        mcp_config_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


def capability_signature() -> str:
    """A short string that changes whenever something relevant to a past
    failure's *cause* might have changed -- the applypilot version, and
    whether CapSolver is now configured.

    "Permanent" failures (captcha, login_issue, expired, ...) get
    apply_attempts=99 specifically so acquire_job() never retries them
    blindly -- most of the time a login_issue really is a bad password and
    retrying wastes an attempt. But "permanent" only ever meant "not worth
    retrying under the same conditions." A captcha failure recorded before
    CAPSOLVER_API_KEY was set, or a login_issue recorded on an older
    version that had a real sign-in bug, deserves one fresh look once
    conditions change -- without that, closing the gap that caused the
    failure requires a human to remember which specific jobs it might have
    affected and manually run --reset-failed. This signature is stored
    alongside a failure (see mark_result/mark_job) and compared against the
    current one in acquire_job() -- a mismatch means "worth one more try."

    Deliberately narrow: only signals proven to matter are included. Adding
    more here should be a considered decision, not a default -- an
    over-broad signature (e.g. hashing the whole environment) would trigger
    pointless retries on every unrelated change.
    """
    from applypilot import __version__
    capsolver = "1" if os.environ.get("CAPSOLVER_API_KEY") else "0"
    return f"v={__version__};capsolver={capsolver}"


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)

            if result == "session_limit":
                release_lock(job["url"])
                update_state(worker_id, status="done", last_action="session limit — retry after reset")
                break
            elif result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = config.make_console()

    reset_count = reset_stuck_jobs()
    if reset_count:
        console.print(f"[dim]Reset {reset_count} stuck job(s) from a previous run.[/dim]")

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

        try:
            from applypilot.apply import email_verify
            swept = email_verify.sweep_verification_emails()
            if swept:
                console.print(f"[dim]Archived {len(swept)} stray verification email(s) from inbox.[/dim]")
        except Exception:
            logger.exception("Inbox sweep failed (non-fatal)")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
