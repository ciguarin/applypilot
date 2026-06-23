"""Prompt builder for the autonomous job application agent."""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp['salary_expectation']} {currency}")

    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")

    edu_list = p.get("education", [])
    if edu_list:
        edu = edu_list[0]
        status = " (In Progress)" if edu.get("in_progress") else f", Graduated {edu.get('end_year', '')}"
        lines.append(
            f"Education: {edu['degree']} in {edu['field_of_study']}, "
            f"{edu['institution']}, {edu.get('start_year', '')}–{edu.get('end_year', '')}{status}"
        )
        if edu.get("gpa"):
            lines.append(f"GPA: {edu['gpa']}")
        if edu.get("in_progress") and edu.get("end_year"):
            lines.append(f"Projected Completion: April {edu['end_year']} (use Month=April, Year={edu['end_year']} for date pickers)")
    elif exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")
    lines.extend([
        "Age 18+: Yes", "Background Check: Yes", "Felony: No",
        "Previously Worked Here: No", "How Heard: Online Job Board",
    ])

    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- "Remote" or "work from anywhere" -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in another city BUT the posting also says "remote OK" or "remote option available" -> ELIGIBLE. Apply.
- "Onsite only" or "hybrid only" in any city outside the list above with NO remote option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- City is overseas (India, Philippines, Europe, etc.) with no remote option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying. If a screening question reveals it's non-local onsite, answer honestly and let the system reject if needed.
Do NOT fill out forms for jobs that are clearly onsite in a non-acceptable location. Check EARLY, save time."""


def _build_salary_section(profile: dict) -> str:
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor)
    conversion_note = comp.get("currency_conversion_note", "")

    try:
        floor_int = int(floor)
        examples = [
            (f"${floor_int // 1000}K", floor_int // 2080),
            (f"${(floor_int + 25000) // 1000}K", (floor_int + 25000) // 2080),
            (f"${(floor_int + 55000) // 1000}K", (floor_int + 55000) // 2080),
        ]
        hourly_line = ", ".join(f"{sal} = ${hr}/hr" for sal, hr in examples)
    except (ValueError, TypeError):
        hourly_line = "Divide annual salary by 2080"

    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    return f"""== SALARY (think, don't just copy) ==
${floor} {currency} is the FLOOR. Never go below it.
1. Job posting shows a range? -> Answer with the MIDPOINT.
2. Senior/Staff/Lead/Principal/Architect/level II+? -> Min $110K {currency} or posted midpoint if higher.
3. {convert_line}
4. No salary info? -> Use ${floor} {currency}.
5. Asked for a range? -> Midpoint ±10%. No posted range? -> "${range_min}-${range_max} {currency}".
6. Hourly rate? -> Annual ÷ 2080. ({hourly_line})"""


def _build_screening_section(profile: dict) -> str:
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))
    work_auth = profile["work_authorization"]

    return f"""== SCREENING QUESTIONS ==
Hard facts -> answer truthfully: location ({city}, no relocation), work auth ({work_auth.get('legally_authorized_to_work', 'see profile')}), citizenship, clearance, criminal/background.
Skills -> be confident. {target_role} with {years} years experience. Same-domain tool = YES.
Open-ended -> 2-3 sentences specific to THIS job. Reference something from the JD + a resume achievement. No generic fluff.
EEO -> "Decline to self-identify" for everything."""


def _build_hard_rules(profile: dict) -> str:
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    first_name = personal.get("first_name", full_name.rsplit(" ", 1)[0])
    last_name = personal.get("last_name", full_name.rsplit(" ", 1)[-1])
    middle_name = personal.get("middle_name", "")
    preferred_name = personal.get("preferred_name", first_name.split()[0])
    display_name = f"{preferred_name} {last_name}".strip()

    work_auth_rule = "Work auth: Answer truthfully from profile."
    permit_type = work_auth.get("work_permit_type", "")
    sponsorship = work_auth.get("require_sponsorship", "")
    if permit_type:
        work_auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}."

    middle_rule = f' Middle name = {middle_name} (only enter if field explicitly asks; do NOT use as first name).' if middle_name else ""
    name_rule = (
        f'Name fields: First = "{first_name}", Last = "{last_name}".{middle_rule}'
        f' Preferred/goes-by = "{preferred_name}" — use this when the form asks for preferred name.'
        f' If the form only has one "Full Name" field, enter "{full_name}".'
        f' NEVER put "{middle_name or preferred_name}" in the First Name field alone.'
    )

    return f"""== HARD RULES ==
1. Never lie about: citizenship, work auth, criminal history, education, clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_captcha_section() -> str:
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    return f"""== CAPTCHA ==
CapSolver key: {capsolver_key or 'NOT SET — use MANUAL FALLBACK for all CAPTCHAs'}
Flow: DETECT → SOLVE via API (createTask → poll → inject) → only MANUAL FALLBACK if errorId > 0.
CapSolver is server-side — it does NOT need to see images. Always try API first regardless of CAPTCHA appearance.

--- DETECT (run after every navigation/click/when stuck) ---
Check hCaptcha BEFORE reCAPTCHA (both use data-sitekey).
browser_evaluate: () => {{{{
  const r = {{}};
  const url = window.location.href;
  const hc = document.querySelector('.h-captcha,[data-hcaptcha-sitekey]');
  if (hc) {{ r.type='hcaptcha'; r.sitekey=hc.dataset.sitekey||hc.dataset.hcaptchaSitekey; }}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"],iframe[src*="hcaptcha.com"]')) {{
    const el=document.querySelector('[data-sitekey]'); if (el) {{ r.type='hcaptcha'; r.sitekey=el.dataset.sitekey; }}
  }}
  if (!r.type) {{ const cf=document.querySelector('.cf-turnstile,[data-turnstile-sitekey]');
    if (cf) {{ r.type='turnstile'; r.sitekey=cf.dataset.sitekey||cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action=cf.dataset.action; if (cf.dataset.cdata) r.cdata=cf.dataset.cdata; }}
  }}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]'))
    {{ r.type='turnstile_script_only'; r.note='Wait 3s and re-detect.'; }}
  if (!r.type) {{ const s=document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{ const m=s.src.match(/render=([^&]+)/); if (m&&m[1]!=='explicit') {{ r.type='recaptchav3'; r.sitekey=m[1]; }} }}
  }}
  if (!r.type) {{ const rc=document.querySelector('.g-recaptcha');
    if (rc) {{ r.type='recaptchav2'; r.sitekey=rc.dataset.sitekey; }}
  }}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{
    const el=document.querySelector('[data-sitekey]'); if (el) {{ r.type='recaptchav2'; r.sitekey=el.dataset.sitekey; }}
  }}
  if (!r.type) {{ const fc=document.querySelector('#FunCaptcha,[data-pkey],.funcaptcha');
    if (fc) {{ r.type='funcaptcha'; r.sitekey=fc.dataset.pkey; }}
  }}
  if (r.type) {{ r.url=url; return r; }}
  return null;
}}}}
null=no CAPTCHA. turnstile_script_only=wait 3s re-detect. Any other type=proceed to SOLVE.

--- SOLVE ---
TASK_TYPE: hcaptcha→HCaptchaTaskProxyLess, recaptchav2→ReCaptchaV2TaskProxyLess, recaptchav3→ReCaptchaV3TaskProxyLess, turnstile→AntiTurnstileTaskProxyLess, funcaptcha→FunCaptchaTaskProxyLess

STEP 1 — CREATE TASK:
browser_evaluate: async () => {{{{
  const r=await fetch('https://api.capsolver.com/createTask',{{method:'POST',headers:{{{{'Content-Type':'application/json'}}}},
    body:JSON.stringify({{clientKey:'{capsolver_key}',task:{{type:'TASK_TYPE',websiteURL:'PAGE_URL',websiteKey:'SITE_KEY'}}}})
  }}); return await r.json();
}}}}
recaptchav3: add "pageAction":"submit". turnstile: add "metadata":{{"action":"...","cdata":"..."}} if found.
errorId>0 → MANUAL FALLBACK.

STEP 2 — POLL (every 3s, max 10x):
browser_evaluate: async () => {{{{
  const r=await fetch('https://api.capsolver.com/getTaskResult',{{method:'POST',headers:{{{{'Content-Type':'application/json'}}}},
    body:JSON.stringify({{clientKey:'{capsolver_key}',taskId:'TASK_ID'}})
  }}); return await r.json();
}}}}
processing→wait 3s poll again. ready→extract: reCAPTCHA/hCaptcha=solution.gRecaptchaResponse, Turnstile=solution.token. errorId>0 or 30s→MANUAL FALLBACK.

STEP 3 — INJECT:
reCAPTCHA v2/v3:
browser_evaluate: () => {{{{
  const token='THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el=>{{el.value=token;el.style.display='block';}});
  if (window.___grecaptcha_cfg) {{ const c=window.___grecaptcha_cfg.clients;
    for (const k in c) {{ const w=(o,d)=>{{if(d>4||!o)return;for(const x in o){{if(typeof o[x]==='function'&&x.length<3)try{{o[x](token);}}catch(e){{}}else if(typeof o[x]==='object')w(o[x],d+1);}}}};w(c[k],0); }}
  }}
  return 'injected';
}}}}
hCaptcha:
browser_evaluate: () => {{{{
  const token='THE_TOKEN';
  const ta=document.querySelector('[name="h-captcha-response"],textarea[name*="hcaptcha"]'); if(ta)ta.value=token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f=>f.setAttribute('data-hcaptcha-response',token));
  return 'injected';
}}}}
Turnstile:
browser_evaluate: () => {{{{
  const token='THE_TOKEN';
  const inp=document.querySelector('[name="cf-turnstile-response"],input[name*="turnstile"]'); if(inp)inp.value=token;
  return 'injected';
}}}}
FunCaptcha:
browser_evaluate: () => {{{{
  const token='THE_TOKEN';
  const inp=document.querySelector('#FunCaptcha-Token,input[name="fc-token"]'); if(inp)inp.value=token;
  return 'injected';
}}}}
After inject: wait 2s, snapshot. Widget gone=success. No change=click Submit. Still stuck=token expired, re-run STEP 1.

--- MANUAL FALLBACK (only if CapSolver errorId > 0) ---
Audio challenge: look for audio/accessibility button. Text/logic puzzles: solve yourself. All else: RESULT:CAPTCHA."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False) -> str:
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    # Copy to current/ for reference, but worker dir is the accessible path
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    # Worker-specific copy — Playwright MCP is sandboxed to the worker dir
    worker_id = job.get("_worker_id", 0)
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    worker_dir.mkdir(parents=True, exist_ok=True)
    worker_pdf = worker_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(worker_pdf))
    pdf_path = str(worker_pdf)

    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required: 2 sentences — "
            f"(1) relevant experience matching this role, (2) available immediately, based in {city}."
        )
    else:
        cl_display = cover_letter_text

    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = personal.get("last_name", full_name.split()[-1] if " " in full_name else "")
    display_name = f"{preferred_name} {last_name}".strip()

    if dry_run:
        submit_instruction = "Do NOT click Submit. Review all fields, then output RESULT:APPLIED (dry run)."
    else:
        submit_instruction = "Before Submit: snapshot and verify EVERY field matches profile and resume — name, email, phone, location, work auth, resume uploaded. Fix anything wrong FIRST."

    prompt = f"""You are an autonomous job application agent. Submit this application.

== TOOLS AVAILABLE (do NOT call ToolSearch — all tools are pre-loaded) ==
- Browser: browser_navigate, browser_snapshot, browser_take_screenshot, browser_click, browser_type, browser_fill_form, browser_evaluate, browser_file_upload, browser_press_key, browser_wait_for, browser_scroll, browser_tabs, browser_run_code_unsafe
- Email (for OTP/verification only): email:list_emails, email:get_email, email:search_emails, email:move_email
Use these directly. NEVER call ToolSearch. NEVER call email:list_accounts — it is not needed and wastes a turn.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF: {pdf_path}
Cover Letter PDF: {cl_upload_path or 'N/A'}

== RESUME TEXT ==
{tailored_resume}

== COVER LETTER ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

{hard_rules}

== NEVER DO (output RESULT:FAILED immediately) ==
- Camera/mic/screen/location permissions requested -> RESULT:FAILED:unsafe_permissions
- Video/audio verification, selfie, ID photo, biometrics -> RESULT:FAILED:unsafe_verification
- Freelancing platform (Mercor, Toptal, Upwork, Fiverr, Turing) -> RESULT:FAILED:not_a_job_application
- Hourly/contract/rate-setting flows (full-time salaried only)
- SSO login (Google/Microsoft OAuth) -> RESULT:FAILED:sso_required
- Payment info, bank details, SSN/SIN

{salary_section}

{screening_section}

== STEPS ==
1. Check job URL. If it starts with "/" (relative path, no domain) -> RESULT:FAILED:bad_url immediately. Do NOT guess domains.
2. browser_navigate to job URL.
3. browser_snapshot. Run CAPTCHA DETECT. Solve if found.
4. Click Apply. If email-only: send_email subject "Application for {job['title']} — {display_name}", body=2-3 sentence pitch + contact, attach "{pdf_path}". Output RESULT:APPLIED.
   After clicking Apply: run CAPTCHA DETECT.
6. Login wall?
   6a. URL is {', '.join(blocked_sso)} or any SSO/OAuth -> RESULT:FAILED:sso_required.
   6b. New tab/popup (browser_tabs list)? Switch to it. SSO URL -> RESULT:FAILED:sso_required.
   6c. Regular login: {personal['email']} / {personal.get('password', '')}
   6d. After Login click: run CAPTCHA DETECT.
   6e. Login failed (wrong password / "invalid credentials" / "email already registered")?
       IMPORTANT: Do NOT invent alternative passwords or try variants. Do NOT create a new account with a different email.
       - Look for "Forgot password", "Reset password", or "Trouble signing in" link and click it immediately.
       - Enter {personal['email']} and submit the reset form.
       - Wait 10s, then search_emails for a password reset email from the site's domain.
       - Use get_email to get the reset link. Navigate to it. Set new password to {personal.get('password', '')}.
       - Return to login, retry 6c with the new password.
       - No "Forgot password" link AND no existing account: try sign up with {personal['email']} / {personal.get('password', '')}.
       - Sign up fails (email taken / already registered): go back to forgot password flow above.
   6f. Email verification/OTP required?
       Do NOT call list_accounts. Go directly:
       - Wait 8s, then call list_emails with limit=10 to get the 10 most recent emails.
       - Find the one from the site's domain (match sender domain, not exact address).
       - Call get_email on that message id to read the full body.
       - OTP/code field visible: extract the numeric code, type it. Then archive:
         move_email(account='default', emailId=<id>, sourceMailbox='INBOX', destinationMailbox='Archive')
       - Verification link (no code field): extract the URL, browser_navigate to it, continue.
         Then archive: move_email(account='default', emailId=<id>, sourceMailbox='INBOX', destinationMailbox='Archive')
       - Nothing relevant in last 10 emails: wait 10s more, call list_emails again once.
       - Still nothing: RESULT:FAILED:login_issue
   6g. Switch back to application tab if needed.
   6h. All failed -> RESULT:FAILED:login_issue.
7. Upload resume: delete existing first, browser_file_upload with PDF path. Always upload fresh.
8. Cover letter field? Text -> paste. File -> upload PDF.
9. Check ALL pre-filled fields. ATS parsers are wrong. Fix "Current Job Title" and everything else against profile.
10. Answer screening questions per rules above.
11. {submit_instruction}
12. After submit: CAPTCHA DETECT. Check for new tabs. Snapshot to confirm "thank you" / "application received".
13. Output RESULT.

== RESULT CODES ==
RESULT:APPLIED | RESULT:EXPIRED | RESULT:CAPTCHA | RESULT:LOGIN_ISSUE
RESULT:FAILED:not_eligible_location | RESULT:FAILED:not_eligible_work_auth | RESULT:FAILED:reason

== BROWSER EFFICIENCY ==
- browser_snapshot once per page for element refs. browser_take_screenshot to check results (10x less data).
- Snapshot again only when you need new refs.
- Fill ALL fields in ONE browser_fill_form call.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each page, fill all, click Next. Repeat to final review.
- After any navigation/Apply/Submit/Login click or when stuck: run CAPTCHA DETECT. Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) block silently with no visual widget.

== FORM TRICKS ==
- New tab opened? browser_tabs list/select. Always check after login/apply/sign-in clicks.
- Workday/Lever pre-fill page: click upload area, browser_file_upload, wait for parse, click Next.
- Dropdowns — NEVER type verbatim and press Enter. Use this exact flow:
  1. Click the dropdown/input to open it.
  2. Type 2-3 characters of the target value to filter. browser_snapshot to see filtered results.
  3. If matching options appear: click the closest one (fuzzy OK — "Job Board"="Online Job Board", "Decline"="Prefer not to say", "Not a Veteran"="I am not a protected veteran").
  4. If no results after typing: clear the field (select-all + delete), then browser_snapshot to read ALL available options, then click the closest match.
  5. Never leave a required dropdown empty. If nothing fits, pick the most neutral/generic option available.
- Checkbox won't check? browser_click it directly.
- Phone with country prefix: type digits only: {phone_digits}
- Canadian postal codes: strip spaces before typing (M1P 4V4 -> M1P4V4). If it's a lookup dropdown: type the full 6 chars (M1P4V4) to filter first — if exact match appears, click it. If no exact match, clear and type just the FSA (first 3 chars, e.g. M1P) to get nearby options, then click the closest result.
- "How Did You Hear About Us?" / "Source" / "How did you find this job?": open the dropdown, pick the closest option to "Online Job Board" (e.g. "Job Board", "Indeed", "LinkedIn", "Internet/Online"). Never leave it blank.
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors? Take snapshot AND screenshot. Fix all, retry.
- React/SPA forms (Ashby, Lever, Greenhouse): if fields show as empty on submit despite being typed, the framework didn't register the input. After typing, dispatch events via JS: browser_evaluate: (ref) => {{ const el = document.querySelector('input[name="..."]') || document.activeElement; el.dispatchEvent(new Event('input', {{bubbles:true}})); el.dispatchEvent(new Event('change', {{bubbles:true}})); }} then re-snapshot to confirm value is set.

{captcha_section}

== GIVE UP WHEN ==
- Same page after 3 attempts -> RESULT:FAILED:stuck

== VALIDATION ERRORS ==
NEVER skip a field marked red or "Fields to fix: N" and proceed to Submit — the form will always block you.
NEVER cancel an education/experience form you've opened mid-fill — finish it completely before closing.
NEVER delete an education entry to avoid filling it — education is always required. Add it back if deleted.
NEVER submit without a completed education entry.
If an edit dialog won't open after 2 tries: browser_snapshot to get fresh element refs, then try clicking the pencil/edit/checkmark icon by ref. If still stuck after 3 attempts total, delete the entry and re-add it from scratch — but fill the new one completely.
For date pickers / Month+Year dropdowns that won't respond to click or type, force-set via JS:
browser_evaluate: () => {{
  const selects = document.querySelectorAll('select');
  selects.forEach(s => {{
    const label = (s.getAttribute('aria-label') || s.id || '').toLowerCase();
    if (label.includes('start') && label.includes('month')) {{ s.value = Array.from(s.options).find(o => o.text.includes('September') || o.text === '9' || o.value === '9')?.value || '9'; s.dispatchEvent(new Event('change', {{bubbles:true}})); }}
    if (label.includes('start') && label.includes('year')) {{ s.value = '2025'; s.dispatchEvent(new Event('change', {{bubbles:true}})); }}
    if ((label.includes('end') || label.includes('projected') || label.includes('completion') || label.includes('graduation')) && label.includes('month')) {{ s.value = Array.from(s.options).find(o => o.text.includes('April') || o.text === '4' || o.value === '4')?.value || '4'; s.dispatchEvent(new Event('change', {{bubbles:true}})); }}
    if ((label.includes('end') || label.includes('projected') || label.includes('completion') || label.includes('graduation')) && label.includes('year')) {{ s.value = '2029'; s.dispatchEvent(new Event('change', {{bubbles:true}})); }}
  }});
  return 'done';
}}
Run this JS before trying manual clicks on date fields. Then snapshot to verify. Only fall back to manual clicks if JS had no effect.
Graduation/completion = April 2029. Start = September 2025.
- Job closed/expired -> RESULT:EXPIRED
- Page broken/500 -> RESULT:FAILED:page_error"""

    return prompt
