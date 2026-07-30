# Security Policy

## Supported versions

Only the latest released version (see [Releases](https://github.com/ciguarin/applypilot/releases)) receives security fixes. There is no long-term support branch.

## Reporting a vulnerability

ApplyPilot handles real credentials on your behalf — LLM API keys, CapSolver API keys, email IMAP/SMTP passwords, and ATS account passwords it creates for you. If you find a security issue (credential leakage, injection via scraped job content, an MCP server receiving more access than it needs, etc.), please **do not open a public issue**.

Instead, report it privately via [GitHub Security Advisories](https://github.com/ciguarin/applypilot/security/advisories/new) for this repo, or open an issue asking for a private contact channel if you'd rather not use that flow.

Include:
- What component is affected (`discover`, `enrich`, `score`, `tailor`, `cover`, `pdf`, `apply`, or the CLI/config layer)
- Steps to reproduce, or the code path that concerns you
- What you'd expect to happen instead

## Scope notes

- `~/.applypilot/.env`, `profile.json`, `resume.txt/pdf`, and `*.db` are user data and are gitignored by design — they should never end up in a commit or a public gist. If you find one that has, that's a valid report.
- The `apply` stage runs Claude Code with real browser access (via `@playwright/mcp`) and a real email inbox (via a third-party MCP server) using credentials from your `.env`. Reports about the blast radius of that access (e.g. tool allowlisting gaps) are in scope.
