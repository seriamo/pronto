# Pronto

## Overview

A Flask web app that displays a user's availability status on a desk-mounted display (Raspberry Pi kiosk). It reads Outlook/Exchange calendars via ICS feeds (multi-calendar), shows busy/free/away/OOO state with seasonal backgrounds, and supports coworker messaging.

## Architecture

Single-file Flask app (`app.py`) with:
- **ICS calendar parsing** — custom regex-based parser for recurring events (RRULE, EXDATE, RECURRENCE-ID) because the `ics` library doesn't handle Outlook RRULEs
- **Multi-calendar support** — primary ICS URL + additional calendars via `ICS_URLS_EXTRA`; each cached individually in `calendar_caches/`
- **Two-tier caching** — raw ICS file cache + parsed events cache. Time-aware: 30 min during business hours, 4 hrs off-hours
- **Single-pass ICS parser** (`parse_ics_single_pass_optimized`) — extracts tz_cache, recurrence_map, busystatus_map, recurring_uids in one scan
- **Location system** — schedule-based defaults with per-day API overrides; persisted to `location_overrides.json`
- **OOO detection** — detects `X-MICROSOFT-CDO-BUSYSTATUS:OOF` and OOO-named events; all-day = purple "OOO", timed = yellow "Away"; chains consecutive OOO blocks
- **OOO override** — `/api/ooo/set` for manual OOO with return date; persisted to `ooo_override.json`
- **Coworker messaging** — `/msg` web UI (Cloudflare Access gated), 160 char limit, displays on Pi for 3 min with easter-egg backgrounds; display only gets `sender_name` (never the email)
- **Privacy-aware** — busy status shows "Next free at X:XX PM" instead of meeting titles
- **Admin endpoints** (`/admin/*`, `/api/test/*`) — mock statuses (busy, free, ooo, away), debug dashboard, message history, background gallery, OOO controls
- **Auth & hardening** — see Security Model below

## Key Files

| File | Purpose |
|---|---|
| `app.py` | Main Flask app — all backend logic (~2,000 lines) |
| `templates/display.html` | Frontend display (polls `/api/status`, `/api/version`, `/api/message`) |
| `templates/message.html` | Coworker message sending UI at `/msg` |
| `templates/test_menu.html` | Admin hub at `/admin` — all debug tools must be linked here |
| `static/` | Seasonal backgrounds, generic `logo.svg`; `custom/` and `easter-eggs/` hold personal files (gitignored) |
| `.env` | All config — see `.env.example` (calendar, personalization, schedule, server, Cloudflare Access) |
| `docs/deployment.md` | Pi setup, Cloudflare Tunnel/Access, troubleshooting |
| `docs/ios-shortcuts.md` | iOS Shortcut setup: location + OOO automation |

## Status Logic

1. Check OOO override (manual via `/api/ooo/set`)
2. Check calendar for OOF busystatus or OOO-named events
3. All-day OOO → purple "Out of office" with return date
4. Timed OOO → yellow "Away" with smart return time (chains consecutive blocks)
5. Current meeting → red "Busy" with next free time
6. No meetings → green "Available" with next meeting time
7. Outside business hours: sleep UNLESS OOO/away is active
8. Privacy: busy never exposes meeting titles

## Security Model

- Binds to `HOST` (default `127.0.0.1`); served by waitress (`FLASK_ENV=development` → Flask debug server)
- `@before_request _enforce_access` assigns roles; **every endpoint is owner-only unless listed** in `VIEWER_ENDPOINTS` (display, status, version, message poll, easter eggs, qr, static) or `USER_ENDPOINTS` (`/msg`, message POST)
- **Owner**: local request (loopback or `TRUSTED_NETWORKS`, no proxy headers, IP/localhost/.local `Host`), or verified Access JWT whose email is in `OWNER_EMAILS` / service-token `common_name` in `OWNER_SERVICE_TOKENS`
- **User**: verified Access JWT with an email. **Viewer**: any verified JWT, or anyone if `PUBLIC_DISPLAY=true`
- Access JWTs (`Cf-Access-Jwt-Assertion` header or `CF_Authorization` cookie) verified with PyJWKClient: RS256, `CF_ACCESS_AUD`, issuer `https://CF_ACCESS_TEAM_DOMAIN`. Never trust `Cf-Access-Authenticated-User-Email`
- Cross-site non-GET requests rejected (`Sec-Fetch-Site` / `Origin`); JSON bodies via `get_json(silent=True)` only (no `force=True`)
- CSP + nosniff + `X-Frame-Options: DENY` + `no-referrer` + `no-store` on every response
- Never log or return ICS URLs or raw exception text; `/api/status` never includes event titles
- Templates: escape any calendar/user data before `innerHTML` (`escapeHtml`); pass server values to JS via `|tojson`

## Key Conventions

- All admin/debug endpoints MUST be added to the `/admin` menu (`test_menu.html`)
- New endpoints default to owner-only; add to `VIEWER_ENDPOINTS`/`USER_ENDPOINTS` only if intended
- No personal data in the repo: names, rooms, hostnames, logos, photos come from `.env` or gitignored `static/custom/`, `static/easter-eggs/`
- Never set hop-by-hop headers (e.g. `Connection`) in responses — waitress rejects them with a 500
- CF Access authenticates by path — browser JS POST must go to same URL as page (e.g., `/msg` accepts GET+POST)
- Message sanitization: denylist regex (blocks control chars, HTML tags); allows all Unicode
- Business hours: 8am-6pm Mon-Fri; configurable via `BUSINESS_START_HOUR`/`BUSINESS_END_HOUR`
- Location schedule: `OFFICE_DAYS`/`OFFICE_HOURS` (default Tue-Thu 10-15); OOO title prefix via `OOO_KEYWORD`
- Meeting time normalization: rounds :05→:00 and :35→:30 (Microsoft 5-min gaps)
- Print-based logging with emoji prefixes
- Cache/state files are gitignored
- Flask runs on port 5001 on the Pi

## Deployment

- **Service**: systemd `desk-display.service`
- **Tunnel**: cloudflared → `http://localhost:5001`
- **Auth**: Cloudflare Access (Zero Trust) — email auth for browser, Service Token for iOS
- **Display**: Chromium kiosk 800×480, must use `--ozone-platform=wayland`
- **Deploy**: `./deploy_to_pi.sh user@host` (rsync + pip install + restart)
