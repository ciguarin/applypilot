"""Deterministic email polling for account-verification links.

Pure IMAP -- no LLM, no agent, no MCP round-trip. Shared by any platform
filler (Workday today, others later) that needs to clear an email
verification gate without spawning a full Claude Code session for what is
just "read the inbox, find a link, click it."

Reuses the same EMAIL_IMAP_HOST/EMAIL_ADDRESS/EMAIL_PASSWORD credentials
already configured in .env for the agent's email MCP server.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import time
from email.header import decode_header, make_header

logger = logging.getLogger(__name__)


class VerificationNotFound(Exception):
    pass


def _connect() -> imaplib.IMAP4_SSL:
    host = os.environ.get("EMAIL_IMAP_HOST")
    # IMAP login must be the Apple ID, not a custom-domain alias -- iCloud's
    # IMAP server rejects the alias even though mail addressed to it lands
    # in the same inbox. Falls back to EMAIL_ADDRESS for non-iCloud setups.
    username = os.environ.get("EMAIL_IMAP_USERNAME") or os.environ.get("EMAIL_ADDRESS")
    password = os.environ.get("EMAIL_PASSWORD")
    if not (host and username and password):
        raise VerificationNotFound("email credentials not configured (EMAIL_IMAP_HOST/EMAIL_IMAP_USERNAME/PASSWORD)")
    conn = imaplib.IMAP4_SSL(host)
    conn.login(username, password)
    conn.select("INBOX")
    return conn


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
                except Exception:
                    continue
        return "\n".join(parts)
    try:
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
    except Exception:
        return ""


_LINK_KEYWORDS = ("verify", "activate", "confirm", "reset")


def get_latest_uid() -> int:
    """Return the highest UID currently in INBOX, as a baseline cutoff.

    Call this right before triggering an action that will send a new email
    (e.g. submitting Create Account), then pass the result as `min_uid` to
    find_verification_link so a stale/expired email already sitting in the
    inbox from a previous attempt can't be mistaken for a fresh one.
    """
    conn = _connect()
    try:
        _typ, data = conn.uid("search", None, "ALL")
        ids = data[0].split() if data and data[0] else []
        return int(ids[-1]) if ids else 0
    finally:
        conn.logout()


def _archive(conn: imaplib.IMAP4_SSL, msg_uid: bytes, destination: str) -> None:
    """Move a message out of INBOX after it's been consumed.

    Mirrors the agent's own convention (apply/prompt.py: move_email(...,
    destinationMailbox='Archive')) after using an OTP/verification email --
    without this, every account-creation/password-reset/OTP email this
    module reads sits in INBOX forever, and a real personal inbox used for
    dozens of ATS signups gets buried fast. Uses the IMAP MOVE extension
    (RFC 6851), which iCloud supports; falls back to COPY+delete+expunge
    for servers that don't.
    """
    try:
        typ, _ = conn.uid("MOVE", msg_uid, destination)
        if typ == "OK":
            return
    except imaplib.IMAP4.error:
        pass
    # Fallback: copy then mark the original deleted.
    try:
        conn.uid("COPY", msg_uid, destination)
        conn.uid("STORE", msg_uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
    except imaplib.IMAP4.error:
        logger.warning("[email_verify] could not archive message uid=%s to %s", msg_uid, destination)


def find_verification_link(
    domain_hint: str,
    min_uid: int = 0,
    wait_seconds: int = 70,
    poll_interval: int = 15,
    archive_to: str | None = "Verification",
) -> str:
    """Poll INBOX for a verification email matching domain_hint, return the link URL.

    Only considers messages with UID > min_uid -- without this, a stale
    verification email from an earlier attempt (already expired, per
    Workday's "link will expire after 24 hours") can be matched instead of
    the fresh one, since it'd otherwise still be sitting in the last 10
    messages. Pass the result of get_latest_uid() (captured before triggering
    the send) as min_uid.

    Mirrors the ~70s patience window already tuned for the agent's OTP flow
    (see apply/prompt.py) -- real verification emails routinely take longer
    than a few seconds to arrive.

    Once matched, the message is moved out of INBOX to `archive_to` (default
    "Verification", an existing mailbox on this account) so a real personal
    inbox doesn't accumulate every account-verification/password-reset email
    this module ever reads. Pass None to leave it in place.

    Raises VerificationNotFound if nothing matches within the window.
    """
    deadline = time.time() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        conn = _connect()
        try:
            _typ, data = conn.uid("search", None, "ALL")
            ids = [uid for uid in (data[0].split() if data and data[0] else []) if int(uid) > min_uid]
            for msg_uid in reversed(ids):
                # iCloud's IMAP server acks plain RFC822 fetches with no
                # literal in this client -- BODY.PEEK[] returns the full
                # message and doesn't mark it \Seen.
                typ, msg_data = conn.uid("fetch", msg_uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                body = _extract_body(msg)
                if domain_hint.lower() not in body.lower():
                    continue
                urls = re.findall(r'https?://[^\s"\'<>\]]+', body)
                candidates = [u for u in urls if any(k in u.lower() for k in _LINK_KEYWORDS)]
                if candidates:
                    logger.info("[email_verify] found verification link on attempt %d", attempt)
                    if archive_to:
                        _archive(conn, msg_uid, archive_to)
                    return candidates[0]
        finally:
            conn.logout()

        if time.time() >= deadline:
            raise VerificationNotFound(
                f"no verification email matching {domain_hint!r} newer than uid {min_uid} within {wait_seconds}s"
            )
        time.sleep(poll_interval)


# ── Inbox sweep ──────────────────────────────────────────────────────────
# Backstop for apply/prompt.py's own move_email-after-use instruction on the
# agent path. The agent follows it most of the time (Archive already holds
# ~1200 messages), but LLM instruction-following isn't 100%, and a stray
# OTP/security-code email has no reason to sit in a real personal inbox once
# consumed. This is pure IMAP + string matching -- no LLM, so it always runs.

_ATS_EMAIL_DOMAINS = (
    "myworkday.com", "workday.com", "myworkdayjobs.com",
    "greenhouse-mail.io", "greenhouse.io",
    "bamboohr.com",
    "icims.com",
    "taleo.net",
    "smartrecruiters.com",
    "lever.co",
    "ashbyhq.com",
    "applytojob.com",
    "ultipro.com", "ukg.com",
    "successfactors.com",
    "workable.com",
    "jobvite.com",
)

_VERIFICATION_SUBJECT_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"verify your (candidate )?account",
    r"security code",
    r"verification code",
    r"confirm your identity",
    r"one[- ]time (passcode|code|password)",
    r"\botp\b",
    r"reset your password",
))


def _is_ats_sender(sender: str) -> bool:
    sender_lower = sender.lower()
    return any(domain in sender_lower for domain in _ATS_EMAIL_DOMAINS)


def _is_verification_subject(subject: str) -> bool:
    return any(pattern.search(subject) for pattern in _VERIFICATION_SUBJECT_PATTERNS)


def sweep_verification_emails(destination: str = "Archive", dry_run: bool = False) -> list[dict]:
    """Archive stray OTP/verification emails left sitting in INBOX.

    Matches on sender domain (known ATS platforms) AND subject content
    (verification/OTP/security-code language) together -- never body text,
    never either signal alone -- so ordinary personal mail is never touched
    even if it happens to say "confirm" or "verify" for unrelated reasons.

    Deliberately does not touch post-application confirmation emails
    ("Thank you for applying") -- those aren't disposable, they're the
    applied-to record.

    Returns the list of matched (and, unless dry_run, archived) messages.
    """
    conn = _connect()
    matched: list[dict] = []
    try:
        _typ, data = conn.uid("search", None, "ALL")
        ids = data[0].split() if data and data[0] else []
        for msg_uid in ids:
            typ, msg_data = conn.uid("fetch", msg_uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            sender = str(make_header(decode_header(msg.get("From", ""))))
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            if not (_is_ats_sender(sender) and _is_verification_subject(subject)):
                continue
            matched.append({"uid": msg_uid.decode(), "from": sender, "subject": subject})
            if not dry_run:
                _archive(conn, msg_uid, destination)
    finally:
        conn.logout()
    return matched
