# Changelog

All notable changes to this project will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

## [1.5.0] - 2026-07-23

### Fixed
- **Apply-worker email tool name mismatch** (`apply/prompt.py`). The prompt told the agent to call email tools as `email:list_emails` etc., which isn't a real MCP tool name (the actual name is `mcp__email__list_emails`) -- every call failed, and the agent fell back to `ToolSearch`, which surfaced an unrelated connected Gmail account instead of the configured IMAP inbox, so OTP/verification codes were never found. Confirmed live: fixing this unstuck a job that had been permanently failing on `email_verification_blocked`.
- **CAPTCHA prompt regression** (`apply/prompt.py`). Our fork's CAPTCHA section was a compressed rewrite of upstream's that dropped the enforcement language telling the agent to always call CapSolver's API before doing anything else, and to go back and try the API if it hadn't yet. Without it, the agent would see a rendered hCaptcha image-challenge and just start clicking on images itself instead of calling `createTask`. Replaced with upstream's verbatim text (checked Pickle-Pixel/ApplyPilot's issue tracker first -- not a known upstream bug, a regression from our own earlier rewrite).
- **Apply-worker shell access not actually blocked on Windows** (`apply/launcher.py`). `--disallowedTools` named `Bash`, but this OS's actual shell tool is `PowerShell`, which was never blocked. Confirmed live: 53 real PowerShell calls across one night's logs, executed unblocked under `--permission-mode bypassPermissions` with no confirmation prompt. Nothing destructive found in what was actually run, but the worker had unrestricted shell access on the host machine by omission, not by design.
- **`RESULT:APPLIED` accepted with no verification** (`apply/launcher.py`). The parser trusted the model's own success marker unconditionally. Confirmed live: a transcript said "Now let me click Save and Continue" and then printed `RESULT:APPLIED` with no click ever happening, still mid-form -- silently marked applied in the DB with a real timestamp. Now requires a quoted on-page confirmation phrase (not just the marker, and not just the phrase anywhere -- the model's own narrative claim used the same trigger words without it being true) before accepting APPLIED; otherwise routes to a new retryable `failed:unverified_applied` status.
- **Indeed `--url` retry misrouting**, **uppercase `RESULT:FAILED:CAPTCHA` not promoted to permanent status**, **silent on-page success with no `RESULT:` marker miscounted as failure**, **`ToolSearch` blocked in `--disallowedTools`** (all `apply/launcher.py`), **Canadian postal code space-stripping instruction**, **Workday dropdown async-state race** (both `apply/prompt.py`) -- see prior commits same date.

## [1.4.0] - 2026-07-22

### Added
- **Automatic retry for permanently-failed jobs when conditions change** (`apply/launcher.py: capability_signature()`). Vanilla upstream behavior sets `apply_attempts=99` for failures judged unrecoverable (`captcha`, `login_issue`, `expired`, etc. -- `PERMANENT_FAILURES`), which correctly avoids blindly re-trying a bad password forever, but also meant a real fix (a login-flow bug patch, or turning on `CAPSOLVER_API_KEY`) could never reach jobs that failed before the fix existed without a human manually running `--reset-failed` -- and there was no way to know *which* past failures a given fix actually applies to. Every failure is now tagged with a signature (currently: applypilot version + whether CapSolver is configured); `acquire_job()` gives a job one fresh attempt, with a full reset attempts budget, whenever the current signature differs from the one it failed under. Deliberately narrow -- only signals proven to matter are included, to avoid retrying on irrelevant changes.

### Fixed
- **`applypilot.__version__` was hardcoded to "1.0.0"** in `__init__.py`, completely disconnected from `pyproject.toml` -- `applypilot --version` had been silently wrong since the very first version bump after the v1 fork. Now resolved dynamically via `importlib.metadata`, the actual source of truth.

## [1.3.1] - 2026-07-22

### Fixed
- **`email_verify.py` now archives verification/reset emails after use**, moving them out of INBOX (default: the existing "Verification" mailbox) via IMAP MOVE. The agent path already did this for OTP emails (`move_email` in `apply/prompt.py`); the deterministic Workday fast path's own email polling never did, leaving every account-verification and password-reset email it ever read sitting in the real inbox indefinitely.

## [1.3.0] - 2026-07-22

### Added
- **Deterministic dead-listing detection** (`scoring/scorer.py`) — before spending an LLM call scoring a job, checks `full_description` against a set of dead/closed-listing phrases ("no longer accepting applications", "this job has expired", "job not found", etc.) and a minimum-length backstop. Matches short-circuit straight to a score of 1 with no LLM call. Complements `discovery/github_readme.py`'s existing prune-on-removal — that only catches a listing after the curator notices and updates the source list; this catches it the moment enrichment actually fetches the dead page. Validated against real data: 15/15 known-dead listings caught, and it surfaced 3 real cases the LLM scorer had gotten wrong (scoring a literal "Job not found" page 10/10) across 215 already-scored jobs.

## [1.2.0] - 2026-07-22

### Added
- **`applypilot config blocked`** — add/remove blocked sites and URL patterns for the apply stage without hand-editing the package's bundled `config/sites.yaml` (which gets overwritten on every `applypilot update`). Additions are stored in `~/.applypilot/blocked_sites.yaml` and merged with the package defaults at load time.
- **`applypilot apply --reset-stuck`** — clear jobs left `in_progress` by a crashed or killed run. Also now happens automatically at the start of every `applypilot apply` invocation.
- **`applypilot status --by-platform`** — breaks the apply-ready queue down by ATS platform (Workday, Greenhouse, LinkedIn, iCIMS, etc.).
- **`applypilot daemon status/enable/disable/run-now`** — direct control over the scheduled background daemon on both platforms, without raw `schtasks`/`launchctl` commands.

### Fixed
- **`doctor`'s daemon health check** only tested whether `schtasks /Query` succeeded, which it does whether the task is enabled or disabled — a disabled daemon was reported as "OK". Now parses the actual `Scheduled Task State`.
- **MCP server versions are now pinned** (`@playwright/mcp`, `@codefuturist/email-mcp`) instead of always resolving to latest on every agent invocation — matters most for the third-party email package, which receives real IMAP/SMTP credentials.
- **Per-worker MCP config files** (containing the IMAP/SMTP password in plaintext) are now deleted once the agent subprocess exits, instead of sitting in `~/.applypilot` indefinitely between runs.
- **Stuck-job recovery** no longer depends on a raw `sqlite3` one-liner duplicated in both platforms' daemon scripts — moved into the package itself (`launcher.reset_stuck_jobs()`).

## [1.1.0] - 2026-07-22

### Added
- **Deterministic Workday apply fast path** (`apply/platforms/workday.py`) — completes the entire Workday application wizard (sign-in/account creation, email verification, password reset, resume upload, address/contact/EEO fields) via Playwright directly, no LLM agent involved. Falls back to the full Claude Code agent for anything genuinely tenant-specific: custom screening questions, CAPTCHAs, or an unrecognized page layout.
- **`apply/email_verify.py`** — pure-IMAP polling for verification/reset links, shared by any platform filler that needs to clear an email gate without a full agent session. UID-baselined so a stale email from an earlier attempt can't be mistaken for a fresh one.
- Tailored resume project bullets now include the description and tech stack line, not just name and date.

### Fixed
- **Windows Task Scheduler daemon hang** — `pwsh.exe` hangs indefinitely with zero output when Task Scheduler launches it non-interactively on some Windows 11 machines. Daemon registration now forces `powershell.exe` 5.1, drops `-WindowStyle Hidden`, and adds `-WakeToRun`.
- **OTP/verification-email wait window** extended from ~18s to ~70s (three-stage backoff) across the agent's Google SSO, Microsoft SSO, and plain email/password login flows — real verification emails routinely take longer than the old window allowed.
- **iCloud IMAP login** — custom-domain email aliases (`you@yourdomain.com`) can't authenticate directly against iCloud's IMAP server; the actual Apple ID is required as the login username even though mail to the alias lands in the same inbox. Fixed in both the email MCP config and `email_verify.py`.
- **Stale job queue ordering** — `acquire_job()` now excludes postings older than 14 days and orders by freshness, matching the existing pruning convention. Untouched backlog was observed hitting 55% dead-listing failures from postings an average of 16 days stale.
- **`RESULT:APPLIED`/`RESULT:FAILED` parsing** is now whitespace-tolerant — a strict no-space match previously mislabeled a real, confirmed application submission as a failure because the model wrote `RESULT: APPLIED` with a space.
- **Blocked-site URL matching** now checks the resolved `application_url`, not just the raw source/tracker `url` — a job sourced from a non-blocked aggregator could still resolve to a blocked ATS (e.g. LinkedIn) once enriched, slipping past the blocklist entirely.

### Changed
- **JobSpy discovery (LinkedIn/Indeed/Glassdoor) is now opt-in**, not on by default — real usage showed ~0.4% conversion to the auto-apply threshold versus the GitHub README sources, and LinkedIn's login flow carries real account-lockout risk during the apply stage. Set `sites:` in `searches.yaml` to re-enable.

## [1.0.0] - 2026-06-23

First release of the Canadian-focused fork. Based on [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) v0.3.0.

### Added
- **GitHub README discovery** — polls curated Canadian internship lists (negarprh/Canadian-Tech-Internships-2026, hanzili/canada_sde_intern_position) with SHA-based change detection. Replaces jobspy + n8n entirely.
- **macOS one-liner install** — `curl ... | bash` installs uv, the package, Node MCPs, browser, daemon, and config in one shot
- **Windows one-liner install** — `irm ... | iex` equivalent with Task Scheduler daemon instead of LaunchAgent
- **Setup wizard overhaul** — collects first/last/middle name split (for ATS field-by-field fill), full education block (degree, field, institution, GPA, start/end year, in-progress flag), IMAP email credentials with auto-detection by domain, browser selection with system detection and Playwright Chromium fallback, PDF/DOCX resume upload with auto-conversion to txt
- **Wizard review screen** — after completing all sections, shows a one-line summary of each. Type a number to redo that section without restarting
- **Platform-agnostic email MCP** — supports any IMAP provider (iCloud, Gmail, Outlook, Fastmail, etc.) via `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST` env vars. Replaces Gmail-only upstream approach
- **OTP email archiving** — apply agent archives OTP/verification emails after use via `move_email` to keep inbox clean
- **Education block in apply prompt** — structured education data (degree, field, institution, GPA, dates, in-progress flag) replaces flat `education_level` string. Includes projected completion hint for date pickers
- **Canadian ATS prompt improvements** — postal code handling (strip spaces, FSA fallback), dropdown fuzzy matching, React/SPA input event dispatch, date picker JS override, education form fill/edit/re-add flows
- **LaTeX PDF generation** — resumes generated via resumake API instead of HTML renderer
- **pypdf bundled** — PDF-to-text conversion ships as a package dependency, no separate install step

### Changed
- **Discovery**: jobspy removed. GitHub README + Workday + smart extract are the three sources
- **Resume format**: no summary section — skills and experience lead. Validator and tailor prompt updated accordingly
- **Package dependencies**: pandas removed (was jobspy-only). pypdf added
- **Daemon**: macOS LaunchAgent and Windows Task Scheduler daemon run `applypilot apply` every 12h. Scripts and plist template bundled inside the package under `scripts/`
- **Config/asset bundling**: `profile.example.json`, `.env.example`, `apply_daemon.sh`, and launchagent template ship inside the installed package — install scripts copy from there, no repo clone required

### Removed
- **jobspy** — replaced by GitHub README discovery
- **n8n dependency** — ingestion workflow ported to pure Python (`discovery/github_readme.py`)
- **Gmail-only MCP** — replaced by generic IMAP MCP

---

*Entries below this line are from the upstream [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) project and are preserved for historical context.*

## [0.2.0] - 2026-02-17

### Added
- Parallel workers for discovery/enrichment (`applypilot run --workers N`)
- Apply utility modes: `--gen`, `--mark-applied`, `--mark-failed`, `--reset-failed`
- Dry-run mode: `applypilot apply --dry-run`
- 5 new tracking columns: `agent_id`, `last_attempted_at`, `apply_duration_ms`, `apply_task_id`, `verification_confidence`
- Manual ATS detection via `config/sites.yaml`
- Qwen3 `/no_think` token optimization
- `config.DEFAULTS` centralized dict for magic numbers

### Fixed
- Config YAML not found after install — moved `config/` into package
- Search config format mismatch between wizard and discovery code
- JobSpy install isolation (`--no-deps`)
- Scoring batch limit (was silently capping at 50)
- Missing logging output

### Changed
- Blocked sites, base URLs, SSO domains externalized to `config/sites.yaml`
- Prompt improvements for salary and screening context
- `acquire_job()` column write fix

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover, apply
- Workday employer portal support (46 preconfigured employers)
- AI scoring, resume tailoring, cover letter generation
- Autonomous browser-based application via Playwright + Claude Code
- Interactive setup wizard
- Multi-provider LLM support (Gemini, OpenAI, local)
- Pipeline stats and HTML dashboard
- YAML-based config for employers, sites, search queries
