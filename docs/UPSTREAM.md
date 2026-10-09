# Upstream facts: telegramdesktop/tproxy-server

Pinned at upstream `master` commit `c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8` (2026-09-29).
Upstream is a proof of concept and changes quickly: re-check before relying on any detail.

The authoritative description of upstream behaviour used by this project is section 2 of
[PLAN.md](../PLAN.md). Items marked "[ПРОВЕРИТЬ НА СЕРВЕРЕ]" there are NOT verified and are
covered by `scripts/server-selfcheck.sh`.

Summary of what the design depends on:

- Relay reads `profiles.json` once at start; any change requires `systemctl restart tproxy-server`.
- `profiles.json` is parsed with unknown fields rejected; 1..`limits.max_profiles` profiles required.
- `backend` may be any numeric loopback address + port (all of 127.0.0.0/8).
- Official MTProxy accepts at most 16 secrets (`-S`) per process and listens on 0.0.0.0.
- Caddy runs with `admin off`: changes need `systemctl restart caddy`.
- `/metrics` exposes only global counters; `/readyz` dials every profile backend.
- `config.json` must never be generated from scratch: only `limits` keys are edited.
