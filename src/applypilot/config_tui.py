"""Interactive settings TUI — launched by `applypilot config` with no subcommand."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from applypilot.config import make_console

_con = make_console()


# ── Key reading ───────────────────────────────────────────────────────────────

def _read_key() -> str:
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return "special:" + msvcrt.getwch()
        return ch
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch += sys.stdin.read(2)
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _is_up(k: str) -> bool:
    return k in ("\x1b[A", "special:\x48")


def _is_down(k: str) -> bool:
    return k in ("\x1b[B", "special:\x50")


def _is_enter(k: str) -> bool:
    return k in ("\r", "\n")


def _is_quit(k: str) -> bool:
    return k in ("q", "Q", "\x1b", "\x03")


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Setting:
    label: str
    get_val: Callable[[], str]
    edit: Callable[[], None]
    is_readonly: bool = False


@dataclass
class Group:
    name: str
    items: list[Setting] = field(default_factory=list)


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render(groups: list[Group], cursor: int) -> None:
    _con.clear()
    _con.print()
    _con.print(
        "  [bold]ApplyPilot Settings[/bold]  "
        "[dim]↑↓ navigate · Enter edit · q quit[/dim]"
    )

    idx = 0
    for group in groups:
        _con.print()
        _con.print(f"  [bold dim]{group.name.upper()}[/bold dim]")
        for s in group.items:
            selected = idx == cursor
            arrow = "[bold cyan]>[/bold cyan]" if selected else " "
            label = f"[bold]{s.label}[/bold]" if selected else s.label
            val = s.get_val()
            _con.print(f"   {arrow} {label:<24}  {val}")
            idx += 1

    _con.print()
    _con.print("  [dim]Press Enter to edit · q to quit[/dim]")


# ── Settings builders ─────────────────────────────────────────────────────────

def _build_groups() -> list[Group]:
    from applypilot.config import PROFILE_PATH, SEARCH_CONFIG_PATH, ENV_PATH, load_env

    load_env()

    # ── Loaders/savers ───────────────────────────────────────────────────────

    def _load_profile() -> dict:
        try:
            return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_profile(profile: dict) -> None:
        PROFILE_PATH.write_text(
            json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _load_searches() -> dict:
        import yaml
        try:
            return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _save_searches(cfg: dict) -> None:
        import yaml
        SEARCH_CONFIG_PATH.write_text(
            yaml.dump(cfg, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )

    def _load_env_text() -> str:
        return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""

    def _save_env(key: str, val: str) -> None:
        text = _load_env_text()
        if f"{key}=" in text:
            text = re.sub(rf"^{re.escape(key)}=.*", f"{key}={val}", text, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\n{key}={val}\n"
        ENV_PATH.write_text(text, encoding="utf-8")

    # ── Profile helpers ──────────────────────────────────────────────────────

    def _pget(key: str, section: str = "personal", mask: bool = False) -> Callable[[], str]:
        def fn() -> str:
            p = _load_profile()
            sec = p.get(section, {})
            v = sec.get(key, "")
            if not v:
                return "[dim]not set[/dim]"
            if mask:
                return "[cyan]****[/cyan]"
            return f"[cyan]{v}[/cyan]"
        return fn

    def _pedit(
        key: str,
        label: str,
        section: str = "personal",
        password: bool = False,
    ) -> Callable[[], None]:
        def fn() -> None:
            from rich.prompt import Prompt
            profile = _load_profile()
            sec = profile.setdefault(section, {})
            current = sec.get(key, "")
            val = Prompt.ask(f"  {label}", default=current, password=password)
            if val != current:
                sec[key] = val
                _save_profile(profile)
                _con.print("  [green]Saved.[/green]")
            else:
                _con.print("  [dim]No change.[/dim]")
        return fn

    # ── Env helpers ──────────────────────────────────────────────────────────

    def _eget(key: str, default: str = "", mask: bool = False) -> Callable[[], str]:
        def fn() -> str:
            v = os.environ.get(key, default)
            if not v:
                return "[dim]not set[/dim]"
            if mask:
                return f"[cyan]{v[:6]}…[/cyan]" if len(v) > 8 else f"[cyan]{v}[/cyan]"
            return f"[cyan]{v}[/cyan]"
        return fn

    def _eedit(key: str, label: str, password: bool = False) -> Callable[[], None]:
        def fn() -> None:
            from rich.prompt import Prompt
            current = os.environ.get(key, "")
            val = Prompt.ask(f"  {label}", default=current, password=password)
            if val != current:
                _save_env(key, val)
                os.environ[key] = val
                _con.print("  [green]Saved.[/green]")
            else:
                _con.print("  [dim]No change.[/dim]")
        return fn

    # ── Search helpers ───────────────────────────────────────────────────────

    def _int_setting(
        keys: list[str], label: str, default: int
    ) -> tuple[Callable[[], str], Callable[[], None]]:
        def get() -> str:
            cfg = _load_searches()
            v: object = cfg
            for k in keys:
                v = v.get(k, default) if isinstance(v, dict) else default
            return f"[cyan]{v}[/cyan]"

        def edit() -> None:
            from rich.prompt import Prompt
            cfg = _load_searches()
            node: dict = cfg
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            current = str(node.get(keys[-1], default))
            raw = Prompt.ask(f"  {label}", default=current)
            if raw.strip() != current:
                try:
                    node[keys[-1]] = int(raw.strip())
                    _save_searches(cfg)
                    _con.print("  [green]Saved.[/green]")
                except ValueError:
                    _con.print("  [red]Invalid number.[/red]")
            else:
                _con.print("  [dim]No change.[/dim]")

        return get, edit

    def _queries_get() -> str:
        cfg = _load_searches()
        qs = [q["query"] for q in cfg.get("queries", [])]
        if not qs:
            return "[dim]not set[/dim]"
        preview = ", ".join(qs[:3])
        return f"[cyan]{preview}{'…' if len(qs) > 3 else ''}[/cyan]"

    def _queries_edit() -> None:
        from rich.prompt import Prompt
        cfg = _load_searches()
        current = [q["query"] for q in cfg.get("queries", [])]
        _con.print(f"  Current: [cyan]{', '.join(current)}[/cyan]")
        raw = Prompt.ask("  New queries (comma-separated, Enter to keep)", default="")
        if raw.strip():
            roles = [r.strip() for r in raw.split(",") if r.strip()]
            cfg["queries"] = [{"query": r, "tier": min(i + 1, 3)} for i, r in enumerate(roles)]
            _save_searches(cfg)
            _con.print("  [green]Saved.[/green]")
        else:
            _con.print("  [dim]No change.[/dim]")

    def _sites_get() -> str:
        cfg = _load_searches()
        return f"[cyan]{', '.join(cfg.get('sites', ['indeed', 'linkedin']))}[/cyan]"

    def _sites_edit() -> None:
        from rich.prompt import Prompt
        cfg = _load_searches()
        current = ", ".join(cfg.get("sites", ["indeed", "linkedin"]))
        raw = Prompt.ask("  Job boards (comma-separated)", default=current)
        if raw.strip() != current:
            cfg["sites"] = [s.strip() for s in raw.split(",") if s.strip()]
            _save_searches(cfg)
            _con.print("  [green]Saved.[/green]")
        else:
            _con.print("  [dim]No change.[/dim]")

    def _score_get() -> str:
        return f"[cyan]{os.environ.get('APPLYPILOT_MIN_SCORE', '7')}[/cyan]"

    def _score_edit() -> None:
        from rich.prompt import Prompt
        current = os.environ.get("APPLYPILOT_MIN_SCORE", "7")
        raw = Prompt.ask("  Min fit score (1–10)", default=current)
        if raw.strip() != current:
            try:
                v = int(raw.strip())
                if not 1 <= v <= 10:
                    raise ValueError
                _save_env("APPLYPILOT_MIN_SCORE", str(v))
                os.environ["APPLYPILOT_MIN_SCORE"] = str(v)
                _con.print("  [green]Saved.[/green]")
            except ValueError:
                _con.print("  [red]Must be an integer 1–10.[/red]")
        else:
            _con.print("  [dim]No change.[/dim]")

    hours_get, hours_edit = _int_setting(["defaults", "hours_old"], "Hours lookback", 168)
    rps_get, rps_edit = _int_setting(["defaults", "results_per_site"], "Results per site", 30)

    # ── Group definitions ────────────────────────────────────────────────────

    profile = Group("Profile", [
        Setting("Name",              _pget("preferred_name"),               _pedit("preferred_name", "Name")),
        Setting("Email",             _pget("email"),                        _pedit("email", "Email")),
        Setting("Phone",             _pget("phone"),                        _pedit("phone", "Phone")),
        Setting("City",              _pget("city"),                         _pedit("city", "City")),
        Setting("LinkedIn URL",      _pget("linkedin_url"),                 _pedit("linkedin_url", "LinkedIn URL")),
        Setting("GitHub URL",        _pget("github_url"),                   _pedit("github_url", "GitHub URL")),
        Setting("Portfolio URL",     _pget("portfolio_url"),                _pedit("portfolio_url", "Portfolio URL")),
        Setting("Job site password", _pget("password", mask=True),         _pedit("password", "Job site password", password=True)),
        Setting("Google email",      _pget("email", section="google_account"), _pedit("email", "Google email", section="google_account")),
        Setting("Google password",   _pget("password", section="google_account", mask=True), _pedit("password", "Google password", section="google_account", password=True)),
    ])

    search = Group("Search", [
        Setting("Queries",           _queries_get, _queries_edit),
        Setting("Job boards",        _sites_get,   _sites_edit),
        Setting("Hours lookback",    hours_get,    hours_edit),
        Setting("Results per site",  rps_get,      rps_edit),
    ])

    llm = Group("LLM / API", [
        Setting("Model",         _eget("LLM_MODEL", "gemini-2.0-flash"),      _eedit("LLM_MODEL", "Model")),
        Setting("Min score",     _score_get,                                   _score_edit),
        Setting("Gemini key",    _eget("GEMINI_API_KEY", mask=True),           _eedit("GEMINI_API_KEY", "Gemini API key", password=True)),
        Setting("OpenAI key",    _eget("OPENAI_API_KEY", mask=True),           _eedit("OPENAI_API_KEY", "OpenAI API key", password=True)),
        Setting("OpenRouter URL", _eget("LLM_URL"),                            _eedit("LLM_URL", "OpenRouter/custom base URL")),
        Setting("OpenRouter key", _eget("LLM_API_KEY", mask=True),             _eedit("LLM_API_KEY", "API key for OpenRouter/custom URL", password=True)),
        Setting("CapSolver key", _eget("CAPSOLVER_API_KEY", mask=True),        _eedit("CAPSOLVER_API_KEY", "CapSolver API key", password=True)),
    ])

    return [profile, search, llm]


# ── Main entry point ──────────────────────────────────────────────────────────

def run_settings_tui() -> None:
    groups = _build_groups()
    flat: list[Setting] = [s for g in groups for s in g.items]
    cursor = 0

    while True:
        _render(groups, cursor)

        try:
            key = _read_key()
        except (KeyboardInterrupt, EOFError):
            break

        if _is_quit(key):
            break
        elif _is_up(key):
            cursor = (cursor - 1) % len(flat)
        elif _is_down(key):
            cursor = (cursor + 1) % len(flat)
        elif _is_enter(key):
            s = flat[cursor]
            if s.is_readonly:
                continue
            _con.clear()
            _con.print(f"\n  [bold]Editing:[/bold] {s.label}\n")
            try:
                s.edit()
            except (KeyboardInterrupt, EOFError):
                _con.print("\n  [dim]Cancelled.[/dim]")

            import time
            time.sleep(0.6)

            # Rebuild to pick up saved changes
            groups = _build_groups()
            flat = [s for g in groups for s in g.items]

    _con.clear()
