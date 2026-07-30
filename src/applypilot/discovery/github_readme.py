"""GitHub README-based Canadian internship discovery.

Polls curated GitHub repos that maintain markdown tables of Canadian tech
internship postings. Uses SHA-based change detection, only parses when a
README actually changes.

Replaces the n8n GitHub ingestion workflow with a pure-stdlib equivalent.
No extra dependencies beyond what applypilot already requires.

Default sources (extend via searches.yaml `github_readme_sources`):
  - negarprh/Canadian-Tech-Internships-2026
  - hanzili/canada_sde_intern_position
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

from applypilot.config import APP_DIR, PROFILE_PATH
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

SHA_CACHE_PATH = APP_DIR / "github_sha_cache.json"
GITHUB_UA = "applypilot-discovery/1.0"

DEFAULT_SOURCES = [
    {
        "key": "canadian_tech",
        "name": "Canadian Tech Internships 2026",
        "readme_url": "https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2026/refs/heads/main/README.md",
        "sha_url": "https://api.github.com/repos/negarprh/Canadian-Tech-Internships-2026/commits?path=README.md&per_page=1",
        "parser": "negarprh",
    },
    {
        "key": "hanzili",
        "name": "Canada SDE Intern Positions",
        "readme_url": "https://raw.githubusercontent.com/hanzili/canada_sde_intern_position/main/README.md",
        "sha_url": "https://api.github.com/repos/hanzili/canada_sde_intern_position/commits?path=README.md&per_page=1",
        "parser": "hanzili",
    },
]

_EXCLUDED_TITLES = {
    "senior", "staff", "lead", "principal", "director",
    "phd", "ph.d", "mba", "manager", "head of",
}
_EXCLUDED_HARDWARE = {
    "hardware", "firmware", "embedded", "fpga", "asic",
    "pcb", "mechanical", "electrical", "vlsi", "vhdl", "verilog",
    "design methodology", "design verification", "design emulation", "design validation",
}
_SKIP_CATEGORIES = {"hardware / firmware", "hardware/firmware"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(url: str, accept: str = "text/plain") -> str | None:
    req = Request(url, headers={"User-Agent": GITHUB_UA, "Accept": accept})
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except URLError as e:
        log.warning("Fetch failed %s: %s", url, e)
        return None


def _get_sha(sha_url: str) -> str | None:
    data = _fetch(sha_url, accept="application/vnd.github.v3+json")
    if not data:
        return None
    try:
        parsed = json.loads(data)
        return parsed[0]["sha"] if parsed else None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None


def _load_sha_cache() -> dict:
    try:
        return json.loads(SHA_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sha_cache(cache: dict) -> None:
    SHA_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _load_preferred_cities() -> list[str]:
    try:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        cities = profile.get("preferred_cities", [])
        if cities:
            return [c.lower() for c in cities]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ["toronto", "remote"]


def _location_ok(loc: str, preferred_cities: list[str] | None = None) -> bool:
    l = loc.lower()
    cities = preferred_cities if preferred_cities is not None else _load_preferred_cities()
    if "remote" in cities and any(kw in l for kw in ("remote", "anywhere", "canada")):
        return True
    return any(city in l for city in cities if city != "remote")


def _title_ok(title: str, check_hardware: bool = True) -> bool:
    t = title.lower()
    if any(kw in t for kw in _EXCLUDED_TITLES):
        return False
    if check_hardware and any(kw in t for kw in _EXCLUDED_HARDWARE):
        return False
    return True


def _extract_last_url(cell: str) -> str | None:
    matches = re.findall(r'\(https?://[^)]+\)', cell)
    return matches[-1][1:-1] if matches else None


# ---------------------------------------------------------------------------
# Parsers (one per README schema)
# ---------------------------------------------------------------------------

def _parse_negarprh(raw: str, preferred_cities: list[str] | None = None) -> list[dict]:
    """Parse negarprh/Canadian-Tech-Internships table format.

    Columns: Company | Role | Location | Apply
    Company cell may contain ↳ meaning "same as previous row".
    """
    jobs: list[dict] = []
    headers: list[str] = []
    in_table = False
    last_company = ""

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if not cells:
            continue
        if "company" in cells[0].lower():
            headers = [re.sub(r"[^a-z]", "", c.lower()) for c in cells]
            in_table = True
            continue
        if all(re.match(r"^[-: ]+$", c) for c in cells):
            continue
        if not in_table or len(cells) < 4:
            continue

        row = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}
        company = row.get("company", "")
        if company in ("↳", ""):
            company = last_company
        else:
            last_company = company

        role = row.get("role", "")
        location = row.get("location", "")
        apply_cell = row.get("apply", "")

        if "🔒" in apply_cell or "closed" in apply_cell.lower():
            continue
        url = _extract_last_url(apply_cell)
        if not url or "linkedin.com" in url:
            continue
        if not _location_ok(location, preferred_cities):
            continue
        if not _title_ok(role, check_hardware=True):
            continue

        jobs.append({"url": url, "title": f"{role} at {company}", "location": location})

    return jobs


def _parse_hanzili(raw: str, preferred_cities: list[str] | None = None) -> list[dict]:
    """Parse hanzili/canada_sde_intern_position table format.

    Columns: Title | Company | Location | Apply
    Section headers (##) indicate category. Skip hardware/firmware sections.
    """
    jobs: list[dict] = []
    headers: list[str] = []
    in_table = False
    skip_category = False

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            cat = line[3:].lower()
            skip_category = any(sc in cat for sc in _SKIP_CATEGORIES)
            in_table = False
            continue
        if skip_category:
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if not cells:
            continue
        if "title" in cells[0].lower():
            headers = [re.sub(r"[^a-z]", "", c.lower()) for c in cells]
            in_table = True
            continue
        if all(re.match(r"^[-:|\\s]+$", c) for c in cells):
            continue
        if not in_table or len(cells) < 4:
            continue

        row = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}
        raw_title = row.get("title", "")
        title = re.sub(r"<!--[^>]+-->", "", raw_title)
        title = re.sub(r"[🔥💤🆕]", "", title, flags=re.UNICODE)
        title = re.sub(r"^[\s\W]+", "", title).strip()

        company = row.get("company", "").strip()
        raw_location = row.get("location", "")
        location = re.sub(r"\s*\([^)]+\)\s*$", "", raw_location).strip()

        apply_cell = row.get("apply", "")
        m = re.search(r"\(<?(https?://[^>)]+)>?\)", apply_cell)
        if not m:
            continue
        url = m.group(1)
        if "linkedin.com" in url:
            continue
        if not _location_ok(raw_location, preferred_cities):
            continue
        if not _title_ok(title, check_hardware=False):
            continue
        if not title or not company:
            continue

        jobs.append({"url": url, "title": f"{title} at {company}", "location": location})

    return jobs


_PARSERS = {
    "negarprh": _parse_negarprh,
    "hanzili": _parse_hanzili,
}


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def _prune_removed(conn: sqlite3.Connection, current_urls: set[str]) -> int:
    rows = conn.execute(
        "SELECT url FROM jobs WHERE strategy='github_readme' "
        "AND (apply_status IS NULL OR apply_status='failed')"
    ).fetchall()
    stale = [r[0] for r in rows if r[0] not in current_urls]
    if not stale:
        return 0
    ph = ",".join("?" * len(stale))
    deleted = conn.execute(f"DELETE FROM jobs WHERE url IN ({ph})", stale).rowcount
    conn.commit()
    return deleted


def _insert_jobs(conn: sqlite3.Connection, jobs: list[dict], source_name: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for j in jobs:
        cur = conn.execute(
            "INSERT OR IGNORE INTO jobs (url, title, location, site, strategy, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (j["url"], j["title"], j.get("location"), source_name, "github_readme", now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_github_readme_discovery(sources: list[dict] | None = None) -> dict:
    """Discover Canadian internships from curated GitHub README tables.

    Uses SHA-based change detection: fetches all sources only when at least
    one README has changed since the last run. Prunes listings that have been
    removed from the source tables.

    Args:
        sources: Override the default source list. Each entry needs:
                 key, name, readme_url, sha_url, parser (negarprh|hanzili).

    Returns:
        Dict with inserted, pruned, sources_checked counts.
    """
    init_db()
    conn = get_connection()
    active_sources = sources or DEFAULT_SOURCES
    sha_cache = _load_sha_cache()

    # Check all SHAs first, skip entirely if nothing changed
    new_shas: dict[str, str | None] = {}
    any_changed = False
    for source in active_sources:
        sha = _get_sha(source["sha_url"])
        new_shas[source["key"]] = sha
        if sha and sha != sha_cache.get(source["key"]):
            any_changed = True

    if not any_changed:
        log.info("[github_readme] No README changes since last run, skipping")
        return {"inserted": 0, "pruned": 0, "sources_checked": len(active_sources)}

    # At least one source changed, fetch and parse all
    preferred_cities = _load_preferred_cities()
    log.info("[github_readme] Filtering for cities: %s", preferred_cities)
    all_jobs: list[dict] = []
    for source in active_sources:
        readme = _fetch(source["readme_url"])
        if not readme:
            log.error("[%s] Failed to fetch README", source["name"])
            continue
        parser_fn = _PARSERS.get(source["parser"])
        if not parser_fn:
            log.error("[%s] Unknown parser: %s", source["name"], source["parser"])
            continue
        jobs = parser_fn(readme, preferred_cities)
        log.info("[%s] Parsed %d qualifying listings", source["name"], len(jobs))
        for j in jobs:
            j["_source_name"] = source["name"]
        all_jobs.extend(jobs)

    all_urls = {j["url"] for j in all_jobs}

    # Prune listings removed from source tables
    pruned = _prune_removed(conn, all_urls)
    if pruned:
        log.info("[github_readme] Pruned %d removed listings", pruned)

    # Insert new listings grouped by source
    inserted = 0
    for source in active_sources:
        source_jobs = [j for j in all_jobs if j.get("_source_name") == source["name"]]
        if source_jobs:
            inserted += _insert_jobs(conn, source_jobs, source["name"])

    # Persist updated SHAs
    for key, sha in new_shas.items():
        if sha:
            sha_cache[key] = sha
    _save_sha_cache(sha_cache)

    log.info("[github_readme] Done: %d new, %d pruned", inserted, pruned)
    return {"inserted": inserted, "pruned": pruned, "sources_checked": len(active_sources)}
