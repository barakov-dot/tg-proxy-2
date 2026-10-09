# tg-proxy-2

Web panel + Telegram bot over an installed tproxy-server. Full spec: PLAN.md (single source of truth).

## Stack
Python 3.12+, FastAPI, aiogram 3, SQLite (WAL), Jinja2 + HTMX. Deploy target: Ubuntu 24.04 / Debian 13, root.

## Commands
- Setup: `uv venv --python 3.12 ~/.venvs/tgpanel && uv pip install --python ~/.venvs/tgpanel/bin/python -e ".[dev]"  (venv outside the Nextcloud-synced folder; it breaks there)`
- Lint/types/tests: `ruff check . && mypy && pytest`
- Shell: `shellcheck install.sh scripts/*.sh`

## Rules
- Locally only unit tests and linters run. Never start tproxy-server/MTProxy/Caddy/nftables/systemd here.
- All system access goes through `SystemOps` (real + fake). `domain` and `render` are pure.
- Layers: services are the only API for web/bot/scheduler; they must not touch the system directly.
- After each task with green ruff/mypy/pytest/shellcheck: conventional commit, then `git push origin main`.
- Code, identifiers, comments, commit messages in English; UI texts in Russian (single texts module).
- Never commit secrets, tokens, real domains, or /etc contents. Fixtures are synthetic only.

## Prohibitions (PLAN.md section 12)
- External commands only as argument lists (no shell); profile names only `u<id>`.
- Panel listens on 127.0.0.1 only. No Caddy access logs. No secrets in logs/audit/errors/URLs.
- Proxy files are written only via the apply pipeline (backup + rollback); only `profiles.json`,
  `limits` in `config.json`, and our Caddyfile block may be changed.
- Never poll `/readyz` in a loop. Dependencies pinned with hashes; frontend assets vendored.
