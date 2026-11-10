# RSSPhaser Improvement Plan

Date: 2026-11-10. Status: proposal, nothing in this document is implemented yet.

## Context

- RSSPhaser polls security news feeds, keeps entries that match keywords, and re-serves them as one feed (`/feed.xml`).
- The owner's current workflow: Slack's RSS app reads `/feed.xml` → a person spots important items → ticket in Redmine → work → clients informed. Every step after Slack is manual.
- That workflow is one example. Other users of RSSPhaser will use other tools, so the core must stay tool-neutral and every integration must be optional.
- Deployment facts confirmed by the owner: internet-facing behind a reverse proxy with Transport Layer Security (TLS); single administrator.
- Basis of this plan: a full read of all 2,847 lines in the repository. No production data, logs or metrics were available, so anything about production is marked "to verify".
- Effort tags: **S** = small (hours), **M** = medium (days), **L** = large (a week or more). These are rough.

## Design principle: lean core, optional plugins

- **Core** = fetch, match, store, score, serve feeds, triage inbox, events, generic outgoing webhook.
- **Plugin** = anything tied to a specific external product or a specific team's process. Disabled by default. The core never imports a plugin.
- **Contract between them:**
  - The core emits events: `match.created`, `match.updated`, `source.failed`, `cycle.completed`.
  - A plugin subscribes to events and may register **actions** (something a person or a rule can trigger on a match).
  - Plugins live in `plugins/<name>/` and are switched on in configuration.
- **Configurable links instead of hard-wired behaviour:** every link shown on a match (in Slack, in the feed, in the inbox) comes from a list of link templates that the administrator defines. See F1.

## Headline findings

1. **The recurring poll is switched off at startup (confirmed by test).** `app/scheduler.py:46` passes `next_run_time=None`, which APScheduler 3.10.4 treats as "add this job paused".
   - Effect: polling runs once when the scheduler container starts, then never again, until someone changes the poll interval in the interface (that reschedules and un-pauses the job). After the next restart it is paused again.
   - Evidence: in-memory test with the same arguments: 0 runs in 4.5 seconds against 4 runs for a control job; runs started only after `reschedule_job`.
   - Not verified: whether production is currently in the paused state. See Phase 0.
2. **Nothing alerts when the pipeline goes quiet.** "No news" and "broken" look identical in Slack. Finding 1 is exactly this kind of silent failure.
3. **The login is internet-facing with no brute-force limit, no second factor, and a 31-day session.**
4. **The interface has no view of matches at all.** The only way to see what matched is the feed. Which keyword matched is not stored, so there is no triage, routing or reporting to build on.
5. **No tests, no continuous integration, no schema migration tool, no backup routine in the repository.** These are prerequisites for changing a production system safely.

## Phase 0 — Confirm and stabilise (do first)

| # | Action | Type | Effort |
|---|--------|------|--------|
| 0.1 | Check production: dashboard "Last poll" column and `docker compose logs scheduler` for the spacing of "Poll cycle starting." lines. | Read-only | S |
| 0.2 | Fix the paused job: remove `next_run_time=None` in `app/scheduler.py:46`. Keep the immediate first poll. | Code, 1 line | S |
| 0.3 | Check production `.env`: `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `DB_PASSWORD`, `DB_ROOT_PASSWORD` must not be the values from `.env.example` or the fallbacks in `docker-compose.yml:19-22`. A known `SECRET_KEY` lets anyone forge an admin session without the password. | Read-only | S |
| 0.4 | Check whether port 8000 is reachable from the internet directly, bypassing the proxy. `docker-compose.yml:48` publishes it on all interfaces. If the proxy is on the same host, change to `127.0.0.1:8000:8000`. | Verify, then config | S |
| 0.5 | Take a database backup and add a scheduled dump of the `db_data` volume before any further change. | Operations | S |
| 0.6 | Interim workaround if 0.1 shows polling is paused and 0.2 cannot ship immediately: change the poll interval in the interface and change it back. | Operations | S |

## Foundations (prerequisites for everything after Phase 0)

- **Tests (M):** pytest suite for `app/matching.py`, `app/poller.py` (with recorded feed fixtures), `app/auth.py`, and route tests through Flask's test client. Include a regression test that the poll job has a next run time.
- **Continuous integration (S):** GitHub Actions workflow running tests, a linter (ruff), and `pip-audit` on every push. None exists today.
- **Schema migrations (M):** adopt Alembic. `Base.metadata.create_all` (`app/db.py:86`) only creates missing tables; it cannot add columns, so every feature below is blocked without this. Rule: additive changes only (new tables, nullable columns), backup first.
- **Staging (S):** a second Compose stack with its own database, used for every change before production.
- **Dependency hygiene (S):** lock transitive dependencies with hashes (pip-tools); add a Dependabot configuration file. Current pins are clean per `pip-audit` as of 2026-11-10.

## Workstream 1 — Security

Ranked for an internet-facing instance behind a TLS proxy with one administrator.

| # | Finding | Evidence | Severity | Fix | Effort |
|---|---------|----------|----------|-----|--------|
| S1 | No login rate limit or lockout. Each attempt also costs a bcrypt check on one of only 2 workers, so it doubles as a denial-of-service lever. | `app/web.py:73-91` | High | Rate limit at the proxy and in the application (Flask-Limiter); progressive delay; alert on repeated failures. | S |
| S2 | No second factor. | `app/auth.py` | High | Time-based One-Time Password (TOTP) as a second factor; alternatively put the admin paths behind the proxy's single sign-on or an IP allow-list and leave only `/feed.xml` public. | M |
| S3 | Application does not refuse to start with example secrets. | `app/config.py:148-159` | High if defaults are live | Startup check rejecting known example values and short keys. | S |
| S4 | Session cookie lacks the `Secure` flag; application is unaware it sits behind a proxy, so generated links and the feed's self-link use the wrong scheme/host. | `app/web.py:53-56`, `app/web.py:353` | Medium | `SESSION_COOKIE_SECURE=True`, Werkzeug `ProxyFix`, and a configured public base address instead of `request.url_root`. | S |
| S5 | No Cross-Site Request Forgery (CSRF) protection on any form. `SameSite=Lax` reduces but does not remove the risk. | all POST routes in `app/web.py` | Medium | Flask-WTF `CSRFProtect`; token in forms and in the `fetch` header in `app/templates/base.html`. | M |
| S6 | Open redirect after login: `next=//attacker.example` passes the `startswith("/")` check. | `app/web.py:84-88` | Medium | Reject values starting with `//` or `/\`; parse and require an empty host. | S |
| S7 | Session lasts 31 days (Flask default for permanent sessions); logout is a GET link. | `app/web.py:82`, `app/web.py:93` | Medium | 8–12 hour lifetime with idle timeout; logout by POST. | S |
| S8 | Server-Side Request Forgery (SSRF): a source address can point at internal systems (`http://db:3306`, cloud metadata address); redirects are followed; the error text is shown on the dashboard. | `app/web.py:143`, `app/poller.py:89` | Medium (administrator only) | Resolve the host and block private, loopback and link-local ranges; re-check on each redirect; cap redirects. | M |
| S9 | Unbounded download: `requests.get` reads the whole response into memory; the read timeout is per chunk, not total. A hostile or broken source can exhaust memory. | `app/poller.py:89` | Medium | Stream with a size cap (for example 10 MB) and a total time budget. | S |
| S10 | `/feed.xml` is public, unauthenticated, uncached, and rebuilds up to 200 items with full bodies per request on 2 workers. Cheap to overload. | `app/web.py:351-359`, `app/feed.py` | Medium | Caching (see P2), proxy rate limit, optional secret token in the address for private feeds. | S |
| S11 | `/healthz` returns raw database error text to anonymous callers. | `app/web.py:370` | Low | Return a generic message; log the detail. | S |
| S12 | Script injection through a source name containing `'` inside the inline `confirm(...)` handler. Self-inflicted today, but real. | `app/templates/sources.html:47` | Low | Move to a `data-` attribute plus an event listener. | S |
| S13 | No security response headers (Content-Security-Policy, frame, content-type, referrer). Inline scripts and styles block a strict policy. | `app/templates/base.html` | Low | Move script and style to static files; add headers. | M |
| S14 | Article bodies from upstream feeds are stored and re-served as raw HTML. | `app/poller.py:182`, `app/feed.py:48` | Low | Sanitise with an allow-list (nh3) at ingest. | S |
| S15 | Containers: compiler toolchain left in the image; base image not pinned by digest; no read-only filesystem, capability drop, `no-new-privileges` or resource limits; the database root password is passed to the app and scheduler containers through `env_file`. | `Dockerfile:7-12`, `docker-compose.yml:36,59` | Low–Medium | Multi-stage build; pin digest; hardening options; pass only needed variables. | M |
| S16 | Database password with characters such as `@` or `/` breaks the connection string, which nudges towards weak passwords. | `app/config.py:72-77` | Low | Build with `sqlalchemy.engine.URL.create`. | S |
| S17 | No audit trail of configuration changes. | — | Low | `audit_log` table: who, what, when, from which address. | S |
| S18 | New with this plan: plugins and link templates add an outbound surface. | — | Design rule | Plugin secrets only in environment variables; link templates percent-encode every placeholder; outgoing webhooks signed and subject to the S8 address checks. | S |

## Workstream 2 — Speed and efficiency

Caveat: ranked from code reading, not from measurements. Step P0 comes first so the rest is driven by numbers.

| # | Item | Evidence | Fix | Effort |
|---|------|----------|-----|--------|
| P0 | No timing data exists. | — | Prometheus `/metrics` endpoint (cycle duration, per-source fetch time, feed build time, match counts) and a Grafana dashboard. | M |
| P1 | **End-to-end alert delay is the biggest speed issue.** Delay = poll interval (15 minutes) + Slack RSS app's own fetch schedule, which we do not control. | `config.yaml:12` | Push matches straight to the chat tool at the moment of matching (see F1). Removes the second wait entirely. | M |
| P2 | Feed is rebuilt for every request: query, XML generation, pretty-printing. No `ETag`, no `Cache-Control`, no compression. | `app/feed.py:17-60` | Render once per poll cycle, cache the bytes, serve with `ETag`/`Last-Modified` and 304 responses; drop `pretty=True`; compress at the proxy. | S |
| P3 | Matching lower-cases the full title and body once per keyword. With K keywords, a 100 KB body is copied K times per entry. | `app/matching.py:59-60`, `app/poller.py:169` | Lower-case each entry once; pre-lower-case terms once per cycle. For large keyword lists (expected once F15 is live), switch to a single-pass multi-pattern matcher (Aho–Corasick). | S |
| P4 | Matching runs on raw HTML, so keywords hit tag attributes and link addresses. Costs time and precision. | `app/poller.py:163` | Strip HTML to text before matching; keep sanitised HTML for display. | S |
| P5 | One fixed interval for all sources. | `app/scheduler.py:39-48` | Per-source interval; adaptive polling (faster for sources that publish often, slower for quiet ones); honour `Retry-After` and feed `ttl`. | M |
| P6 | Near-real-time is possible for sources that advertise a WebSub hub. | — | Subscribe to the hub and receive pushes instead of polling. Needs a public callback path. | L |
| P7 | 2 synchronous gunicorn workers; one slow request blocks half the capacity. | `Dockerfile:35-37` | `gthread` worker class with threads; confirm the proxy buffers requests. | S |
| P8 | Each source poll opens 3–4 database sessions and inserts matches row by row; the dashboard opens about 9 sessions per page view. | `app/poller.py:115,208,222,242`, `app/web.py:102-125` | One session per source; bulk insert; one session per dashboard request. | S |
| P9 | Feed ordering sorts on two columns with single-column indexes. Negligible at the 5,000-row cap; matters if retention grows. | `app/models.py:76-77`, `app/feed.py:33` | Composite index on (`published_at`, `fetched_at`). | S |
| P10 | Image carries `build-essential`; slower pulls and deploys. | `Dockerfile:7-12` | Multi-stage build (same change as S15). | S |
| P11 | Body column is `TEXT` (64 KB in MariaDB) but the application truncates at 102,400 characters; `INSERT IGNORE` likely truncates silently. To verify. | `app/models.py:87`, `config.yaml:16` | `MEDIUMTEXT`, or lower the application limit. | S |
| P12 | Plugins must never slow the poll cycle. | — | Events are queued in an `outbox` table and delivered by a separate worker with retries; a failing plugin cannot block or lose a match. | M |

## Workstream 3 — Features

**Direction:** turn RSSPhaser from a feed filter into a signal-to-action pipeline, with the action side left open for each user to wire to their own tools.

### 3A. Close the loop: from match to action

- **F1 Chat alerts with configurable links (M, core).**
  - Post each match by webhook with title, source, matched keywords and priority. Updates to the same story go into the same thread.
  - **Action links are a list the administrator defines**, each with a label and an address template. Placeholders: `{title}`, `{link}`, `{summary}`, `{source}`, `{keywords}`, `{cve}`, `{priority}`, `{match_id}`, `{base_url}`.
  - Shipped defaults: "Open article" → `{link}` and "Open in RSSPhaser" → `{base_url}/matches/{match_id}`. Anyone can replace or add links to make them do something else.
  - Example of flexibility without any plugin: a link template pointing at a ticket tool's "new issue" page with the subject and description pre-filled. Redmine, GitLab, GitHub and Jira accept pre-filled fields in the address; exact parameter names to verify per tool and version.
  - Slack is the first target; the message builder sits behind a small notifier interface so other chat tools reuse the same link templates.
  - `/feed.xml` keeps running in parallel until the push is trusted.
- **F2 Triage inbox (M, core).** A "Matches" page: search, filter, notes, and a state per item. Default states: new → triaged → actioned → dismissed. State names are configurable. Shows the same action links as F1.
- **F3 Plugin framework and generic outgoing webhook (M, core).**
  - Event bus, plugin loader, plugin settings page, delivery through the outbox (P12).
  - Generic outgoing webhook: sends the match as JSON to any address on chosen events. Covers automation platforms and in-house scripts with no plugin at all.
- **F4 Redmine plugin (M, plugin, off by default).**
  - Lives in `plugins/redmine/`. Not part of the core; removing the folder changes nothing else.
  - Registers the action "Create Redmine ticket": creates an issue through Redmine's REST interface, pre-filled with subject, summary, link and vulnerability identifiers; stores the issue reference on the match; shows ticket status in the inbox.
  - If an identifier already has a ticket, adds a note instead of a duplicate.
  - Serves as the reference implementation for other ticket-tool plugins.
- **F5 Action rules (S, core, after F3 and F7).** "When a match meets condition X, run action Y", where Y is any registered action: post to a channel, call a webhook, or a plugin action. Example: "our stack" keyword group AND actively exploited → run the ticket action with high priority.
- **F6 Impact map (L, plugin).** Register which audience (client, team, system owner) uses which technology. A match on "Exim" lists who is affected, generates a draft notice from a template, and tracks who has been informed.
- **F7 Store which keywords matched (S, core).** New `match_keywords` table. Prerequisite for routing, rules and reporting.
- **F8 Reporting (S, core, after F2).** Time from publication → alert → actioned. Usable as audit evidence and as a service-quality figure.

### 3B. Smarter signal, less noise

- **F9 Keyword groups, weights and routing (M).** Groups such as "Our stack", "Client stack", "General threat"; a weight per keyword; a channel per group.
- **F10 Exclusion and combination rules (M).** Exclude terms ("webinar", "sponsored"); require-all combinations ("Exim" AND "remote code execution"); title-only and per-source scoping. Still no regular expressions.
- **F11 Vulnerability enrichment (M).** Extract Common Vulnerabilities and Exposures (CVE) identifiers, then add the severity score from the National Vulnerability Database, the exploitation probability from the Exploit Prediction Scoring System, and the "actively exploited" flag from the Known Exploited Vulnerabilities catalogue of the Cybersecurity and Infrastructure Security Agency. Produces one priority score.
- **F12 Story clustering (M).** Group items about the same identifier or near-identical titles across sources. "Reported by 5 sources" becomes a signal, and the chat gets one thread instead of five posts.
- **F13 Keep recent unmatched entries for 7 days (M).** Enables: applying a new keyword retroactively; a "test this keyword" preview; noise statistics.
- **F14 Keyword analytics (S, after F7).** Hits per keyword, noisiest keywords, keywords with no hits in 90 days, thumbs up/down per match to measure precision.
- **F15 Updated-entry detection (S).** Today an entry whose content changes under the same identifier is ignored (`INSERT IGNORE`, `app/poller.py:225`). Detect a changed content hash and post an update.

### 3C. Dynamic watch-list from real inventory

Goal: the keyword list follows what is actually installed, with no manual upkeep.

- **F16 Inventory intake (M, core interface + plugin providers).**
  - A table `inventory_items`: package name, version, distribution and release, host or group, provider, last seen.
  - **Push:** a token-protected endpoint that accepts a package list. A one-line scheduled job per host or a configuration-management task sends the output of `dpkg-query` or `rpm -qa`.
  - **Pull:** provider plugins that read a file, an address, or configuration-management facts on a schedule.
  - Providers are plugins, so each user connects their own source of truth.
- **F17 Automatic keyword lifecycle (M, core).**
  - Inventory items generate keywords tagged `origin=inventory`, kept separate from manual keywords and never overwriting them.
  - A keyword appears when a package is first reported and is retired automatically when no host has reported it for N days.
  - Noise control: an allow-list or minimum host count, plus a deny-list for generic names (`base-files`, `bash`, `python3`) that would match everything.
  - A change log shows what was added and retired, with a dry-run mode before automatic changes are enabled.
- **F18 Debian and Red Hat security trackers as structured sources (M).**
  - Debian Security Tracker and Debian Security Advisories; Red Hat Security Data interface and Red Hat Security Advisories. Both publish machine-readable data; exact endpoints and formats to confirm at implementation.
  - New source type alongside RSS/Atom: a small adapter per tracker that yields normalised advisories (identifier, package, affected releases, fixed version, severity).
- **F19 Version-aware matching (L, after F16 and F18).**
  - Compare the installed version from the inventory with the fixed version from the tracker.
  - Result per advisory: "affected on these hosts", "already fixed" or "not installed". Only "affected" alerts by default.
  - Needs each distribution's own version comparison rules (Debian and RPM order versions differently); use the established libraries rather than custom code.
- **F20 More structured sources (M).** National Vulnerability Database, Known Exploited Vulnerabilities catalogue, GitHub Security Advisories, other distribution trackers, through the same adapter interface as F18.

### 3D. More inputs and outputs

- **F21 JSON interface with tokens (M).** Read matches, sources, keywords, statistics; write triage state; inventory push (F16). Also the base for anything external that wants to query RSSPhaser.
- **F22 Multiple output feeds (M).** One feed per keyword group, audience or priority, each with an optional secret token; JSON Feed alongside RSS.
- **F23 More notification channels (M, plugins).** Daily or weekly email digest; Microsoft Teams; paging for "actively exploited + our stack" outside working hours.
- **F24 Upstream dependency radar (S–M).** Add release feeds of software you run and status-page feeds of providers you depend on. Same pipeline, new use.
- **F25 Bulk source management (S).** Import and export of source lists in the OPML format; curated starter packs.
- **F26 Manual item injection (S).** Add an item found elsewhere so it follows the same triage and action flow.
- **F27 Periodic bulletin (M, plugin, after F6).** Generated summary per audience: what appeared, what was relevant, what was done.
- **F28 Searchable archive (S).** Full-text index on matches: "have we seen this before?"

### 3E. Operate with confidence

- **F29 Heartbeat and silence alerts (S).** Scheduler healthcheck in `docker-compose.yml`; alert when no poll cycle has completed within twice the interval; optional external dead-man's-switch ping.
- **F30 Source health (S).** Alert after N consecutive failures; alert when a source has published nothing for an unusual time; failure history instead of only the last status.
- **F31 Quiet hours and snooze (S).** Mute a keyword, source or story for a period.

## Suggested sequencing

1. **Phase 0** — confirm and stabilise (items 0.1–0.6).
2. **Phase 1** — Foundations, plus S1, S3, S4, S6, S7, S9, S11, S12 (all small), plus F29 and F30.
3. **Phase 2** — S2, S5, S8, S10, S13–S17; P0, P2, P3, P4, P7, P8.
4. **Phase 3** — F7 → F2 → F3 (with P12 and S18) → F1. Result: push alerts with configurable links, an inbox, and the plugin base.
5. **Phase 4** — F4 Redmine plugin, F5 action rules, F8 reporting; F9–F15; P5.
6. **Phase 5** — F21 → F16 → F17 → F18 → F19: the dynamic watch-list with Debian and Red Hat trackers.
7. **Later** — F6, F20, F22–F28, F31, P6.

Delivery rules for a sensitive production system:
- One small, independently revertible change per commit.
- Every new feature behind a setting that defaults to off; every plugin off by default.
- Database changes additive only; backup before each migration.
- Staging first; `/feed.xml` stays unchanged until the push alerts are trusted.

## Parked — outside the phases, to revisit later

Artificial Intelligence (AI) features are deliberately not scheduled. Recorded here so the design above does not block them.

- **A1 Provider-neutral AI module.** One internal interface with three back-ends: Anthropic's interface, any OpenAI-compatible endpoint (covers self-hosted model servers), and "off". Never in the critical path: if AI fails, the plain alert still goes out.
- **A2 AI enrichment.** Two-line summary, "why this matters to us", affected products, suggested priority, draft ticket text, draft notice. All output is a suggestion; a person approves.
- **A3 Model Context Protocol (MCP) server for RSSPhaser.** Tools such as `list_matches`, `get_match`, `set_triage_state`, `test_keyword`, and any registered plugin action. Lets Claude Code, or any compatible client including one backed by a self-hosted model, run triage. Builds on F21.
- **A4 Scheduled briefing.** A scheduled agent run that posts a morning summary.
- **Requirements already stated by the owner:** must support Claude Code integration and self-hosted models.
- **Safeguard to keep:** article text is untrusted input and may contain instructions aimed at the model. The summarising model gets no tools; actions go through approval.
- **What the phases already provide for this:** F21 (JSON interface), F3 (events and actions), F7 (matched keywords). No rework is expected when this is picked up.

## Critical files

- `app/scheduler.py` — paused-job fix, heartbeat, per-source intervals.
- `app/web.py` — login hardening, CSRF, redirect check, health output, matches inbox, JSON interface.
- `app/auth.py` — rate limit, second factor, session lifetime.
- `app/poller.py` — size cap, SSRF guard, single lower-casing, HTML stripping, matched-keyword storage, event emission.
- `app/matching.py` — reuse `text_matches` / `entry_matches`; extend with exclusion and combination rules.
- `app/feed.py` — caching; reuse `build_feed_xml` for per-group feeds.
- `app/models.py`, `app/db.py` — new tables, Alembic; reuse the `AppState` key/value helpers (`_state_int`, `set_retention` pattern) for new settings.
- `app/templates/*` — CSRF tokens, static assets, matches inbox, link-template and plugin settings.
- `Dockerfile`, `docker-compose.yml` — multi-stage build, hardening, port binding, scheduler healthcheck, outbox worker.
- New in core: `app/events.py`, `app/outbox.py`, `app/plugins.py`, `app/links.py`, `app/notify/`, `app/sources/` (adapter interface, Debian and Red Hat adapters), `app/inventory.py`, `app/enrich/`.
- New outside core: `plugins/redmine/`, `plugins/inventory_*/`, `tests/`, `.github/workflows/`, `migrations/`.

## Verification

- **Paused-job fix:** unit test asserting the poll job's next run time is set after `start()`; in staging with a 1-minute interval, confirm at least three "Poll cycle starting." log lines one minute apart.
- **End-to-end smoke test** (performed once on 2026-11-10 against the current code): build with Docker Compose, then check `/healthz`, `/feed.xml`, login failure and success, keyword add/toggle/delete, logout, and one full poll cycle.
- **Security items:** one test per finding (for example: `next=//example.org` redirects to the dashboard; a sixth failed login is refused; a POST without a CSRF token returns 400; a source pointing at `127.0.0.1` is rejected).
- **Performance items:** record P0 metrics before and after each change; a change with no measured gain is reverted.
- **Plugin isolation:** the full test suite passes with the `plugins/` folder removed; a plugin that raises an error does not delay or lose a match.
- **Link templates:** placeholders containing `&`, `#`, quotes and non-ASCII text arrive intact and encoded.
- **Dynamic watch-list:** report a package → keyword appears; stop reporting → keyword retires after N days; manual keywords untouched throughout.
- **Integrations:** each tested against a sandbox first (a test chat channel, a test Redmine project).

## Not verified — needs input or production access

- Whether production polling is currently paused (0.1).
- Whether production secrets differ from the examples (0.3).
- Whether port 8000 is reachable without the proxy (0.4).
- Number of sources, keywords and feed requests in production; all performance rankings depend on this.
- Slack RSS app's fetch interval; no confirmed figure.
- Redmine version, whether its REST interface is enabled, and the exact pre-fill parameters it accepts.
- Exact endpoints and formats of the Debian and Red Hat trackers (F18).
- Where the installed-software inventory lives today (decides the first F16 provider).
- P11 (silent truncation of bodies above 64 KB).
