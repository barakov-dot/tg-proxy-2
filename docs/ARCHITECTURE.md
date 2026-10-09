# Architecture

One service, one process: `python -m tgpanel.main` (`tgpanel.service`, root). It owns the SQLite
database and the apply lock. Spec: [PLAN.md](../PLAN.md). User docs: [README.md](../README.md).

## Layers (imports go downwards only)

```
web/ (FastAPI+Jinja2+HTMX)   bot/ (aiogram 3)   scheduler/   collector/        <- entry points
                      services/   (the only API for the entry points)
                      apply/      (pipeline, backup/rollback, importer, settings_spec)
         domain/ (pure)   render/ (pure)   db/ (SQLite, repo)   system/ (SystemOps)
```

- `domain` and `render` are pure: no I/O, no system access (pool/address allocation, expiry rules,
  counter deltas, profile/config/nft/Caddy rendering).
- `system` is the only place that touches the OS: `SystemOps` (interface), `RealSystemOps`,
  `FakeSystemOps` (in-memory files, units, nft sets, failure injection) and `ShellTools` (git, pip,
  journal for the ops CLI). Commands are argument lists, never a shell.
- `services` are the scenarios (create/disable/extend/delete users, requests, broadcast, settings,
  traffic, dashboard, backups, admin). Web, bot and scheduler talk to services only; services never
  touch the system directly, they go through the apply pipeline.
- `web/deps.py` defines ports (`TrafficPort`, `RequestsPort`, `BroadcastPort`) so the web layer never
  imports the bot or the collector; `web/adapters.py` and `bot/broadcast_port.py` implement them.

## Composition (`tgpanel/main.py`)

`compose(env, ops)` builds `AppContext` (db + pipeline + users + settings + importer), the bot
`Runtime` (requests, broadcast, scheduler, notifier; one shared message rate limiter), the
`Collector`, `TrafficService`/`DashboardService` and the FastAPI app. `prepare` runs
`startup_recovery`, loads the proxy host name and registers the bootstrap admins from
`TGPANEL_ADMIN_IDS` on the first start only (`install.bootstrapped`). `run_stack` runs the components
under a `Supervisor`: web (uvicorn, 127.0.0.1 only, no access log), collector, bot (long polling),
scheduler. Each has its own restart loop with exponential backoff and scrubbed logs, so a crash of
one never stops the others. SIGTERM/SIGINT set one stop event; components stop, an in-flight apply is
awaited (bounded), the database is closed, exit code 0. The end-to-end tests (`tests/e2e`) drive this
same composition over `FakeSystemOps`.

## The apply pipeline (`apply/pipeline.py`)

`pipeline.run_operation(mutation, reason=..., actor=...)` is the only way to change anything that
affects the proxy. Operations are queued; operations that arrive while one is running are coalesced
into the next batch (one apply for many operations). For one batch, under the cross-process lock:

1. open the DB transaction, run the mutations (the lock is held until commit/rollback);
2. render the desired state (`profiles.json`, `limits` in `config.json`, pool env files, nft table);
3. backup of the current files, journal written (crash recovery data);
4. validate (`tproxy-server -check`, nft/Caddy syntax) before touching anything;
5. atomic writes, pool (MTProxy) units, relay restart, `/healthz` and `/readyz`;
6. commit; on any error: restore files and units from the backup, restart, roll the DB transaction
   back, record the failed run (`apply_runs`), call the `on_failure` hook (admin notification, banner).

Links are returned by services only after a successful apply. `/readyz` is called by the apply only
(it dials every backend and would create false activity), never polled. `startup_recovery` restores
files of an apply that was interrupted by a crash, using the journal.

## The DB-write rule

All DB writes outside an apply operation go through `await pipeline.db_write(fn, ...)`. It takes the
same asyncio lock the apply transaction holds from BEGIN to COMMIT/ROLLBACK, so writers never meet
"database is locked", never wait for SQLite's busy timeout and are never swallowed into an apply
transaction. Waiting is bounded (30 s by default) and ends in `DbWriteTimeout` with a clear message;
the collector uses a shorter bound and drops a sample instead of blocking. Reads may use `db.run`.
Schema and migrations: `db/` (`migrations/`, `repo.py`).

## Collector, scheduler, bot

- `collector.Collector.poll_once` reads `nft list set` counters (never `/readyz`), turns them into
  deltas (`domain.counters`, reset-safe) and writes minute buckets and activity flags in ONE
  `db_write`; `rollup.maybe_rollup` folds minutes into hours and days once a day.
- `scheduler.Scheduler.tick` expires users (one apply for all), sends expiry notices and reminders,
  runs the rollup hook and the daily backup.
- `bot`: dispatcher with throttle/private-only/log middlewares; the same `RequestService` instance is
  shared with the web panel through `RequestsPortAdapter`.

## Configuration and state

- `/etc/tgpanel/tgpanel.env` (0600): `TGPANEL_BOT_TOKEN`, `TGPANEL_ADMIN_IDS`, `TGPANEL_PANEL_DOMAIN`,
  `TGPANEL_PANEL_PATH`, `TGPANEL_SECRET_KEY` (>= 32 chars, else the service refuses to start),
  `TGPANEL_DB`, `TGPANEL_LISTEN` (loopback only), optional `TGPANEL_ENV_FILE`.
- SQLite settings (`apply/settings_spec.py`: typed registry, shown on the settings page) and the
  tables of `db/migrations`.
- Logging: root INFO with a scrubbing filter (proxy secrets, bot tokens, the signing key); aiogram,
  aiohttp, httpx and uvicorn access loggers are WARNING; DEBUG is never emitted.

## Testing

Unit tests per package; `tests/e2e` composes the real app over `FakeSystemOps` and a temporary
SQLite file (web through httpx ASGI transport, bot through an aiogram dispatcher with a mocked session,
collector/scheduler through `poll_once`/`tick`), with a global guard that scans logs, audit rows,
`apply_runs`, system calls and HTML for leaked secrets and counts `/readyz` calls.
