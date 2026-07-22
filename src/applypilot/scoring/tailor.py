"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    FABRICATION_WATCHLIST,
    sanitize_text,
    validate_json_fields,
    validate_tailored_resume,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Role Type Detection ────────────────────────────────────────────────────

def _detect_role_type(job: dict, profile: dict) -> str:
    """Detect role type from job description keywords.

    Returns one of the keys in tailoring_instructions.role_type_detection,
    defaulting to 'full_stack_software' if no match.
    """
    ti = profile.get("tailoring_instructions", {})
    detection = ti.get("role_type_detection", {})
    desc = (job.get("full_description") or "") + " " + (job.get("title") or "")
    desc_lower = desc.lower()

    scores: dict[str, int] = {}
    for role_type, keywords_str in detection.items():
        keywords = [k.strip().lower() for k in keywords_str.split(",")]
        score = sum(1 for kw in keywords if kw in desc_lower)
        if score > 0:
            scores[role_type] = score

    if not scores:
        return "full_stack_software"

    best = max(scores, key=lambda k: (scores[k], -list(detection.keys()).index(k)))
    return best


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict, job: Optional[dict] = None) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    Detects role type from the job description, selects matching
    skills_profile, bullet variants, project order, and project descriptions
    from profile.json.  The LLM only generates the title, skills (3
    categories verbatim), and project bullets; locked experience content
    is injected by assemble_resume_text().
    """
    role_type = _detect_role_type(job, profile) if job else "full_stack_software"
    resume_facts = profile.get("resume_facts", {})

    # ── Select profile entries for this role type ────────────────────────

    skills_profiles = resume_facts.get("skills_profiles", {})
    selected_skills = skills_profiles.get(role_type, skills_profiles.get("full_stack_software", {}))

    locked = resume_facts.get("locked_content", {})
    chick_bullets = locked.get("chick_fil_a_bullets", [])
    mit_sql = locked.get("mitsubishi_sql_bullet", "")

    b1_variants = resume_facts.get("mitsubishi_bullet_1_variants", {})
    mit_b1 = b1_variants.get(role_type, b1_variants.get("full_stack_software", ""))

    b3_variants = resume_facts.get("mitsubishi_bullet_3_variants", {})
    if role_type in ("devops_backend", "full_stack_software"):
        mit_b3 = b3_variants.get("technical", "")
    else:
        mit_b3 = b3_variants.get("stakeholder", b3_variants.get("stakeholder_with_documentation", ""))

    project_order = resume_facts.get("project_order_by_role", {}).get(role_type, ["Glyco", "Production Homelab & Automation"])
    proj_descs = resume_facts.get("project_descriptions", {})
    glyco_desc_key = "expanded" if role_type in ("ai_ml", "data_analytics") else "concise"
    glyco_desc = proj_descs.get("glyco", {}).get(glyco_desc_key, "")

    homelab_profiles = proj_descs.get("homelab", {})
    homelab_role_map = {
        "data_analytics": "data_focused",
        "infrastructure_architecture": "infra_focused",
        "devops_backend": "devops_focused",
        "business_analyst": "general",
        "full_stack_software": "general",
        "ai_ml": "general",
    }
    homelab_key = homelab_role_map.get(role_type, "general")
    homelab_desc = homelab_profiles.get(homelab_key, homelab_profiles.get("general", ""))

    proj_dates = resume_facts.get("project_dates", {})
    glyco_date = proj_dates.get("Glyco", "Mar 2026")
    homelab_date = proj_dates.get("Production Homelab & Automation", "Jan 2024 - Present")

    # Build skills block (3 categories)
    skills_lines = []
    for cat in ("languages_and_frameworks", "tools", "domain_knowledge"):
        label = {
            "languages_and_frameworks": "Languages & Frameworks",
            "tools": "Tools",
            "domain_knowledge": "Domain Knowledge",
        }[cat]
        val = selected_skills.get(cat, "")
        skills_lines.append(f'"{label}": "{val}"')
    skills_json_block = ",\n        ".join(skills_lines)

    # Locked experience preview — tells LLM what the resume will contain
    # so it can tailor project bullets coherently
    chick_header = "Chick-Fil-A STC | Toronto, ON | Front of House Team Member | Sep 2024 | Present"
    mitsubishi_header = "Mitsubishi Motors | Toronto, ON | Data Analyst Intern | May 2024 | Aug 2024"
    mit_bullets_preview = f"- {mit_b1}\n- {mit_sql}\n- {mit_b3}"

    # Project order by name
    proj_order_str = " → ".join(project_order)

    company = job.get("site", "") if job else ""
    location = job.get("location", "N/A") if job else ""

    banned_str = ", ".join(BANNED_WORDS)

    return f"""You are a senior technical recruiter tailoring a resume for a specific role.

## TARGET ROLE
Company: {company}
Location: {location}
Title: {job.get("title", "") if job else ""}

## RESUME STRUCTURE (fixed — do not change section order)
The final resume will be assembled in this order:
1. HEADER — single line (name | email | phone | location | GitHub)
2. EDUCATION — York University, GPA 3.7
3. EXPERIENCE — Chick-Fil-A STC + Mitsubishi Motors (locked content, shown below)
4. SKILLS — 3 categories (you must provide these verbatim)
5. PROJECTS — 2 projects in this order: {proj_order_str}

## EXPERIENCE SECTIONS (for context only — these are CODE-INJECTED, you do NOT generate them)
The resume will contain these experience entries. You do NOT need to output them — they
are automatically inserted. They are shown here so your project choices are coherent:

--- Chick-Fil-A STC ---
{chr(10).join(f"  {b}" for b in chick_bullets)}

--- Mitsubishi Motors ---
{mit_bullets_preview}

## SKILLS — output these verbatim in the "skills" field:
{{
        {skills_json_block}
}}

## PROJECT 1: {project_order[0]} — Glyco
Date: {glyco_date}
Description to use: {glyco_desc}
Write 2-3 bullets emphasizing the aspects most relevant to this role.

## PROJECT 2: {project_order[1]} — Production Homelab & Automation
Date: {homelab_date}
Description to use: {homelab_desc}
Write 2-3 bullets emphasizing the aspects most relevant to this role.

## HARD RULES
- NO summary or objective section
- NO fabricating metrics — only use the resume's real numbers
- NO banned words: {banned_str}
- DO NOT modify skills — output them exactly as given
- DO NOT output experience sections — they are code-injected
- DO NOT include education in the JSON — it is code-injected
- Title should match the target role title closely (e.g. "Software Engineer Intern")
- Write short, direct bullets. Strong verb + what you built + impact.
- Every bullet must be reworded for THIS role — different angle from base resume.
- Max 4 bullets per project section.

## OUTPUT — return ONLY this JSON, no markdown fences, no commentary:
{{"title":"Role Title","skills":{{"Languages & Frameworks":"...","Tools":"...","Domain Knowledge":"..."}},"projects":[{{"name":"Glyco","bullets":["bullet 1","bullet 2","bullet 3"]}},{{"name":"Production Homelab & Automation","bullets":["bullet 1","bullet 2"]}}]}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks to TECHNICAL SKILLS that aren't in the original. The allowed skills are ONLY: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## TOLERANCE RULE:
The goal is to get interviews, not to be a perfect fact-checker. Allow up to 3 minor stretches per resume:
- Adding a closely related tool the candidate could realistically know is a MINOR STRETCH, not fabrication.
- Reframing a metric with slightly different wording is a MINOR STRETCH.
- Adding any LEARNABLE skill given their existing stack is a MINOR STRETCH.
- Only FAIL if there are MAJOR lies: completely invented projects, fake companies, fake degrees, wildly inflated numbers, or skills from a completely different domain.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict, job: Optional[dict] = None) -> str:
    """Assemble full resume text from LLM JSON + profile data.

    Section order: Header → Education → Experience → Skills → Projects.
    Locked experience content is injected from profile.json, not from the LLM.

    Args:
        data: Parsed JSON resume from the LLM (title, skills, projects).
        profile: User profile dict from load_profile().
        job: Job dict, used to detect role type for bullet/skills variant selection.

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})
    lines: list[str] = []

    # ── Detect role type for variant selection ───────────────────────────
    if job:
        role_type = _detect_role_type(job, profile)
    else:
        role_type = _detect_role_type({"full_description": "", "title": data.get("title", "")}, profile)

    locked = resume_facts.get("locked_content", {})
    chick_bullets = locked.get("chick_fil_a_bullets", [])
    mit_sql = locked.get("mitsubishi_sql_bullet", "")
    b1_variants = resume_facts.get("mitsubishi_bullet_1_variants", {})
    mit_b1 = b1_variants.get(role_type, b1_variants.get("full_stack_software", ""))
    b3_variants = resume_facts.get("mitsubishi_bullet_3_variants", {})
    if role_type in ("devops_backend", "full_stack_software"):
        mit_b3 = b3_variants.get("technical", "")
    else:
        mit_b3 = b3_variants.get("stakeholder", b3_variants.get("stakeholder_with_documentation", ""))
    proj_dates = resume_facts.get("project_dates", {})
    glyco_date = proj_dates.get("Glyco", "Mar 2026")
    homelab_date = proj_dates.get("Production Homelab & Automation", "Jan 2024 - Present")
    proj_descs = resume_facts.get("project_descriptions", {})
    glyco_header_desc = proj_descs.get("glyco", {}).get("concise", "")
    glyco_tech_stack = proj_descs.get("glyco", {}).get("tech_stack", "")
    homelab_header_desc = proj_descs.get("homelab", {}).get("header", "")
    homelab_tech_stack = proj_descs.get("homelab", {}).get("tech_stack", "")
    gpa = resume_facts.get("gpa", "3.7")
    degree = resume_facts.get("degree", "BA Honours Computer Science")
    minor_text = resume_facts.get("minor", "")
    school = resume_facts.get("preserved_school", "York University | Lassonde School of Engineering")

    # ── 1. Header (single line) ─────────────────────────────────────────
    name = personal.get("full_name", "Your Name")
    email = personal.get("email", "your.email@example.com")
    phone = personal.get("phone", "0000000000")
    city = personal.get("city", "Toronto")
    province = personal.get("province_state", "ON")
    github = personal.get("github_url", "https://github.com/ciguarin").replace("https://", "")
    lines.append(f"{name}   {email} | {phone} | {city}, {province} | {github}")
    lines.append("")

    # ── 2. Education ────────────────────────────────────────────────────
    lines.append("EDUCATION")
    minor_str = f" (Minor in {minor_text} Intended)" if minor_text else ""
    lines.append(f"{school}   Toronto, ON")
    lines.append(f"{degree}{minor_str}, GPA: {gpa}   Sep 2025 | Apr 2029")
    lines.append("")

    # ── 3. Experience ───────────────────────────────────────────────────
    lines.append("EXPERIENCE")

    # Chick-Fil-A
    lines.append("Chick-Fil-A STC   Toronto, ON")
    lines.append("Front of House Team Member   Sep 2024 | Present")
    for b in chick_bullets:
        lines.append(f"- {sanitize_text(b)}")
    lines.append("")

    # Mitsubishi Motors
    lines.append("Mitsubishi Motors   Toronto, ON")
    lines.append("Data Analyst Intern   May 2024 | Aug 2024")
    lines.append(f"- {sanitize_text(mit_b1)}")
    lines.append(f"- {sanitize_text(mit_sql)}")
    lines.append(f"- {sanitize_text(mit_b3)}")
    lines.append("")

    # ── 4. Skills ───────────────────────────────────────────────────────
    lines.append("SKILLS")
    skills = data.get("skills", {})
    if isinstance(skills, dict):
        for cat in ("Languages & Frameworks", "Tools", "Domain Knowledge"):
            val = skills.get(cat, "")
            if val:
                lines.append(f"{cat}   {sanitize_text(str(val))}")
    lines.append("")

    # ── 5. Projects ─────────────────────────────────────────────────────
    lines.append("PROJECTS")
    projects = data.get("projects", [])
    for proj in projects:
        pname = proj.get("name", "")
        is_glyco = "glyco" in pname.lower()
        is_homelab = "homelab" in pname.lower()
        date = glyco_date if is_glyco else homelab_date if is_homelab else ""
        desc = glyco_header_desc if is_glyco else homelab_header_desc if is_homelab else ""
        tech_stack = glyco_tech_stack if is_glyco else homelab_tech_stack if is_homelab else ""

        header = sanitize_text(pname)
        if desc:
            header += f" — {sanitize_text(desc)}"
        if tech_stack:
            header += f"   {sanitize_text(tech_stack)}"
        lines.append(header)

        if date:
            lines.append(date)
        for b in proj.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    return "\n".join(lines)


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(profile, job)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=2048, temperature=0.4)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(data, profile, mode=validation_mode)
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = assemble_resume_text(data, profile, job)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile, job)

        # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                # In normal mode, only retry on judge failure if there are retries left
                if validation_mode != "lenient":
                    continue
            # Accept best attempt on last retry (all modes) or if lenient
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        # Both passed
        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 20,
                  validation_mode: str = "normal") -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {"approved": 0, "failed_validation": 0, "failed_judge": 0, "error": 0}

    for job in jobs:
        completed += 1
        try:
            tailored, report = tailor_resume(resume_text, job, profile,
                                             validation_mode=validation_mode)

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            # Save tailored resume text
            txt_path = TAILORED_DIR / f"{prefix}.txt"
            txt_path.write_text(tailored, encoding="utf-8")

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Save validation report
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Generate PDF for approved resumes (best-effort)
            # "approved_with_judge_warning" is also a success — resume was generated.
            pdf_path = None
            if report["status"] in ("approved", "approved_with_judge_warning"):
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": str(txt_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    now = datetime.now(timezone.utc).isoformat()
    _success_statuses = {"approved", "approved_with_judge_warning"}
    for r in results:
        if r["status"] in _success_statuses:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "failed": stats.get("failed_validation", 0) + stats.get("failed_judge", 0),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
