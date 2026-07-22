"""Deterministic Playwright filler for Workday application flows.

No Claude/LLM subprocess involved. Connects to the worker's existing Chrome
instance over CDP (the same one launched by chrome.py for the agent path) and
drives the standard Workday "apply" wizard directly.

Workday's DOM is unusually stable across tenants -- every company runs the
same underlying product (myworkdayjobs.com), just re-skinned. The landing
page, sign-in/create-account form, and step names below were verified live
against real postings across multiple tenants during development (CAE,
Motorola/Avigilon, and others), including tenant-specific variations like
an extra consent checkbox on Create Account and a Google/LinkedIn/email
chooser in front of the sign-in form.

Anything this module doesn't recognize -- an unexpected step, a required
field it can't confidently match, a CAPTCHA, a free-text screening question
-- raises NeedsAgent so the caller falls back to the full Claude Code agent
for that one job. The goal is to keep the deterministic path narrow and
correct rather than broad and fragile.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from applypilot.apply import email_verify

logger = logging.getLogger(__name__)

STEP_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 30_000

# Never fill this -- it's a bot-trap honeypot field, invisible to real users.
# Workday flags the submission as automated if it's touched.
HONEYPOT_ID = "beecatcher"


class NeedsAgent(Exception):
    """Raised when the deterministic filler hits something it can't handle
    confidently. Caller should fall back to the full LLM agent for this job."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class WorkdayResult:
    status: str  # "applied" | "needs_agent"
    reason: str = ""


def is_workday(url: str) -> bool:
    return bool(url) and "myworkdayjobs.com" in url.lower()


def _format_postal_code(value: str) -> str:
    """Insert the space Workday's Canadian postal code validation requires
    (e.g. "M1P4V4" -> "M1P 4V4"). Profile data isn't guaranteed to already
    have it -- leave anything else (US ZIP, other countries) untouched."""
    compact = value.replace(" ", "")
    if len(compact) == 6 and re.fullmatch(r"[A-Za-z]\d[A-Za-z]\d[A-Za-z]\d", compact):
        return f"{compact[:3]} {compact[3:]}"
    return value


def _click(page: Page, automation_id: str, timeout: int = STEP_TIMEOUT_MS) -> None:
    """Click a Workday element by data-automation-id.

    Workday wraps several buttons (sign-in/create-account submit especially)
    in a reCAPTCHA badge overlay (data-automation-id="click_filter") that
    intercepts pointer events until the captcha script settles. A real click
    goes through fine in a real browser; Playwright's strict actionability
    check just keeps retrying against it. Give the normal click a short
    window, then force through the overlay rather than hanging the full
    default timeout for something that isn't actually blocked.
    """
    sel = f'[data-automation-id="{automation_id}"]'
    page.wait_for_selector(sel, timeout=timeout, state="visible")
    try:
        page.click(sel, timeout=5000)
    except PWTimeoutError:
        page.click(sel, timeout=timeout, force=True)


def _fill(page: Page, automation_id: str, value: str, timeout: int = STEP_TIMEOUT_MS) -> None:
    sel = f'[data-automation-id="{automation_id}"]'
    page.wait_for_selector(sel, timeout=timeout, state="visible")
    page.fill(sel, value)


def _exists(page: Page, automation_id: str) -> bool:
    return page.locator(f'[data-automation-id="{automation_id}"]').count() > 0


def _current_step_name(page: Page, retries: int = 6) -> str:
    """Read the active step label from the progress bar, e.g. 'My Information'.

    The progress bar can take a moment to hydrate right after a client-side
    navigation (post-sign-in redirect, Next click) -- retry briefly before
    reporting "unknown" rather than bailing on a one-off timing miss.
    """
    for attempt in range(retries):
        loc = page.locator('[data-automation-id="progressBarActiveStep"]')
        if loc.count() > 0:
            text = loc.first.inner_text()
            # Text looks like "current step 3 of 8\nMy Information" -- take the last line.
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if lines:
                return lines[-1]
        if attempt < retries - 1:
            page.wait_for_timeout(1000)
    return ""


def _reveal_email_signin(page: Page) -> None:
    """Some tenants (CAE) land on a Google/LinkedIn/email chooser instead of
    the email+password form directly (Motorola skips straight to the form).
    Reveal it if present -- this chooser can also reappear after clicking an
    email verification link, not just on the initial landing page."""
    if _exists(page, "SignInWithEmailButton"):
        _click(page, "SignInWithEmailButton")
        page.wait_for_timeout(1000)


def _attempt_sign_in(page: Page, email: str, password: str) -> bool:
    """Fill and submit the Sign In form, waiting out the client-side redirect.

    Returns True if the wizard advanced past auth (step is no longer Sign
    In/Create Account), False if the credentials didn't work or the form
    never appeared.
    """
    _reveal_email_signin(page)
    try:
        page.wait_for_selector(
            '[data-automation-id="signInSubmitButton"]', timeout=STEP_TIMEOUT_MS, state="visible",
        )
    except PWTimeoutError:
        return False

    _fill(page, "email", email)
    _fill(page, "password", password)
    _click(page, "signInSubmitButton")
    # Sign-in redirects client-side back to the job apply page -- can take
    # longer than a fixed short wait to settle.
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        pass
    page.wait_for_timeout(1000)

    step = _current_step_name(page)
    return bool(step) and "Sign In" not in step and "Create Account" not in step


def _attempt_password_reset(page: Page, email: str, new_password: str) -> bool:
    """Use Workday's Forgot Password flow to set the account's password to
    match the profile's, then sign in with it.

    Only call this when the account is already known to exist (e.g. a
    duplicate-email bounce-back from Create Account) and sign-in with the
    profile's stored password failed -- most likely because the account was
    created before profile.json's password field was last changed.
    Retrieves the reset link the same deterministic IMAP way as the signup
    verification link. Confirmed live: the request-reset and set-new-password
    forms reuse the same "resetPasswordButton" automation id.
    """
    if not _exists(page, "forgotPasswordLink"):
        return False
    _click(page, "forgotPasswordLink")
    try:
        page.wait_for_selector(
            '[data-automation-id="resetPasswordButton"]', timeout=STEP_TIMEOUT_MS, state="visible",
        )
    except PWTimeoutError:
        return False

    _fill(page, "email", email)
    baseline_uid = email_verify.get_latest_uid()
    _click(page, "resetPasswordButton")
    page.wait_for_timeout(2000)

    tenant_host = re.match(r"https?://([^/]+)", page.url)
    domain_hint = tenant_host.group(1) if tenant_host else "myworkdayjobs.com"
    try:
        link = email_verify.find_verification_link(domain_hint, min_uid=baseline_uid)
    except email_verify.VerificationNotFound:
        return False

    page.goto(link, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
    try:
        page.wait_for_selector(
            '[data-automation-id="resetPasswordButton"]', timeout=STEP_TIMEOUT_MS, state="visible",
        )
    except PWTimeoutError:
        return False

    _fill(page, "password", new_password)
    if _exists(page, "verifyPassword"):
        _fill(page, "verifyPassword", new_password)
    _click(page, "resetPasswordButton")
    page.wait_for_timeout(2000)

    return _attempt_sign_in(page, email, new_password)


def _handle_signin_or_create_account(page: Page, email: str, password: str) -> None:
    """Try signing in with an existing account; fall back to creating one.

    Never touches the beecatcher honeypot field.
    """
    _reveal_email_signin(page)

    # Workday defaults to the Sign In tab -- try it first with the profile's
    # credentials before assuming this is a brand-new signup. An account can
    # already exist for this tenant (a prior real application, or leftover
    # dev/test signups); Create Account silently bounces a duplicate email
    # back to Sign In with no inline error at all (confirmed live), which is
    # exactly what tripped up both this module and the full agent on a live
    # Motorola re-run.
    if _exists(page, "signInSubmitButton") and _attempt_sign_in(page, email, password):
        return

    if _exists(page, "createAccountLink"):
        _click(page, "createAccountLink")

    try:
        page.wait_for_selector(
            '[data-automation-id="createAccountSubmitButton"]', timeout=STEP_TIMEOUT_MS, state="visible",
        )
    except PWTimeoutError:
        raise NeedsAgent("unexpected sign-in/create-account page structure")

    _fill(page, "email", email)
    _fill(page, "password", password)
    if _exists(page, "verifyPassword"):
        _fill(page, "verifyPassword", password)

    # Some tenants (not all -- CAE didn't have this, Motorola/Avigilon did)
    # require a terms/privacy consent checkbox before Create Account will
    # proceed. Agreeing to a site's own ToS to create an account is not a
    # judgment call -- always check it when present.
    if _exists(page, "createAccountCheckbox"):
        checkbox = page.locator('[data-automation-id="createAccountCheckbox"]')
        if not checkbox.first.is_checked():
            checkbox.first.check(force=True)

    # Capture the inbox baseline before submitting, so the verification
    # email we wait for below can't accidentally match a stale one already
    # sitting in the inbox from an earlier, expired signup attempt.
    email_baseline_uid = email_verify.get_latest_uid()
    _click(page, "createAccountSubmitButton")
    page.wait_for_timeout(3000)

    # Three possible outcomes after submitting Create Account. Order matters:
    # check the explicit "verify your account" text first (genuinely new
    # account), since a bare "back on Sign In" or "/login" URL check alone
    # can't distinguish a real verification redirect from a duplicate-email
    # bounce-back -- this tenant's auth pages live under /login regardless.
    # Checked against rendered visible text, not page.content() -- this SPA
    # leaves stale/hidden aria-live and error-banner nodes in the raw HTML
    # from earlier states in the same session, which false-positive matched
    # "verify your account" even when nothing was actually shown.
    if "verify your account" in page.inner_text("body").lower():
        tenant_host = re.match(r"https?://([^/]+)", page.url)
        domain_hint = tenant_host.group(1) if tenant_host else "myworkdayjobs.com"
        try:
            link = email_verify.find_verification_link(domain_hint, min_uid=email_baseline_uid)
        except email_verify.VerificationNotFound as e:
            raise NeedsAgent(f"email verification link not found: {e}")
        page.goto(link, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        if not _attempt_sign_in(page, email, password):
            raise NeedsAgent("could not sign in after verifying account")
        return

    if _exists(page, "signInSubmitButton"):
        # Bounced back to Sign In with no error text -- the email is already
        # registered. Try the same credentials first; if they don't work,
        # the account exists under a different password (e.g. created
        # before profile.json's password field was last changed) -- reset
        # it to match rather than handing off, since we already know this
        # account is genuinely ours.
        if _attempt_sign_in(page, email, password):
            return
        if _attempt_password_reset(page, email, password):
            return
        raise NeedsAgent("account already exists but sign-in and password reset both failed")

    if _exists(page, "createAccountSubmitButton"):
        raise NeedsAgent("account creation failed with a validation error")

    raise NeedsAgent("unexpected state after Create Account submission")


def _upload_resume(page: Page, resume_pdf: Path) -> None:
    if _exists(page, "file-upload-successful"):
        return
    try:
        page.wait_for_selector('input[type="file"]', timeout=STEP_TIMEOUT_MS, state="attached")
    except PWTimeoutError:
        raise NeedsAgent("no file upload input found for resume")
    page.locator('input[type="file"]').first.set_input_files(str(resume_pdf))
    page.wait_for_timeout(3000)


def _fill_field(page: Page, suffix: str, value: str, widget: str = "text") -> bool:
    """Fill one field, addressed by its formField-{suffix} wrapper.

    Workday's field wrappers (data-automation-id="formField-X") are stable,
    predictable, and directly named after the field's meaning -- unlike the
    actual <input> elements inside them, which carry no data-automation-id
    at all. Three widget shapes cover the fields this module fills:

      text   -- plain <input>; typed key-by-key rather than .fill(), since
                 some of these (address, postal code, phone) run masking/
                 autocomplete JS that listens for real keystrokes and
                 silently ignores .fill()'s synthetic value assignment
      listbox -- a <button aria-haspopup="listbox"> that opens a floating
                 [role="option"] list on click (province, country code, etc)
      radio  -- a fieldset of <input type="radio"> + sibling <label>; click
                 the label with matching text since the input itself is
                 visually hidden behind custom styling
    """
    wrapper = page.locator(f'[data-automation-id="formField-{suffix}"]')
    if wrapper.count() == 0:
        return False
    try:
        if widget == "text":
            inp = wrapper.locator("input")
            if inp.count() == 0:
                return False
            inp.first.click()
            inp.first.fill("")
            inp.first.press_sequentially(value, delay=20)
            inp.first.blur()
            # Give the field's validation/reflow a moment to commit before
            # the caller moves on to the next field -- firing fills back to
            # back too fast can lose one to the same async-state race as
            # the listbox selection below.
            page.wait_for_timeout(300)
        elif widget == "listbox":
            btn = wrapper.locator('button[aria-haspopup="listbox"]')
            if btn.count() == 0:
                return False
            btn.first.click()
            page.wait_for_timeout(500)
            option = page.get_by_role("option", name=value, exact=True)
            if option.count() == 0:
                page.keyboard.press("Escape")
                return False
            option.first.click()
            # Selecting an option is an async state update -- moving on to
            # the next field immediately can race it and leave the button
            # showing "Select One" again.
            page.wait_for_timeout(400)
        elif widget == "radio":
            label = wrapper.locator(f'label:text-is("{value}")')
            if label.count() == 0:
                return False
            label.first.click()
            page.wait_for_timeout(300)
        else:
            return False
        return True
    except Exception:
        logger.debug("Failed to fill field %s", suffix, exc_info=True)
        return False


def _next_step(page: Page) -> bool:
    """Click the Next/Continue button. Returns False if none found (likely Review page)."""
    for automation_id in ("pageFooterNextButton", "bottom-navigation-next-button", "wizard-next-button"):
        if _exists(page, automation_id):
            _click(page, automation_id)
            page.wait_for_timeout(1500)
            return True
    return False


def _drive_wizard(
    page: Page,
    profile: dict,
    resume_pdf: Path,
    dry_run: bool = False,
) -> WorkdayResult:
    """Drive the wizard steps from wherever `page` currently sits.

    Assumes account creation/sign-in is already done. Split out from
    apply_via_workday so a mid-wizard page (e.g. after a fresh sign-in)
    can be driven directly without re-running account handling.
    """
    personal = profile["personal"]

    # From here the wizard proceeds through: Autofill with Resume (upload) ->
    # My Information -> My Experience -> Application Questions (1-2 pages) ->
    # Voluntary Disclosures -> Review. Loop step-by-step rather than assuming
    # a fixed count, since tenants configure different numbers of pages.
    seen_upload = False
    last_step = None
    max_steps = 12
    for _ in range(max_steps):
        step = _current_step_name(page)
        logger.info("[workday] step: %s", step or "(unknown)")

        if not step:
            raise NeedsAgent("lost track of wizard step (progress bar not found)")

        # If Next was clicked last iteration and we're still on the same
        # step, something didn't fill correctly (very likely a per-tenant
        # field this module doesn't know about) -- bail to the agent rather
        # than retry the same failing click forever.
        if step == last_step:
            errors = page.locator('[data-automation-id="inputAlert"]').count()
            raise NeedsAgent(f"stuck on step {step!r} -- {errors} unresolved field error(s)")
        last_step = step

        if "Resume" in step and not seen_upload:
            _upload_resume(page, resume_pdf)
            seen_upload = True

        elif "My Information" in step or "My Experience" in step:
            _fill_field(page, "addressLine1", personal["address"], "text")
            _fill_field(page, "city", personal["city"], "text")
            _fill_field(page, "postalCode", _format_postal_code(personal["postal_code"]), "text")
            _fill_field(page, "phoneNumber", personal["phone"], "text")
            _fill_field(page, "countryRegion", personal.get("province_state", ""), "listbox")
            _fill_field(page, "candidateIsPreviousWorker", "No", "radio")

        elif "Application Questions" in step:
            work_auth = profile.get("work_authorization", {})
            _fill_field(
                page, "legallyAuthorized",
                "Yes" if work_auth.get("legally_authorized_to_work") else "No", "radio",
            )
            _fill_field(
                page, "requireSponsorship",
                "Yes" if work_auth.get("require_sponsorship") else "No", "radio",
            )

        elif "Voluntary Disclosures" in step or "Self Identify" in step:
            eeo = profile.get("eeo_voluntary", {})
            _fill_field(page, "gender", eeo.get("gender", "Decline to self-identify"), "listbox")
            _fill_field(page, "ethnicity", eeo.get("race_ethnicity", "Decline to self-identify"), "listbox")
            _fill_field(page, "veteranStatus", eeo.get("veteran_status", "Decline to self-identify"), "listbox")
            _fill_field(page, "disability", eeo.get("disability_status", "Decline to self-identify"), "radio")

        elif "Review" in step:
            if dry_run:
                return WorkdayResult(status="applied", reason="dry_run: reached Review page")
            if not _exists(page, "pageFooterNextButton"):
                raise NeedsAgent("no submit button found on Review page")
            _click(page, "pageFooterNextButton")
            page.wait_for_timeout(3000)
            if _exists(page, "applicationConfirmationPage") or "thank" in page.inner_text("body").lower():
                return WorkdayResult(status="applied")
            raise NeedsAgent("submitted but no confirmation detected -- needs verification")

        else:
            raise NeedsAgent(f"unrecognized wizard step: {step!r}")

        if not _next_step(page):
            # No Next button and we're not on Review -- something's off.
            if "Review" not in step:
                raise NeedsAgent(f"no Next button found after step {step!r}")

    raise NeedsAgent("exceeded max step count without reaching Review")


def apply_via_workday(
    page: Page,
    job: dict,
    profile: dict,
    resume_pdf: Path,
    dry_run: bool = False,
) -> WorkdayResult:
    """Drive a Workday application end-to-end with zero LLM involvement.

    Raises NeedsAgent for anything outside the well-verified happy path --
    caller is expected to catch it and fall back to the full agent for that job.
    """
    personal = profile["personal"]
    url = job.get("application_url") or job.get("url", "")

    page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(1500)

    # Cookie banner -- present on most Workday career sites.
    if _exists(page, "legalNoticeAcceptButton"):
        _click(page, "legalNoticeAcceptButton")
        page.wait_for_timeout(500)

    if not _exists(page, "autofillWithResume"):
        raise NeedsAgent("landing page missing 'Autofill with Resume' -- unfamiliar layout")
    _click(page, "autofillWithResume")
    page.wait_for_timeout(2000)

    step = _current_step_name(page)
    if "Sign In" in step or "Create Account" in step:
        _handle_signin_or_create_account(page, personal["email"], personal["password"])
        page.wait_for_timeout(1500)

    return _drive_wizard(page, profile, resume_pdf, dry_run=dry_run)
