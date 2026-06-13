# Campground Monitor

A self-hosted web app that watches **recreation.gov** for campsite and wilderness-permit
availability and notifies you when something opens up. Point it at any US campground or permit,
set your dates, and it polls on an interval and pushes an alert when a spot frees up — handy for
catching cancellations on sold-out sites.

Built with FastAPI + HTMX, runs in Docker.

## What it does
- Monitors **campgrounds** (multi-night stays, by check-in/check-out) and **wilderness permits**
  (single entry date, by party size and trailhead).
- Searches ~5,800 reservable recreation.gov facilities by state, agency, and name.
- Polls each monitor on its own interval, dedupes alerts, and auto-stops once the trip is over.
- Notifies via **ntfy** push and/or **email**.
- Is **multi-user**: everyone logs in and manages their own monitors; one admin manages users.

## How the codebase is organized

There are two interfaces over one shared core:

1. **Web UI** (`app/`) — the main application: FastAPI + Jinja2 templates + HTMX, Dockerized.
2. **CLI** (`monitor.py`) — an optional, Windows-only standalone script for quick command-line
   monitoring. It reuses the same recreation.gov logic but is **not** part of the Docker image
   (it plays a sound via `winsound`). You can ignore it entirely if you only want the web app.

### The web app, module by module

- **`app/main.py`** — the entry point. A `create_app()` factory wires up routes and runs a
  *lifespan*: on startup it resumes every monitor that was `running`, and on shutdown it stops
  them cleanly. This is what makes monitors survive a container restart.
- **`app/config.py`** — every setting, read from environment variables with sensible defaults.
  Start here to see what's configurable. Also holds the browser-like HTTP headers used to talk
  to recreation.gov.
- **`app/routes/`** — the HTTP layer, one file per area:
  `auth_routes` (login/logout), `dashboard`, `wizard` (the monitor-creation flow), `logs`
  (per-monitor check history), `admin` (user management), and `api` (the HTMX partials and the
  monitor create/update/delete endpoints).
- **`app/monitor_manager.py`** — the engine room. It owns one `asyncio` task per running monitor,
  persists configs to JSON, dedupes alerts with a cooldown, and runs the auto-stop logic. Sync
  HTTP calls are pushed onto threads (`asyncio.to_thread`) so they don't block the event loop.
- **`app/monitor_engine.py`** — the recreation.gov integration itself: `check_campground()` and
  `check_permit()` plus the notification senders (ntfy, email). This is the layer the CLI shares.
- **`app/ridb.py`** — loads the facility catalog (a CSV export from recreation.gov's RIDB) into
  memory at startup and serves the search/filter queries behind the creation wizard.
- **`app/auth.py`** + **`app/user_store.py`** — auth. Sessions are signed cookies
  (`itsdangerous`); users live in a JSON file with bcrypt-hashed passwords. `auth.py` also holds
  the guards that gate routes by login, admin role, and monitor ownership.

### How state is stored

Everything is plain JSON on disk under `data/` (bind-mounted, so it outlives container
rebuilds): `monitors.json` holds the monitor configs and `users.json` holds the accounts. There
is no database — the write volume is tiny and this keeps backup/restore trivial. Both the user
store and the monitor manager write via a temp-file-then-rename pattern so a crash mid-write
can't corrupt the file.

### Campgrounds vs. permits

The two facility kinds hit different recreation.gov endpoints and have different shapes, and
that split runs through the whole codebase:

| | Campground | Permit |
|---|---|---|
| Endpoint | `/api/camps/availability/campground/{id}/month` | `/api/permitinyo/{id}/availabilityv2` |
| Dates | check-in + check-out (multi-night) | single entry date |
| Filter | — | party size, optional trailhead/division |
| Result | sites (site id, loop, type) | divisions (remaining vs. total quota) |

A stay spanning two months makes two campground calls and merges them. Permit support targets
"permitinyo"-style permits (e.g. Yosemite Wilderness); lottery-only permits (e.g. Half Dome) use
a different API and won't return availability. Availability checks need no API key — just the
browser-like headers in `config.py`.

### Monitor lifecycle (the core loop)

1. You create a monitor in the wizard → it's saved to `monitors.json` and an async task starts.
2. The task polls recreation.gov on the configured interval.
3. Newly available sites/divisions fire a notification, deduped with a 15-minute cooldown.
4. A monitor **auto-stops ~36 hours after its trip window ends**, so a forgotten monitor doesn't
   keep polling dates in the past.
5. On restart, monitors still marked `running` resume via the lifespan hook in `main.py`.

## Project layout
```
campground-monitor/
├── app/
│   ├── main.py              # app factory + startup/shutdown lifecycle
│   ├── config.py            # all settings, from environment variables
│   ├── auth.py              # session cookies + login/admin/owner guards
│   ├── user_store.py        # user CRUD (JSON + bcrypt), first-boot admin seed
│   ├── monitor_engine.py    # recreation.gov checks + notification senders
│   ├── monitor_manager.py   # async task lifecycle, JSON persistence, auto-stop
│   ├── ridb.py              # in-memory facility catalog from the CSV export
│   ├── routes/              # auth, dashboard, wizard, logs, admin, api
│   ├── templates/           # Jinja2 templates + HTMX partials
│   └── static/style.css
├── monitor.py               # optional standalone CLI (Windows only)
├── Dockerfile               # builds the app image (Python 3.11-slim)
├── docker-compose.yml       # `app` service + optional Cloudflare `tunnel` sidecar
├── cloudflared-camp.yml     # tunnel config (edit for your own tunnel/domain)
└── .env.example             # copy to .env and fill in
```
Two things you supply yourself (both git-ignored): the RIDB CSV catalog and the `data/`
directory (created on first run).

## Running it

It's all Dockerized, so the short version is:

```bash
cp .env.example .env        # then edit it (see below)
docker compose up -d app    # serves on http://localhost:8002
```

A few things to know:

- **You need the RIDB catalog before it'll start.** The app loads a CSV export of recreation.gov
  facilities at boot. Grab the full export from <https://ridb.recreation.gov/>, unzip it into a
  `RIDBFullExport_V1_CSV/` folder at the repo root, and you're set. Without it the app won't
  come up (and the search wizard would have nothing to show anyway).
- **Configure via `.env`.** Copy `.env.example` and fill it in. The ones that matter:
  `AUTH_USERNAME` / `AUTH_PASSWORD` seed your admin login on first boot (after that, manage users
  from `/admin`), and `SESSION_SECRET` should be set to a random string. Email alerts need a
  Gmail App Password in `EMAIL_FROM` / `EMAIL_PASSWORD`; leave those blank to skip email.
- **First login** uses the `AUTH_USERNAME` / `AUTH_PASSWORD` you set.

**Public access (optional).** `docker-compose.yml` includes a Cloudflare Tunnel sidecar so you
can expose the app on your own domain over HTTPS without opening ports. It's currently pointed at
the original author's tunnel — to use it, create your own tunnel, point `cloudflared-camp.yml`
at it (tunnel ID + your hostname), and set the tunnel env vars noted in `.env.example`. If you
just want it on your LAN, ignore the tunnel and use `docker compose up -d app`.

## Tech stack
FastAPI · Uvicorn · Jinja2 · HTMX · itsdangerous (signed session cookies) · bcrypt · requests ·
Docker. Python 3.11 in the image.
