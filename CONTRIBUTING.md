# Contributing to ApplyPilot

## Development Setup

```bash
git clone https://github.com/ciguarin/applypilot.git
cd applypilot
uv sync --dev
```

Run the CLI from source:
```bash
uv run applypilot --version
```

Lint:
```bash
uv run ruff check src/
uv run ruff check src/ --fix
```

## Project Structure

```
applypilot/
├── src/applypilot/
│   ├── cli.py                  # CLI entry points (typer)
│   ├── pipeline.py             # Stage orchestrator
│   ├── config.py               # Config loading, tier detection
│   ├── database.py             # SQLite schema + queries
│   ├── llm.py                  # Multi-provider LLM client
│   ├── discovery/
│   │   ├── github_readme.py    # Canadian internship lists (primary source)
│   │   ├── workday.py          # Corporate Workday portals
│   │   └── smartextract.py     # AI-powered URL scraping
│   ├── enrichment/
│   │   └── detail.py           # Full description + apply URL fetch
│   ├── scoring/
│   │   ├── scorer.py           # LLM fit scoring
│   │   ├── tailor.py           # Resume tailoring
│   │   ├── cover_letter.py     # Cover letter generation
│   │   ├── validator.py        # Resume output validation
│   │   └── pdf.py              # LaTeX PDF via resumake
│   ├── apply/
│   │   ├── launcher.py         # Chrome worker + Claude Code orchestration
│   │   └── prompt.py           # Apply agent prompt builder
│   ├── wizard/
│   │   └── init.py             # Setup wizard (applypilot init)
│   ├── config/
│   │   ├── profile.example.json
│   │   ├── searches.example.yaml
│   │   ├── employers.yaml      # Workday employer list
│   │   ├── sites.yaml          # ATS site config
│   │   └── .env.example
│   └── scripts/
│       ├── apply_daemon.sh     # macOS/Linux 12h daemon
│       └── launchagents/
│           └── com.applypilot.apply.plist.template
├── install.sh                  # macOS one-liner installer
├── install.ps1                 # Windows one-liner installer
└── pyproject.toml
```

## Adding Canadian Employers (Workday)

Edit `src/applypilot/config/employers.yaml`. Find the company's Workday URL (format: `https://<tenant>.wd<N>.myworkdayjobs.com`) and add:

```yaml
company_key:
  name: "Company Name"
  tenant: "companytenantid"
  base_url: "https://companytenantid.wd3.myworkdayjobs.com"
```

Test with `applypilot run discover`.

## Adding GitHub README Sources

Edit `src/applypilot/discovery/github_readme.py`. Add an entry to `DEFAULT_SOURCES`:

```python
{
    "key": "unique_key",
    "name": "Display Name",
    "readme_url": "https://raw.githubusercontent.com/user/repo/main/README.md",
    "sha_url": "https://api.github.com/repos/user/repo/commits?path=README.md&per_page=1",
    "parser": "negarprh",  # or "hanzili" depending on table schema
}
```

If the README uses a different table format, add a new parser function and register it in `_PARSERS`.

## Releases

1. Bump `version` in `pyproject.toml`
2. Update `CHANGELOG.md`
3. Commit and push to `v1`
4. `git tag vX.Y.Z && git push origin vX.Y.Z`
5. `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."`

Install scripts reference `@v1` (the branch) so users always get the latest on that line. Pin to a specific tag in the install URL when making a breaking change that warrants a new major branch.

## License

AGPL-3.0. Contributions are licensed under the same terms.
