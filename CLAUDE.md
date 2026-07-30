# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Style rule: no em dashes

Never use em dashes (—) anywhere in this repo: source code, comments, docstrings, README/CHANGELOG/CONTRIBUTING/SECURITY, commit messages, or console output strings. Write complete sentences instead. Use periods, commas, or parentheses for a break in a sentence.

This applies to LLM-generated output too. `scoring/validator.py`'s `sanitize_text()` already strips em dashes from LLM-generated resume/cover-letter fields (converts to a comma). If you add a new field or a new place that assembles resume/cover-letter text, route it through `sanitize_text()` or an equivalent, and check for any place that splices a literal `" — "` into an f-string directly (that bypasses `sanitize_text()` entirely, since it's not part of either input string being sanitized). This exact bug existed in `scoring/tailor.py`'s project-header line and was fixed by switching the separator to `": "`.

Do not retroactively edit already-generated resumes or cover letters sitting in `~/.applypilot/tailored_resumes/` or `~/.applypilot/cover_letters/` to fix this. Those are past output tied to real job applications; leave them as-is. This rule is about preventing new occurrences, not scrubbing history.
