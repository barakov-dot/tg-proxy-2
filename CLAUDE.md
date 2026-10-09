# tg-proxy-2

Web panel + Telegram bot over an installed tproxy-server. Full spec: PLAN.md (single source of truth).

## Stack
Python 3.12+, FastAPI, aiogram 3, SQLite (WAL), Jinja2 + HTMX. Deploy target: Ubuntu 24.04 / Debian 13, root.

## Commands
- Setup: `uv venv --python 3.12 ~/.venvs/tgpanel && uv pip install --python ~/.venvs/tgpanel/bin/python -e ".[dev]"`
  (venv OUTSIDE the Nextcloud-synced folder; it breaks there).
- Full gate: `V=~/.venvs/tgpanel/bin; $V/ruff check . && $V/ruff format --check tgpanel tests && $V/mypy && $V/pytest -q && shellcheck install.sh scripts/*.sh`
- Service entry point: `python -m tgpanel.main` (never run it locally against a real system; tests use
  `tgpanel.main.compose` over `FakeSystemOps`). CLI: `python -m tgpanel.cli <command>` (installed as `tgpanel`):
  status, apply, import, backup, restore, legacy-mtproxy, doctor, repair, update, uninstall, show-url,
  reset-password (+ internal for install.sh: bootstrap, caddy-install, pre-install-backup, migrate).
- Docs: README.md (user, Russian), docs/INSTALL.md, docs/ARCHITECTURE.md, docs/UPSTREAM.md, PLAN.md (spec).

## Gotchas
- Tests: `tests/e2e` is the integration suite (real composed app over `FakeSystemOps`, global secret/`/readyz`
  guard in `tests/e2e/conftest.py`); other packages have focused unit tests. Fake ops: `FakeSystemOps.fail_on`,
  `add_traffic`, `seed_upstream("clean"|"owner")`. Fake clock `tests.apply.conftest.Clock` ticks +1 s per call.
- Service env (`/etc/tgpanel/tgpanel.env`): TGPANEL_BOT_TOKEN, TGPANEL_ADMIN_IDS (first start only),
  TGPANEL_PANEL_DOMAIN, TGPANEL_PANEL_PATH, TGPANEL_SECRET_KEY (>= 32 chars or the service refuses to start),
  TGPANEL_DB, TGPANEL_LISTEN (loopback only), TGPANEL_ENV_FILE. Empty/invalid bot token = bot disabled, rest runs.
- Every component runs under `main.Supervisor` (own restart loop); keep new background work inside one.
- `ruff format` rewrites files: run it only on `tgpanel tests`.

## Rules
- Locally only unit tests and linters run. Never start tproxy-server/MTProxy/Caddy/nftables/systemd here.
- All system access goes through `SystemOps` (real + fake). `domain` and `render` are pure.
- DB writes outside apply operations go ONLY through `await pipeline.db_write(fn, ...)` (it takes the
  lock held by the apply transaction; 30 s timeout -> `DbWriteTimeout`); reads may use `db.run`.
  Proxy-affecting changes use `pipeline.run_operation(mutation, ...)`. See `tgpanel/db/__init__.py`.
- Layers: services are the only API for web/bot/scheduler; they must not touch the system directly.
- Never poll `/readyz` outside the apply pipeline; reading traffic/dashboard must not call it (e2e asserts this).
- After each task with green ruff/mypy/pytest/shellcheck: conventional commit, then `git push origin main`.
- Code, identifiers, comments, commit messages in English; UI texts in Russian (single texts module).
- Never commit secrets, tokens, real domains, or /etc contents. Fixtures are synthetic only.

## Prohibitions (PLAN.md section 12)
- External commands only as argument lists (no shell); profile names only `u<id>`.
- Panel listens on 127.0.0.1 only. No Caddy access logs. No secrets in logs/audit/errors/URLs.
- Proxy files are written only via the apply pipeline (backup + rollback); only `profiles.json`,
  `limits` in `config.json`, and our Caddyfile block may be changed.
- Never poll `/readyz` in a loop. Dependencies pinned with hashes; frontend assets vendored.
