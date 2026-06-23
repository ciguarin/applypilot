"""ApplyPilot setup wizard — extended for Canadian internship pipeline.

Covers everything upstream does, plus:
  - first/last/middle name split (for ATS form auto-fill)
  - education block (degree, institution, GPA, dates)
  - browser auto-detection (Chrome, Brave, Edge, Chromium, or download)
  - IMAP email credentials (for OTP/verification during auto-apply)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from applypilot.config import (
    APP_DIR,
    ENV_PATH,
    PROFILE_PATH,
    RESUME_PATH,
    RESUME_PDF_PATH,
    SEARCH_CONFIG_PATH,
    ensure_dirs,
)

console = Console()

def _load_template() -> dict:
    # Template is always in the cloned repo at ~/.applypilot/config/, regardless of APPLYPILOT_DIR
    candidates = [
        Path.home() / ".applypilot" / "config" / "profile.example.json",
        APP_DIR / "config" / "profile.example.json",
    ]
    for p in candidates:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------------------
# Browser detection
# ---------------------------------------------------------------------------

_BROWSER_CANDIDATES: dict[str, list[str]] = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        "/Applications/Arc.app/Contents/MacOS/Arc",
    ],
    "Linux": [
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
        shutil.which("brave-browser") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("chromium") or "",
        shutil.which("microsoft-edge") or "",
    ],
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
}


def _find_browsers() -> list[tuple[str, str]]:
    """Return list of (label, path) for found Chromium-based browsers."""
    system = platform.system()
    candidates = _BROWSER_CANDIDATES.get(system, [])
    found = []
    for path in candidates:
        if path and Path(path).exists():
            name = Path(path).stem
            label_map = {
                "Google Chrome": "Chrome",
                "Brave Browser": "Brave",
                "Microsoft Edge": "Edge",
                "Chromium": "Chromium",
                "Vivaldi": "Vivaldi",
                "Arc": "Arc",
                "google-chrome": "Chrome",
                "google-chrome-stable": "Chrome",
                "brave-browser": "Brave",
                "chromium-browser": "Chromium",
                "chromium": "Chromium",
                "microsoft-edge": "Edge",
                "chrome": "Chrome",
                "msedge": "Edge",
            }
            label = label_map.get(name, name)
            found.append((label, path))
    return found


def _playwright_chromium_path() -> str | None:
    """Return path to Playwright's bundled Chromium if already installed."""
    system = platform.system()
    if system == "Darwin":
        roots = [Path.home() / "Library/Caches/ms-playwright"]
        exe_name = "chrome"
    elif system == "Linux":
        roots = [Path.home() / ".cache/ms-playwright"]
        exe_name = "chrome"
    elif system == "Windows":
        roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"]
        exe_name = "chrome.exe"
    else:
        return None

    for root in roots:
        if not root.exists():
            continue
        for exe in root.rglob(exe_name):
            if exe.exists() and exe.is_file():
                return str(exe)
    return None


def _setup_browser(env_lines: list[str]) -> None:
    """Detect available browsers and let user choose, or download Chromium."""
    console.print("\n[bold cyan]Browser[/bold cyan] [dim](Chromium-based required for auto-apply)[/dim]")

    found = _find_browsers()
    playwright_chromium = _playwright_chromium_path()

    if not found and not playwright_chromium:
        console.print("[yellow]No Chromium-based browser detected on this system.[/yellow]")
        console.print("[dim]Options: install Chrome/Brave/Edge, or let Playwright download Chromium.[/dim]")
    else:
        if found:
            names = ", ".join(f"[green]{label}[/green]" for label, _ in found)
            console.print(f"Found: {names}")
        if playwright_chromium:
            console.print(f"[dim]Playwright Chromium: {playwright_chromium}[/dim]")

    # Build choice list
    choices: list[tuple[str, str]] = list(found)
    if playwright_chromium:
        choices.append(("Playwright Chromium", playwright_chromium))

    if choices:
        console.print()
        for i, (label, path) in enumerate(choices):
            console.print(f"  [{i + 1}] {label}  [dim]{path}[/dim]")
        console.print(f"  [{len(choices) + 1}] Download Playwright Chromium now")
        console.print(f"  [{len(choices) + 2}] Enter custom path")
        console.print(f"  [0] Skip (configure CHROME_PATH in .env later)")
        console.print()

        idx_str = Prompt.ask(
            "Choose",
            default="1" if choices else str(len(choices) + 1),
        )
        try:
            idx = int(idx_str)
        except ValueError:
            idx = 1

        if idx == 0:
            console.print("[dim]Skipped. Set CHROME_PATH in .env before running auto-apply.[/dim]")
            return
        elif 1 <= idx <= len(choices):
            label, path = choices[idx - 1]
            env_lines += [f"CHROME_PATH={path}", ""]
            console.print(f"[green]Using {label}:[/green] {path}")
            return
        elif idx == len(choices) + 1:
            pass  # fall through to download
        elif idx == len(choices) + 2:
            custom = Prompt.ask("Full path to browser executable")
            if custom.strip() and Path(custom.strip()).exists():
                env_lines += [f"CHROME_PATH={custom.strip()}", ""]
                console.print(f"[green]Set CHROME_PATH to:[/green] {custom.strip()}")
            else:
                console.print("[yellow]Path not found. Set CHROME_PATH in .env manually.[/yellow]")
            return
    else:
        if not Confirm.ask("Download Playwright Chromium? (~300MB, takes a minute)", default=True):
            console.print("[dim]Set CHROME_PATH in .env before running auto-apply.[/dim]")
            return

    # Download Playwright Chromium
    console.print("[cyan]Downloading Playwright Chromium...[/cyan]")
    try:
        subprocess.run(
            ["npx", "--yes", "playwright", "install", "chromium"],
            check=True, timeout=300,
        )
        path = _playwright_chromium_path()
        if path:
            env_lines += [f"CHROME_PATH={path}", ""]
            console.print(f"[green]Playwright Chromium installed:[/green] {path}")
        else:
            console.print("[yellow]Installed but couldn't find path. Set CHROME_PATH in .env manually.[/yellow]")
    except Exception as e:
        console.print(f"[red]Download failed:[/red] {e}")
        console.print("[dim]Install Chrome/Brave manually, then set CHROME_PATH in .env.[/dim]")


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def _to_txt(src: Path) -> str | None:
    """Extract plain text from a PDF or DOCX. Returns text or None on failure."""
    suffix = src.suffix.lower()

    if suffix == ".pdf":
        # 1. pypdf — pure Python, installed by install.sh, works everywhere
        try:
            import pypdf
            reader = pypdf.PdfReader(str(src))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if text.strip():
                return text
        except Exception:
            pass

        # 2. pdftotext (poppler) — better layout preservation if available
        if shutil.which("pdftotext"):
            try:
                result = subprocess.run(
                    ["pdftotext", "-layout", str(src), "-"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout
            except Exception:
                pass

        # 3. pdfminer.six
        try:
            from pdfminer.high_level import extract_text as pdfminer_extract
            text = pdfminer_extract(str(src))
            if text.strip():
                return text
        except ImportError:
            pass

    elif suffix in (".docx", ".doc"):
        # 1. python-docx — pure Python
        try:
            import docx
            doc = docx.Document(str(src))
            text = "\n".join(p.text for p in doc.paragraphs)
            if text.strip():
                return text
        except ImportError:
            pass

        # 2. pandoc — if available
        if shutil.which("pandoc"):
            try:
                result = subprocess.run(
                    ["pandoc", str(src), "-t", "plain", "--wrap=none"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout
            except Exception:
                pass

    return None


def _setup_resume() -> None:
    console.print(Panel(
        "[bold]Step 1: Resume[/bold]\n"
        "Provide your resume in two formats:\n"
        "  • Plain text (.txt) — used by the AI for tailoring\n"
        "  • PDF (.pdf) — uploaded to application forms\n\n"
        "If you only have a PDF or DOCX, ApplyPilot will convert it to text automatically."
    ))

    # ── Plain text (or convert from PDF/DOCX) ────────────────────────────────
    while True:
        path_str = Prompt.ask("Resume file (.txt, .pdf, or .docx)")
        src = Path(path_str.strip().strip('"').strip("'")).expanduser().resolve()
        if not src.exists():
            console.print(f"[red]File not found:[/red] {src}")
            continue

        suffix = src.suffix.lower()

        if suffix == ".txt":
            shutil.copy2(src, RESUME_PATH)
            console.print(f"[green]Copied to {RESUME_PATH}[/green]")
            pdf_src = src.with_suffix(".pdf")
            break

        elif suffix in (".pdf", ".docx", ".doc"):
            console.print(f"[cyan]Converting {src.name} to plain text...[/cyan]")
            text = _to_txt(src)
            if text:
                RESUME_PATH.write_text(text, encoding="utf-8")
                console.print(f"[green]Converted and saved to {RESUME_PATH}[/green]")
                console.print("[dim]Review it after setup — conversions aren't always perfect.[/dim]")
            else:
                console.print(
                    "[yellow]Conversion failed.[/yellow] No supported tool found.\n"
                    "Install one: [bold]brew install poppler[/bold] (pdftotext) or [bold]brew install pandoc[/bold]\n"
                    "Or provide a .txt version manually."
                )
                if not Confirm.ask("Continue without plain-text resume? (you can add it later)", default=False):
                    continue
            pdf_src = src if suffix == ".pdf" else src.with_suffix(".pdf")
            break

        else:
            console.print("[red]Unsupported format.[/red] Provide .txt, .pdf, or .docx")
            continue

    # ── PDF ──────────────────────────────────────────────────────────────────
    # Pre-fill with the PDF if user already gave one
    default_pdf = str(pdf_src) if "pdf_src" in dir() and pdf_src.exists() else ""
    while True:
        prompt_text = "PDF resume (.pdf)"
        path_str = Prompt.ask(prompt_text, default=default_pdf)
        if not path_str.strip():
            console.print("[yellow]Skipped. Add resume.pdf to ~/.applypilot/ before applying.[/yellow]")
            break
        src = Path(path_str.strip().strip('"').strip("'")).expanduser().resolve()
        if not src.exists():
            console.print(f"[red]File not found:[/red] {src}")
            continue
        if src.suffix.lower() != ".pdf":
            console.print("[red]Must be a .pdf file.[/red]")
            continue
        shutil.copy2(src, RESUME_PDF_PATH)
        console.print(f"[green]Copied to {RESUME_PDF_PATH}[/green]")
        break


# ---------------------------------------------------------------------------
# Personal
# ---------------------------------------------------------------------------

def _setup_personal(profile: dict) -> None:
    console.print(Panel("[bold]Step 2: Personal Information[/bold]\nUsed for ATS form auto-fill."))

    first     = Prompt.ask("First name (legal)")
    last      = Prompt.ask("Last name (legal)")
    middle    = Prompt.ask("Middle name (leave blank if N/A)", default="")
    preferred = Prompt.ask("Preferred/nickname (leave blank to use first name)", default="")

    profile["personal"] = {
        "full_name":      f"{first} {last}",
        "first_name":     first,
        "last_name":      last,
        "middle_name":    middle,
        "preferred_name": preferred or first,
        "email":          Prompt.ask("Email address"),
        "phone":          Prompt.ask("Phone number (e.g. 4165550000)", default=""),
        "city":           Prompt.ask("City"),
        "province_state": Prompt.ask("Province/State (e.g. Ontario)", default=""),
        "country":        Prompt.ask("Country (e.g. Canada)", default="Canada"),
        "postal_code":    Prompt.ask("Postal/ZIP code (no spaces, e.g. M5V0A1)", default=""),
        "address":        Prompt.ask("Street address (optional)", default=""),
        "linkedin_url":   Prompt.ask("LinkedIn URL", default=""),
        "github_url":     Prompt.ask("GitHub URL (optional)", default=""),
        "portfolio_url":  Prompt.ask("Portfolio URL (optional)", default=""),
        "website_url":    Prompt.ask("Personal website URL (optional)", default=""),
        "password":       "",  # set later from AP_PASSWORD in _setup_env
    }


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------

def _setup_education(profile: dict) -> None:
    console.print(Panel("[bold]Step 3: Education[/bold]\nUsed to fill education sections during auto-apply."))

    degree      = Prompt.ask("Degree (e.g. Bachelor of Arts (Honours))")
    field       = Prompt.ask("Field of study (e.g. Computer Science)")
    institution = Prompt.ask("Institution (e.g. York University (Lassonde School of Engineering))")
    gpa         = Prompt.ask("GPA (e.g. 3.7)", default="")
    start_year  = Prompt.ask("Start year (e.g. 2025)", default="")
    end_year    = Prompt.ask("Expected graduation year (e.g. 2029)", default="")
    in_progress = Confirm.ask("Currently in progress?", default=True)

    profile["education"] = [{
        "degree":         degree,
        "field_of_study": field,
        "institution":    institution,
        "gpa":            gpa,
        "start_year":     start_year,
        "end_year":       end_year,
        "in_progress":    in_progress,
    }]
    profile.setdefault("experience", {})["education_level"] = degree


# ---------------------------------------------------------------------------
# Work authorization
# ---------------------------------------------------------------------------

def _setup_work_auth(profile: dict) -> None:
    console.print("\n[bold cyan]Work Authorization[/bold cyan]")
    profile["work_authorization"] = {
        "legally_authorized_to_work": Confirm.ask("Legally authorized to work in your target country?"),
        "require_sponsorship":        Confirm.ask("Will you need sponsorship now or in the future?"),
        "work_permit_type":           Prompt.ask("Work permit type (e.g. Citizen, PR, Open Work Permit)", default=""),
    }


# ---------------------------------------------------------------------------
# Compensation
# ---------------------------------------------------------------------------

def _setup_compensation(profile: dict) -> None:
    console.print("\n[bold cyan]Compensation[/bold cyan]")
    salary       = Prompt.ask("Expected annual salary (number, e.g. 60000)", default="")
    currency     = Prompt.ask("Currency", default="CAD")
    salary_range = Prompt.ask("Acceptable range (e.g. 55000-75000)", default="")
    parts = salary_range.split("-") if "-" in salary_range else [salary, salary]
    profile["compensation"] = {
        "salary_expectation": salary,
        "salary_currency":    currency,
        "salary_range_min":   parts[0].strip(),
        "salary_range_max":   parts[1].strip() if len(parts) > 1 else parts[0].strip(),
    }


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------

def _setup_experience(profile: dict) -> None:
    console.print("\n[bold cyan]Experience[/bold cyan]")
    current_title = Prompt.ask("Current/most recent job title (or 'Computer Science Student')", default="")
    target_role   = Prompt.ask("Target role (e.g. Software Engineer Intern)", default=current_title)
    profile.setdefault("experience", {}).update({
        "years_of_experience_total": Prompt.ask("Years of professional experience", default="0"),
        "current_title":             current_title,
        "target_role":               target_role,
    })


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def _setup_skills(profile: dict) -> None:
    console.print("\n[bold cyan]Skills[/bold cyan] [dim](comma-separated)[/dim]")
    langs      = Prompt.ask("Programming languages", default="")
    frameworks = Prompt.ask("Frameworks & libraries", default="")
    tools      = Prompt.ask("Tools & platforms (e.g. Docker, AWS, Git)", default="")
    profile["skills_boundary"] = {
        "programming_languages": [s.strip() for s in langs.split(",") if s.strip()],
        "frameworks":            [s.strip() for s in frameworks.split(",") if s.strip()],
        "tools":                 [s.strip() for s in tools.split(",") if s.strip()],
    }


# ---------------------------------------------------------------------------
# Resume facts
# ---------------------------------------------------------------------------

def _setup_resume_facts(profile: dict, template: dict) -> None:
    console.print("\n[bold cyan]Resume Facts[/bold cyan] [dim](preserved exactly during AI tailoring)[/dim]")

    base = template.get("resume_facts", {})

    companies = Prompt.ask("Companies to always keep (comma-separated)", default="")
    projects  = Prompt.ask("Projects to always keep (comma-separated)", default="")
    school    = Prompt.ask("School name(s) to preserve (e.g. York University | Lassonde)", default="")
    metrics   = Prompt.ask("Real metrics to preserve (e.g. 'GPA: 3.7, 99.9% uptime')", default="")

    edu_list = profile.get("education", [])
    gpa_str  = edu_list[0].get("gpa", "") if edu_list else ""
    end_year = edu_list[0].get("end_year", "") if edu_list else ""
    degree   = edu_list[0].get("degree", "") if edu_list else ""

    profile["resume_facts"] = {
        **base,
        "preserved_companies": [s.strip() for s in companies.split(",") if s.strip()],
        "preserved_projects":  [s.strip() for s in projects.split(",") if s.strip()],
        "preserved_school":    school.strip(),
        "graduation_year":     end_year,
        "degree":              degree,
        "gpa":                 gpa_str,
        "real_metrics":        [s.strip() for s in metrics.split(",") if s.strip()],
    }


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _setup_availability(profile: dict) -> None:
    console.print("\n[bold cyan]Availability[/bold cyan]")
    profile["availability"] = {
        "earliest_start_date": Prompt.ask("Earliest start date", default="Immediately"),
    }


# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------

def _setup_searches() -> None:
    console.print(Panel("[bold]Step 4: Job Search Config[/bold]\nWhat you're looking for and where."))

    location     = Prompt.ask("Target location (e.g. 'Canada', 'Remote', 'Toronto, ON')", default="Canada")
    distance_str = Prompt.ask("Search radius in km (0 = remote-only)", default="0")
    try:
        distance = int(distance_str)
    except ValueError:
        distance = 0

    roles_raw = Prompt.ask("Target job titles (comma-separated, e.g. 'Software Engineer Intern, Backend Developer')")
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()] or ["Software Engineer Intern"]

    lines = [
        "# ApplyPilot search configuration",
        "",
        "defaults:",
        f'  location: "{location}"',
        f"  distance: {distance}",
        "  hours_old: 72",
        "  results_per_site: 50",
        "",
        "locations:",
        f'  - location: "{location}"',
        f"    remote: {str(distance == 0).lower()}",
        "",
        "queries:",
    ]
    for i, role in enumerate(roles):
        lines.append(f'  - query: "{role}"')
        lines.append(f"    tier: {min(i + 1, 3)}")

    SEARCH_CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]Search config saved to {SEARCH_CONFIG_PATH}[/green]")


# ---------------------------------------------------------------------------
# API keys + email + browser (.env)
# ---------------------------------------------------------------------------

def _setup_env(profile: dict) -> None:
    console.print(Panel("[bold]Step 5: API Keys & Auto-Apply Config[/bold]"))

    env_lines = ["# ApplyPilot configuration — generated by setup wizard", ""]

    # LLM
    console.print("[bold cyan]LLM Provider[/bold cyan] [dim](powers scoring, tailoring, cover letters)[/dim]")
    provider = Prompt.ask("Provider", choices=["gemini", "openai", "openrouter", "local"], default="gemini")

    if provider == "gemini":
        key   = Prompt.ask("Gemini API key (from aistudio.google.com)")
        model = Prompt.ask("Model", default="gemini-2.0-flash")
        env_lines += [f"GEMINI_API_KEY={key}", f"LLM_MODEL={model}", ""]
    elif provider == "openai":
        key   = Prompt.ask("OpenAI API key")
        model = Prompt.ask("Model", default="gpt-4o-mini")
        env_lines += [f"OPENAI_API_KEY={key}", f"LLM_MODEL={model}", ""]
    elif provider == "openrouter":
        key   = Prompt.ask("OpenRouter API key (from openrouter.ai)")
        model = Prompt.ask("Model (e.g. google/gemini-2.5-flash-lite)", default="google/gemini-2.5-flash-lite")
        env_lines += [
            "LLM_URL=https://openrouter.ai/api/v1",
            f"LLM_API_KEY={key}",
            f"LLM_MODEL={model}",
            "",
        ]
    else:
        url   = Prompt.ask("Local LLM endpoint URL", default="http://localhost:8080/v1")
        model = Prompt.ask("Model name", default="local-model")
        env_lines += [f"LLM_URL={url}", f"LLM_MODEL={model}", ""]

    # Job site password (asked once here, shared with profile.personal.password)
    ap_pass = Prompt.ask("\nJob site account password (used when ApplyPilot creates ATS accounts — make it unique)", password=True, default="")
    if ap_pass:
        env_lines += [f"AP_PASSWORD={ap_pass}", ""]
        profile.setdefault("personal", {})["password"] = ap_pass

    # CapSolver
    console.print("\n[bold cyan]CapSolver[/bold cyan] [dim](optional — solves CAPTCHAs, ~$2 for hundreds of solves, capsolver.com)[/dim]")
    if Confirm.ask("Configure CapSolver?", default=False):
        caps = Prompt.ask("CapSolver API key")
        env_lines += [f"CAPSOLVER_API_KEY={caps}", ""]

    # Browser
    _setup_browser(env_lines)

    # IMAP email
    console.print("\n[bold cyan]Email — for OTP and verification codes during auto-apply[/bold cyan]")
    console.print("[dim]Supports any IMAP provider:[/dim]")
    console.print("[dim]  iCloud  →  imap.mail.me.com  / smtp.mail.me.com[/dim]")
    console.print("[dim]  Gmail   →  imap.gmail.com    / smtp.gmail.com    (enable IMAP + App Password)[/dim]")
    console.print("[dim]  Outlook →  outlook.office365.com / smtp.office365.com[/dim]")
    console.print("[dim]  Other   →  check your provider's IMAP settings[/dim]")
    if Confirm.ask("Configure email access?", default=True):
        email_addr = Prompt.ask("Email address")
        email_pass = Prompt.ask("App password or email password", password=True)

        # Suggest IMAP host based on domain
        domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
        imap_defaults = {
            "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com"),
            "me.com":     ("imap.mail.me.com", "smtp.mail.me.com"),
            "mac.com":    ("imap.mail.me.com", "smtp.mail.me.com"),
            "gmail.com":  ("imap.gmail.com", "smtp.gmail.com"),
            "googlemail.com": ("imap.gmail.com", "smtp.gmail.com"),
            "outlook.com":    ("outlook.office365.com", "smtp.office365.com"),
            "hotmail.com":    ("outlook.office365.com", "smtp.office365.com"),
            "live.com":       ("outlook.office365.com", "smtp.office365.com"),
            "yahoo.com":  ("imap.mail.yahoo.com", "smtp.mail.yahoo.com"),
            "fastmail.com": ("imap.fastmail.com", "smtp.fastmail.com"),
        }
        default_imap, default_smtp = imap_defaults.get(domain, ("imap.mail.me.com", "smtp.mail.me.com"))

        imap_host = Prompt.ask("IMAP host", default=default_imap)
        smtp_host = Prompt.ask("SMTP host", default=default_smtp)
        env_lines += [
            f"EMAIL_ADDRESS={email_addr}",
            f"EMAIL_PASSWORD={email_pass}",
            f"EMAIL_IMAP_HOST={imap_host}",
            f"EMAIL_SMTP_HOST={smtp_host}",
            "",
        ]

    ENV_PATH.write_text("\n".join(env_lines), encoding="utf-8")
    console.print(f"\n[green]Configuration saved to {ENV_PATH}[/green]")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def _review_summary(profile: dict) -> str:
    """Return a multi-line string summarising what's been collected so far."""
    p = profile.get("personal", {})
    edu = (profile.get("education") or [{}])[0]
    exp = profile.get("experience", {})
    sb  = profile.get("skills_boundary", {})
    all_skills = (
        sb.get("programming_languages", [])
        + sb.get("frameworks", [])
        + sb.get("tools", [])
    )

    resume_status = "✓ " + RESUME_PATH.name if RESUME_PATH.exists() else "✗ not set"
    if RESUME_PDF_PATH.exists() and not RESUME_PATH.exists():
        resume_status = "✓ " + RESUME_PDF_PATH.name + " (PDF only)"

    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if "GEMINI_API_KEY" in env_text:
        llm_status = "Gemini"
    elif "OPENAI_API_KEY" in env_text:
        llm_status = "OpenAI"
    elif "LLM_URL" in env_text:
        llm_status = "OpenRouter/local"
    else:
        llm_status = "✗ not set"
    email_status = "✓ set" if "EMAIL_ADDRESS" in env_text else "✗ not set"
    browser_status = "✓ set" if "CHROME_PATH" in env_text else "✗ not set"

    searches_status = "✓ set" if SEARCH_CONFIG_PATH.exists() else "✗ not set"

    lines = [
        f"  [bold]1. Resume    [/bold] {resume_status}",
        f"  [bold]2. Personal  [/bold] {p.get('full_name', '—')}  {p.get('email', '')}",
        f"  [bold]3. Education [/bold] {edu.get('degree', '—')}, {edu.get('institution', '—')}, GPA {edu.get('gpa', '—')}",
        f"  [bold]4. Profile   [/bold] {exp.get('target_role', '—')}, {len(all_skills)} skills",
        f"  [bold]5. Searches  [/bold] {searches_status}",
        f"  [bold]6. API/Email [/bold] LLM: {llm_status}  Email: {email_status}  Browser: {browser_status}",
    ]
    return "\n".join(lines)


def _save_profile(profile: dict) -> None:
    PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")


def run_wizard() -> None:
    console.print()
    console.print(
        Panel.fit(
            "[bold green]ApplyPilot Setup Wizard[/bold green]\n\n"
            "This will create your configuration at:\n"
            f"  [cyan]{APP_DIR}[/cyan]\n\n"
            "You can re-run this anytime with [bold]applypilot init[/bold].",
            border_style="green",
        )
    )

    ensure_dirs()
    console.print(f"[dim]Created {APP_DIR}[/dim]\n")

    template = _load_template()
    profile: dict = {}
    for key in ("tailoring_instructions", "eeo_voluntary"):
        if key in template:
            profile[key] = template[key]

    def run_section(n: int) -> None:
        console.print()
        if n == 1:
            _setup_resume()
        elif n == 2:
            _setup_personal(profile)
        elif n == 3:
            _setup_education(profile)
            _setup_work_auth(profile)
            _setup_compensation(profile)
            _setup_experience(profile)
            _setup_skills(profile)
            _setup_resume_facts(profile, template)
            _setup_availability(profile)
        elif n == 4:
            _setup_searches()
        elif n == 5:
            _setup_env(profile)
        _save_profile(profile)

    # Run all sections once
    for i in range(1, 6):
        run_section(i)

    # Review loop — redo any section without restarting
    while True:
        console.print()
        console.print(
            Panel.fit(
                "[bold]Review[/bold]\n\n"
                + _review_summary(profile)
                + "\n\n[dim]Enter a number (1-6) to redo that section, or press Enter to finish.[/dim]",
                border_style="cyan",
            )
        )
        choice = Prompt.ask("Redo section [1-6] or finish", default="").strip()
        if not choice:
            break
        if choice.isdigit() and 1 <= int(choice) <= 5:
            run_section(int(choice))
        elif choice == "6":
            run_section(5)  # section 6 in display = env (index 5)
        else:
            console.print("[yellow]Enter a number 1–6 or press Enter.[/yellow]")

    from applypilot.config import get_tier, TIER_LABELS, TIER_COMMANDS
    tier = get_tier()

    tier_lines: list[str] = []
    for t in range(1, 4):
        label = TIER_LABELS[t]
        cmds = ", ".join(f"[bold]{c}[/bold]" for c in TIER_COMMANDS[t])
        if t <= tier:
            tier_lines.append(f"  [green]✓ Tier {t} — {label}[/green]  ({cmds})")
        elif t == tier + 1:
            tier_lines.append(f"  [yellow]→ Tier {t} — {label}[/yellow]  ({cmds})")
        else:
            tier_lines.append(f"  [dim]✗ Tier {t} — {label}  ({cmds})[/dim]")

    console.print(
        Panel.fit(
            "[bold green]Setup complete![/bold green]\n\n"
            f"[bold]Your tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]\n\n"
            + "\n".join(tier_lines)
            + "\n\n[dim]Edit profile.json to customize locked bullets, project descriptions, and skills profiles.[/dim]",
            border_style="green",
        )
    )
