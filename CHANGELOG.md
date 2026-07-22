# Changelog

All notable changes to this project will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

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
