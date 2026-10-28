# Security Policy

## Reporting a vulnerability

Please **don't open a public issue**. Instead, use GitHub's
[private vulnerability reporting](../../security/advisories/new)
(**Security → Report a vulnerability**). I'll acknowledge within a week.

## Supported versions

Only the latest `main` is supported.

## Hardening checklist for your deployment

- Keep `HOST=127.0.0.1`. Expose the app only through Cloudflare Tunnel + Access.
- Set `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `OWNER_EMAILS` and `OWNER_SERVICE_TOKENS`, so the app verifies Access JWTs itself.
- Treat your `ICS_URL` like a password. If it leaks, reset it in Outlook (unpublish and republish the calendar).
- Rotate the iOS service token if a device is lost.
- Never commit `.env`, `static/custom/` or `static/easter-eggs/` (already gitignored).
