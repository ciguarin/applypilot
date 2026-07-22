"""ATS platform detection, shared by diagnostics and per-platform apply fillers.

Only Workday has a deterministic filler (workday.py) today. The others are
classified here purely for visibility -- `applypilot status --by-platform`
uses this to show what's actually in the queue, since URL substrings are a
much cheaper and more honest way to answer "how many of these are Workday"
than repeatedly hand-writing the same classification as a one-off script.
"""

from __future__ import annotations

_PATTERNS: list[tuple[str, str]] = [
    ("myworkdayjobs.com", "Workday"),
    ("greenhouse.io", "Greenhouse"),
    ("lever.co", "Lever"),
    ("icims.com", "iCIMS"),
    ("taleo.net", "Taleo"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("bamboohr.com", "BambooHR"),
    ("ashbyhq.com", "Ashby"),
    ("linkedin.com", "LinkedIn"),
    ("indeed.com", "Indeed"),
]


def classify_platform(url: str | None) -> str:
    """Return a short platform label for a job's apply URL, or 'Other'."""
    if not url:
        return "Unknown"
    url_lower = url.lower()
    for substring, label in _PATTERNS:
        if substring in url_lower:
            return label
    return "Other"
