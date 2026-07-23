"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
import re
from pathlib import Path

from applypilot.config import TAILORED_DIR

log = logging.getLogger(__name__)

# ── One-page fitting ─────────────────────────────────────────────────────
# The template has no awareness of content length -- a short resume leaves
# visible whitespace, a long one silently spills onto a second PDF page
# with an arbitrary mid-section break. Fixed by measuring actual rendered
# height and correcting with CSS zoom (not `transform: scale`) -- zoom
# triggers real layout reflow, so Chromium's print pagination is computed
# on the *post-zoom* box and actually avoids/creates the page break as
# needed. `transform` is purely visual; print engines compute page breaks
# on the pre-transform layout, so it would not prevent overflow at all.

# Must match the @page rule in build_html()'s CSS.
_PAGE_HEIGHT_IN = 11.0
_PAGE_MARGIN_TOP_IN = 0.35
_PAGE_MARGIN_BOTTOM_IN = 0.35
_CSS_DPI = 96  # standard CSS px-per-inch, matches Chromium's layout viewport
_AVAILABLE_HEIGHT_PX = (_PAGE_HEIGHT_IN - _PAGE_MARGIN_TOP_IN - _PAGE_MARGIN_BOTTOM_IN) * _CSS_DPI

# How far content can be compressed/expanded to fit one page. Beyond these,
# force-fitting would make the resume look either obviously stretched or
# cramped -- better to leave genuinely-too-long content spilling onto a
# second page (a real, visible signal the tailoring stage generated too
# much) than produce something that reads as artificially squeezed.
_MIN_DENSITY = 0.82
_MAX_DENSITY = 1.18
_DENSITY_TOLERANCE = 0.03  # skip the zoom correction if already within 3% of a perfect fit


# ── Resume Parser ────────────────────────────────────────────────────────

def _is_section_header(stripped: str) -> bool:
    """True if a line looks like an ALL-CAPS section header (EDUCATION, EXPERIENCE, etc.)."""
    return bool(
        stripped
        and stripped == stripped.upper()
        and not stripped.startswith("-")
        and len(stripped) > 3
        and not stripped.startswith("•")
    )


def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a single-line header (name, email, phone, location, GitHub)
    followed by ALL-CAPS section headers (EDUCATION, EXPERIENCE, SKILLS, PROJECTS).
    There is no standalone title or summary line in the current format.

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: lines before the first ALL-CAPS section header
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if _is_section_header(line.strip()):
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    # Header is a single combined line: name + 3 spaces + email | phone | city, prov | github
    name = ""
    title = ""
    location = ""
    contact = ""
    if header_lines:
        first = header_lines[0]
        if "   " in first:
            name, contact = first.split("   ", 1)
        else:
            name = first
        # Any additional header lines (shouldn't normally occur) become the title.
        if len(header_lines) > 1:
            title = header_lines[1]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        if _is_section_header(stripped):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The SKILLS section text. Category and value are separated by
            3+ spaces (assemble_resume_text's format), with a colon fallback
            for any older-format text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.+?)\s{2,}(.+)$", line)
        if m:
            skills.append((m.group(1).strip(), m.group(2).strip()))
        elif ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    if "SKILLS" in sections:
        skills = parse_skills(sections["SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = sections["EDUCATION"].strip().replace("\n", "<br>")
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""
    title_html = f'<div class="title">{resume["title"]}</div>' if resume["title"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.35in 0.5in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.35;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    {title_html}
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{edu_html}
{exp_html}
{skills_html}
{proj_html}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Measures the actual rendered content height and searches for a CSS zoom
    level that fills as much of one Letter page as possible without
    overflowing onto a second page -- see the "One-page fitting" comment
    above for why zoom (not a transform) is the right tool.

    Two things that aren't obvious and both cost real debugging time to find:

    1. The viewport must be set to the actual print content width (page
       width minus left/right margins) before measuring. `@page` CSS only
       takes effect inside page.pdf()'s own print pass -- the default
       on-screen viewport is wider, so text wraps onto fewer lines than it
       will when actually printed, making any height measured against it
       meaningless for this purpose.
    2. Zoom's effect on height is not linear -- zooming in can push a line
       across a wrap boundary, adding a full extra line's height in one
       step rather than scaling smoothly. A single computed "target height
       / measured height" factor reliably overshoots into a second page
       (confirmed live: a resume that fit fine at zoom 1.1 spilled onto
       page 2 at the "linearly correct" zoom 1.18). This does a bounded
       binary search instead, testing each candidate for real rather than
       trusting the math -- it converges on the largest zoom that still
       measures within one page, which is the actual goal (fill it as much
       as possible without ever overflowing).

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    # Page content width = Letter width minus the @page rule's left+right
    # margins (0.5in each) -- must match build_html()'s CSS.
    content_width_px = int((8.5 - 0.5 - 0.5) * _CSS_DPI)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": content_width_px, "height": 2000})
        page.set_content(html, wait_until="networkidle")

        baseline_height = page.evaluate("document.body.scrollHeight")
        best_factor = 1.0
        if baseline_height > 0 and abs(_AVAILABLE_HEIGHT_PX / baseline_height - 1.0) > _DENSITY_TOLERANCE:
            lo, hi = _MIN_DENSITY, _MAX_DENSITY
            for _ in range(6):  # 6 iterations narrows the search to well under 1% of the range
                mid = (lo + hi) / 2
                page.evaluate(f"document.body.style.zoom = '{mid}'")
                height = page.evaluate("document.body.scrollHeight")
                if height <= _AVAILABLE_HEIGHT_PX:
                    best_factor = mid  # fits -- record it, then try pushing higher to fill more
                    lo = mid
                else:
                    hi = mid  # overflowed -- back off
            page.evaluate(f"document.body.style.zoom = '{best_factor}'")
            log.info("Fitted content to one page with zoom %.3f (baseline %dpx vs %dpx available)",
                      best_factor, baseline_height, int(_AVAILABLE_HEIGHT_PX))

        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    html = build_html(resume)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
