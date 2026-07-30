# ApplyPilot

[![Latest release](https://img.shields.io/github/v/release/ciguarin/applypilot)](https://github.com/ciguarin/applypilot/releases)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

Automated job application pipeline for Canadian CS internships. Discovers listings from curated GitHub repos and job boards, scores them against your profile, tailors your resume, writes cover letters, and submits applications autonomously.

Fork of [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) — rebuilt for Canadian students: intern-first discovery, Canadian job board targeting, city-aware filtering, and a setup wizard that actually works.

## Development methodology

Built iteratively with [Claude Code](https://claude.ai/code) as a development accelerator, not a replacement for engineering judgment — every entry in [CHANGELOG.md](CHANGELOG.md) documents a root-cause diagnosis (not just a symptom fix) and cites how it was verified live against a real job application before being considered done. Roughly three-quarters of commits carry AI co-authorship attribution; the rest are hand-written. Both are normal parts of how this project gets built — see `git log` for the exact split.

---

## Install

**macOS**
```bash
curl -fsSL https://raw.githubusercontent.com/ciguarin/applypilot/v1/install.sh | bash
```

**Windows** (PowerShell)
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
irm https://raw.githubusercontent.com/ciguarin/applypilot/v1/install.ps1 | iex
```

Then run the setup wizard:
```bash
applypilot init
```

---

## Requirements

- Python 3.11+
- [Git](https://git-scm.com) — required by the installer
- [uv](https://astral.sh/uv) — installed automatically by the script
- An LLM API key: [Gemini](https://aistudio.google.com) (free tier works), OpenAI, or any OpenRouter model
- Node.js 18+ and a Chromium browser — only needed for the auto-apply stage (Tier 3)
- [Claude Code](https://claude.ai/code) CLI — only needed for the auto-apply stage (Tier 3)

---

## How it works

```
discover → enrich → score → tailor → cover → pdf → apply
```

| Stage | What it does |
|-------|-------------|
| `discover` | Pulls listings from GitHub-curated internship lists + LinkedIn + Indeed |
| `enrich` | Fetches full job descriptions and direct apply URLs |
| `score` | LLM scores each job 1–10 against your resume and profile |
| `tailor` | LLM rewrites your resume for each high-fit job |
| `cover` | LLM generates a tailored cover letter |
| `pdf` | Converts resumes and cover letters to PDF |
| `apply` | Claude Code + Playwright fills out and submits application forms autonomously |

Each stage is a fully decoupled worker reading from and writing to a central SQLite database. Run them all at once or individually.

```bash
# Run everything
applypilot run

# Run specific stages
applypilot run discover enrich
applypilot run score tailor cover pdf

# Run stages concurrently (streaming mode)
applypilot run --stream

# Auto-apply to all ready jobs
applypilot apply
applypilot apply --limit 5 --workers 2
```

---

## Discovery sources

**GitHub README** (highest signal — ~80% high-fit rate)
Polls two curated Canadian internship lists on every run with SHA-based change detection — skips the fetch if nothing changed:
- [negarprh/Canadian-Tech-Internships-2026](https://github.com/negarprh/Canadian-Tech-Internships-2026)
- [hanzili/canada_sde_intern_position](https://github.com/hanzili/canada_sde_intern_position)

**LinkedIn + Indeed (JobSpy)**
Searches 6 intern-specific queries across Canada with remote filtering. Configured to target the Canadian Indeed index. Discovery runtime is ~5 min for a full run.

**Workday** (disabled by default)
Scrapes ~48 Canadian corporate employers directly. Add `APPLYPILOT_WORKDAY=1` to your `.env` to enable. High runtime (~20 min), low incremental yield vs job boards.

---

## Tiers

| Tier | Requires | Unlocks |
|------|----------|---------|
| 1 | Nothing | `discover`, `enrich`, `status`, `dashboard` |
| 2 | LLM API key | `score`, `tailor`, `cover`, `pdf`, `run` |
| 3 | Claude Code + Node.js + browser | `apply` |

```bash
applypilot doctor              # check which tier you're on and what's missing
applypilot status              # pipeline state at a glance
applypilot status --by-platform  # apply-ready queue broken down by ATS (Workday, Greenhouse, LinkedIn, ...)
```

---

## Configuration

Everything lives in `~/.applypilot/`. Run `applypilot init` once to generate it, then use `applypilot config` to change anything without re-running the wizard.

```bash
applypilot config show        # view all current settings
applypilot config profile     # name, email, phone, LinkedIn, GitHub
applypilot config cities      # target cities (Toronto, Ottawa, Vancouver, etc.)
applypilot config queries     # search queries (job titles)
applypilot config searches    # hours lookback, results per site, job boards
applypilot config model       # LLM model
applypilot config score       # minimum fit score threshold (default: 7)
applypilot config api         # API keys and LLM provider
applypilot config resume      # open resume.txt in your editor
applypilot config blocked     # add/remove sites or URL patterns the apply stage should skip
```

| File | Purpose |
|------|---------|
| `profile.json` | Your info: name, education, skills, locked resume bullets, project descriptions |
| `searches.yaml` | Target queries, locations, job boards |
| `.env` | API keys, model override, feature flags |
| `applypilot.db` | SQLite pipeline state — the conveyor belt |
| `tailored_resumes/` | Per-job tailored resumes (txt + pdf) |
| `cover_letters/` | Per-job cover letters (txt + pdf) |

---

## Scheduling

The installer sets up a background daemon that runs `applypilot run` followed by `applypilot apply` every 12 hours:

- **macOS** — LaunchAgent (`~/Library/LaunchAgents/com.applypilot.apply.plist`)
- **Windows** — Task Scheduler (`ApplyPilot.Apply`)

Control it directly without touching Task Scheduler or `launchctl`:

```bash
applypilot daemon status    # registered? enabled? when did it last/next run?
applypilot daemon enable    # turn it back on (re-registers it if missing entirely)
applypilot daemon disable   # pause it without unregistering
applypilot daemon run-now   # trigger a run immediately, outside its schedule
```

If a run crashes or gets killed mid-job, `applypilot apply` clears any job left stuck `in_progress` automatically on its next run — no manual database surgery needed. To do it without launching a full run: `applypilot apply --reset-stuck`.

---

## Keeping up to date

```bash
applypilot update
```

---

## What's different from upstream

| Area | Upstream | This fork |
|------|----------|-----------|
| Discovery | US job boards via JobSpy | JobSpy targeting Canadian Indeed index + GitHub-curated Canadian internship lists |
| Job filter | All roles | Intern/co-op titles only; no senior/director noise |
| Location | Hardcoded | City-aware — wizard lets you pick Toronto, Ottawa, Vancouver, etc. |
| Location filter | Drops all remote-search results (bug) | Fixed: remote searches bypass city filter correctly |
| LinkedIn fetch | Per-job HTTP calls during discovery (45-min timeouts) | Disabled: enrich stage handles descriptions |
| Workday | Always runs | Opt-in via `APPLYPILOT_WORKDAY=1` |
| DB hygiene | None | 14-day TTL prunes expired unscored listings automatically |
| Score versioning | None | Scores auto-invalidate when scoring prompt changes |
| Config | Re-run `init` to change anything | `applypilot config` subcommands for everything |
| `applypilot update` | Overwrites dev installs | Guards against overwriting editable installs |
| Graduation dates in apply agent | Hardcoded (upstream author's dates) | Read from `profile.json` |

---

## License

AGPL-3.0 — same as upstream.
