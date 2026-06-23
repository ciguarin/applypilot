# ApplyPilot

Autonomous job application pipeline for Canadian tech internships. Discovers listings, tailors your resume, writes cover letters, and submits applications — hands-free.

Fork of [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) with first-party Windows + macOS support, Canadian-first discovery, and a platform-agnostic setup wizard.

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

## Requirements

- Python 3.11+
- [uv](https://astral.sh/uv) (installed automatically)
- Node.js 18+ (for auto-apply via Claude Code + Playwright)
- A Chromium-based browser (Chrome, Brave, Edge) — or one gets downloaded automatically
- An LLM API key: [Gemini](https://aistudio.google.com) (free tier available), OpenAI, or OpenRouter

## Pipeline

```
discover → enrich → score → tailor → cover → pdf → apply
```

| Stage | What it does |
|-------|-------------|
| discover | Scrapes Canadian internship listings from curated GitHub repos + Workday corporate boards |
| enrich | Fetches full job descriptions and apply URLs |
| score | LLM assigns a fit score 1–10 against your profile |
| tailor | LLM rewrites your resume for each high-fit job |
| cover | LLM generates a cover letter |
| pdf | Converts resumes and cover letters to PDF via LaTeX |
| apply | Claude Code + Playwright fills out and submits the application autonomously |

Run all stages:
```bash
applypilot run
```

Run specific stages:
```bash
applypilot run discover enrich
applypilot run score tailor cover pdf
```

Auto-apply:
```bash
applypilot apply
applypilot apply --limit 5 --workers 2
```

Check status:
```bash
applypilot status
applypilot doctor
```

## Discovery sources

- **GitHub README** — polls [negarprh/Canadian-Tech-Internships-2026](https://github.com/negarprh/Canadian-Tech-Internships-2026) and [hanzili/canada_sde_intern_position](https://github.com/hanzili/canada_sde_intern_position) every run, with SHA-based change detection (skips fetch if nothing changed)
- **Workday** — scrapes ~50 Canadian corporate employers directly (RBC, TD, CIBC, Shopify, etc.)
- **Smart extract** — AI-powered scraping for additional job board URLs

Filters to Toronto + Remote only. Drops senior, hardware, and firmware roles automatically.

## Tiers

| Tier | Requires | Commands unlocked |
|------|----------|-------------------|
| 1 | Nothing | `discover`, `enrich`, `status`, `dashboard` |
| 2 | LLM API key | `score`, `tailor`, `cover`, `pdf`, `run` |
| 3 | Claude Code + browser + email | `apply` |

## Configuration

After `applypilot init`, your config lives at `~/.applypilot/`:

| File | Purpose |
|------|---------|
| `profile.json` | Your info: name, education, skills, resume bullets |
| `searches.yaml` | Target roles, locations, search radius |
| `.env` | API keys, email credentials, browser path |
| `applypilot.db` | SQLite pipeline state |
| `tailored_resumes/` | Per-job tailored resumes (txt + pdf) |
| `cover_letters/` | Per-job cover letters (txt + pdf) |

## Scheduling

The installer sets up a background daemon that runs the full pipeline every 12 hours:

- **macOS** — LaunchAgent (`~/Library/LaunchAgents/com.applypilot.apply.plist`)
- **Windows** — Task Scheduler (`ApplyPilot.Apply`)

## Differences from upstream

- **Discovery**: Replaces jobspy (US-heavy job boards) with native GitHub README parsing and keeps Workday for Canadian corporates. No n8n required.
- **Wizard**: Collects first/last/middle name, full education block (degree, GPA, dates), browser selection, and IMAP email config. Supports PDF/DOCX resume upload with auto-conversion. Review screen lets you redo any section without restarting.
- **Resume format**: No summary section — skills and experience lead.
- **PDF generation**: Uses LaTeX via resumake for clean output.
- **Email**: Platform-agnostic IMAP (iCloud, Gmail, Outlook, any provider) instead of Gmail-only. Archives OTP emails after use.
- **Apply agent**: Handles Canadian postal codes, Workday/Greenhouse/Lever/Ashby forms, React SPA input events, and date picker edge cases.
- **Install**: One-liner for macOS and Windows. No manual patching.

## License

AGPL-3.0 — same as upstream.
