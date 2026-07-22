"""ApplyPilot — AI-powered end-to-end job application pipeline."""

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("applypilot")
except Exception:
    # Not installed as a package (e.g. running straight from a source
    # checkout with no editable install) -- pyproject.toml is the real
    # source of truth, this is just a fallback so imports don't crash.
    __version__ = "0.0.0-dev"
