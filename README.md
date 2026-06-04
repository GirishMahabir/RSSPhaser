# 🛰️ RSSPhaser

A self-contained RSS/Atom **keyword filter**. It polls a list of noisy security
feeds on a schedule, keeps only the entries that match your keyword list,
stores the matches in MariaDB, and re-serves them as a single clean RSS 2.0 feed
you can subscribe to in any reader.

Built with **Python 3.12, Flask, APScheduler, feedparser, feedgen, SQLAlchemy +
mysqlclient, MariaDB, and gunicorn**. No Redis, no other datastore.

---

## Quick start (one shot)

```bash
# 1. Configure
cp .env.example .env

# 2. Generate a bcrypt hash for your admin password and paste it into .env
#    (this builds the image the first time):
docker compose run --rm app python gen_password_hash.py 'your-strong-password'
#    -> copy the printed hash into ADMIN_PASSWORD_HASH in .env
#    Also set SECRET_KEY to a long random string:
#      python -c "import secrets; print(secrets.token_hex(32))"

# 3. Bring it all up
docker compose up --build
```

Then open:

- **Admin UI:** http://localhost:8000/  (log in with `ADMIN_USER` + your password)
- **Filtered feed (subscribe here):** http://localhost:8000/feed.xml
- **Health check:** http://localhost:8000/healthz

That's it — no manual schema setup, no seeding step. On first boot the schema is
created automatically and the initial sources/keywords/poll interval are seeded
from `config.yaml`.

---

## How it works

```
            ┌─────────────┐   poll every N min   ┌──────────────┐
 RSS/Atom   │  scheduler  │ ───────────────────► │   MariaDB    │
 feeds  ───►│  container  │  fetch→match→store   │  (matches)   │
            └─────────────┘                      └──────┬───────┘
                                                        │ serve
                                                 ┌──────▼───────┐
            you (RSS reader) ◄────/feed.xml──────│ app (gunicorn)│
                                                 │  + admin UI   │
                                                 └──────────────┘
```

### Matching logic
- Keywords are **plain literal terms — no regular expressions** — matched
  **case-insensitively**. There is deliberately no regex engine in the matching
  path, so catastrophic backtracking / ReDoS is impossible by construction:
  matching is linear-time and can never hang, no matter what an admin types.
- Each keyword has a **mode**:
  - **`word`** (default) — whole-word match. The term must be bounded by a
    non-alphanumeric character (or a string edge) on both ends. `ai` matches the
    standalone word "ai" but not "em**ai**l". Works for multi-word terms too
    (`data breach` matches "a data breach." but not "meta**data breach**y").
  - **`contains`** — plain substring; matches inside larger words. Ideal for
    prefixes like `CVE-`, which catches every advisory number with one keyword.
- For each entry the **title is checked first**. If any keyword matches the
  title, the entry is kept and the body isn't checked. Otherwise the
  **body/summary** is checked. A match in either stage keeps the entry.
- The body is **truncated** (default 100 KB, `BODY_TRUNCATE_BYTES`) before
  matching, purely to bound memory on pathologically large payloads.

> **Why no regex?** Regex gave power we didn't need (you almost never need
> backreferences to filter security headlines) at the cost of a real stability
> risk — a single bad pattern can pin a CPU for an exponential amount of time.
> Literal keyword modes cover essentially every real filtering need with zero
> attack surface. Translating the classic patterns is easy: `CVE-\d{4}-\d{4,7}`
> → `CVE-` (contains); `critical (vulnerability|flaw|bug)` → three keywords;
> `ransomware` → `ransomware` (word).

### Deduplication & retention
- Each entry gets a stable **dedup id** (the entry's `id`/`guid`, else a hash of
  link+title). A **UNIQUE constraint** plus `INSERT IGNORE` semantics means
  concurrent or retried inserts can never create duplicate matches.
- The `matches` table is indexed on date for fast feed serving.
- **Retention** prunes matches beyond a configurable count
  (`RETENTION_MAX_MATCHES`, default 5000) and/or age (`RETENTION_MAX_AGE_DAYS`,
  default 90) so the table never grows unbounded.

### Config vs. UI (first-boot seeding)
`config.yaml` seeds `sources`, `keywords`, and the poll interval **only on the
first boot**, guarded by an `initialized` flag stored in the database. After
that, **the web UI is the source of truth** and your edits persist to MariaDB —
re-seeding will never overwrite UI changes on later boots.

---

## Managing the app

### Sources
Dashboard → **Sources**. Add/edit/delete feed URLs and toggle each one enabled.

### Keywords
**Keywords** page. Add/edit/delete literal keywords, choose a match mode
(`whole word` or `contains`), and toggle each enabled.

### Poll interval
**Overview** → **Polling**. Change the minutes between polls; the scheduler
picks up the change within ~30 seconds — **no restart required**.

### Retention (keeping the database bounded)
**Overview** → **Retention**. Two limits keep the `matches` table from growing
forever, editable live in the UI (no restart):

- **Max items to keep** — keep only the newest N matches.
- **Max age (days)** — drop matches older than N days.

Set either to **0** to disable that dimension. Cleanup runs **automatically at
the end of every poll cycle**, so it's effectively on your poll interval; you
can also click **Run cleanup now** to prune on demand. The panel shows the
current item count and the on-disk size of the matches table so you can tune the
limits. (The `config.yaml` / env values are just the initial defaults; once set,
the UI values win.)

### Dashboard / status
Per-source last poll time, last-run duration, success/error state, last error
message and last match count; plus totals for matches stored and active
sources/keywords.

---

## ⚠️ The single-scheduler guarantee (read this if it double-polls)

> The most error-prone part of this kind of app is starting APScheduler
> **exactly once** under gunicorn. If every gunicorn worker started its own
> scheduler you'd get duplicated polling.

RSSPhaser avoids this **structurally**: the scheduler runs in its **own
dedicated container** (`scheduler` service in `docker-compose.yml`, command
`python scheduler_main.py`). The gunicorn web containers never import or start
APScheduler at all. So no matter how many web workers run, there is exactly one
poller.

**If you ever see double-polling, this is the first place to look:**
- Make sure the `scheduler` service is **not scaled above 1 replica**
  (`docker compose up --scale scheduler=1`, which is the default).
- Confirm only the `scheduler` container logs `Scheduler started; poll
  interval = N minute(s).` The `app` containers must never log that line.

---

## Configuration reference

Secrets and connection settings come from **environment variables** (`.env`);
operational tunables come from `config.yaml` and can be **overridden** by the
matching env var.

| Setting | Env var | config.yaml | Default |
|---|---|---|---|
| Admin username | `ADMIN_USER` | — | — (required) |
| Admin password (bcrypt) | `ADMIN_PASSWORD_HASH` | — | — (required) |
| Flask session secret | `SECRET_KEY` | — | — (required) |
| DB host/port/name/user/pass | `DB_*` | — | see `.env.example` |
| Poll interval (min) | `POLL_INTERVAL_MINUTES` | `poll_interval_minutes` | 15 |
| Body truncation (bytes) | `BODY_TRUNCATE_BYTES` | `body_truncate_bytes` | 102400 |
| Output item cap | `OUTPUT_ITEM_CAP` | `output_item_cap` | 200 |
| Retention max matches | `RETENTION_MAX_MATCHES` | `retention_max_matches` | 5000 |
| Retention max age (days) | `RETENTION_MAX_AGE_DAYS` | `retention_max_age_days` | 90 |
| HTTP connect timeout (s) | `HTTP_CONNECT_TIMEOUT_SECONDS` | `http_connect_timeout_seconds` | 10 |
| HTTP read timeout (s) | `HTTP_READ_TIMEOUT_SECONDS` | `http_read_timeout_seconds` | 20 |
| HTTP retries | `HTTP_MAX_RETRIES` | `http_max_retries` | 3 |
| Poll concurrency | `POLL_MAX_WORKERS` | `poll_max_workers` | 5 |
| Initial sources | — | `sources` | (5 examples) |
| Initial keywords | — | `keywords` | (5 examples) |

The app **validates `config.yaml` and all required env vars at startup** and
fails fast with a clear message if anything is missing or malformed.

---

## Generating the admin password hash

```bash
# Inside Docker (recommended — uses the same bcrypt as the app):
docker compose run --rm app python gen_password_hash.py 'your-password'

# Or locally if you have Python + bcrypt installed:
python gen_password_hash.py 'your-password'

# Interactive (hidden prompt):
python gen_password_hash.py
```

Paste the printed `$2b$...` string into `ADMIN_PASSWORD_HASH` in `.env`, and
**wrap it in single quotes**:

```dotenv
ADMIN_PASSWORD_HASH='$2b$12$....'
```

> ⚠️ This matters. bcrypt hashes contain `$` characters. Without single quotes,
> Docker Compose interpolates segments like `$12` / `$rk...` as variables and
> **silently corrupts the hash**, so login fails with seemingly-correct
> credentials. Single quotes make Compose treat the value literally. (`SECRET_KEY`
> with random hex doesn't need quotes, but quoting it does no harm.)

---

## Operations

- **Health:** `/healthz` checks DB connectivity and is wired to the Docker
  healthcheck for the `app` service.
- **Logging:** structured logs to stdout (`docker compose logs -f`), including
  per-poll-phase timestamps and per-source durations.
- **Graceful shutdown:** the scheduler handles SIGTERM/SIGINT and stops
  APScheduler cleanly. The poll job uses `max_instances=1` + `coalesce=True` so
  slow polls never overlap or stack.
- **Non-root:** containers run as an unprivileged `appuser`.
- **Resilience:** an unreachable or malformed feed records its error for the
  dashboard but never crashes the poll cycle or the app; conditional requests
  (`ETag`/`If-Modified-Since`) make polling cheap, and `304 Not Modified` is a
  no-op.

---

## Subscribe

Point any RSS reader at:

```
http://<host>:8000/feed.xml
```

(No authentication required for the feed; the admin UI is session-protected.)

---

## Local development (without Docker)

You'll need a reachable MariaDB and the env vars set. Then:

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)   # or set vars manually
gunicorn --bind 0.0.0.0:8000 wsgi:app       # web
python scheduler_main.py                    # scheduler (separate terminal)
```
