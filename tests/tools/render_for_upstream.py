"""Render the files our code produces so that REAL upstream tools can check them.

Usage: python tests/tools/render_for_upstream.py OUTDIR

For every scenario a directory ``OUTDIR/<name>/`` is written:

    config.json            patched relay config (absolute paths, runnable locally)
    profiles.json          rendered profiles
    Caddyfile              upstream-style Caddyfile + our panel block
    tgpanel.nft            rendered nft ruleset
    tgpanel-mtproxy@.service, pool-<n>.env, tgpanel-firewall.service
    site/index.html, token.key   (what `tproxy-server -check` wants to find)

Scenarios: the ``clean`` and ``owner`` fixtures with a few users, and 300 users in 19 pools at
``max_sessions_global`` 128 / 1024 / 4096 (the relay refuses budgets it cannot hold: that is
exactly what the check is for).

CI then runs, per scenario:
    tproxy-server -config C -profiles-file P -check      (built from upstream at a pinned commit)
    caddy validate --config Caddyfile --adapter caddyfile
    unshare -Urn nft -c -f tgpanel.nft
    systemd-analyze verify <unit files>
Locally only this generator (and, with Go installed, step 1) is runnable.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tgpanel.domain.models import (  # noqa: E402
    DesiredState,
    PoolRecord,
    RelayLimits,
    UserRecord,
    UserStatus,
)
from tgpanel.render.bundle import render_all  # noqa: E402
from tgpanel.render.caddy import insert_panel_block  # noqa: E402
from tgpanel.render.nft import render_firewall_unit  # noqa: E402
from tgpanel.render.pools import extract_mtproxy_facts  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "upstream"
PANEL_DOMAIN = "panel.example.com"
PANEL_PATH = "Abcdefghijklmnopqrstuv123456"
SENTINEL = "f" * 32


def secret(n: int) -> str:
    return hashlib.sha256(f"user-{n}".encode()).hexdigest()[:32]


def make_users(count: int, per_pool: int = 16) -> tuple[UserRecord, ...]:
    return tuple(
        UserRecord(
            id=i,
            name=f"user{i}",
            secret=secret(i),
            status=UserStatus.ACTIVE,
            pool_id=(i - 1) // per_pool + 1,
            loopback_ip=f"127.64.{(i - 1) // 250}.{(i - 1) % 250 + 1}",
            carrier_mode=None,
            expires_at=None,
        )
        for i in range(1, count + 1)
    )


def make_pools(count: int) -> tuple[PoolRecord, ...]:
    return tuple(PoolRecord(i, 2400 + i - 1, 8900 + i - 1) for i in range(1, count + 1))


def scenarios() -> list[tuple[str, str, DesiredState]]:
    out: list[tuple[str, str, DesiredState]] = []
    for variant in ("clean", "owner"):
        users = make_users(5)
        state = DesiredState(users=users, pools=make_pools(1), sentinel_secret=SENTINEL)
        out.append((variant, variant, state))
    for sessions in (128, 1024, 4096):
        state = DesiredState(
            users=make_users(300),
            pools=make_pools(19),
            sentinel_secret=SENTINEL,
            relay_limits=RelayLimits(sessions, 16384),
        )
        out.append((f"big-{sessions}", "clean", state))
    return out


def facts_for(variant: str):  # type: ignore[no-untyped-def]
    base = FIXTURES / variant
    unit = (base / "mtproxy.service").read_text()
    dropins = [p.read_text() for p in sorted((base / "mtproxy.service.d").glob("*.conf"))]
    env = (base / "mtproxy.env").read_text()
    return extract_mtproxy_facts(unit, dropins, env)


def write(path: Path, data: bytes | str, mode: int = 0o644) -> None:
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)


def render_scenario(out: Path, name: str, variant: str, state: DesiredState) -> None:
    directory = out / name
    (directory / "site").mkdir(parents=True, exist_ok=True)
    write(directory / "site" / "index.html", "<!doctype html><title>site</title>\n")
    write(directory / "token.key", hashlib.sha256(name.encode()).digest(), 0o400)
    config_in = (FIXTURES / variant / "config.json").read_bytes()
    rendered = render_all(state, config_in, facts_for(variant))
    config = json.loads(rendered.config_json)
    config["public_dir"] = str(directory / "site")
    config["token_key_file"] = str(directory / "token.key")
    config["profiles_file"] = str(directory / "profiles.json")
    write(directory / "config.json", json.dumps(config, indent=2) + "\n")
    write(directory / "profiles.json", rendered.profiles_json, 0o600)
    caddyfile = (FIXTURES / variant / "Caddyfile").read_text()
    write(directory / "Caddyfile", insert_panel_block(caddyfile, PANEL_DOMAIN, PANEL_PATH))
    write(directory / "tgpanel.nft", rendered.nft_file)
    write(directory / "tgpanel-mtproxy@.service", rendered.pool_unit)
    write(directory / "tgpanel-firewall.service", render_firewall_unit())
    for pool_id, env in rendered.pool_envs.items():
        write(directory / f"pool-{pool_id}.env", env)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = Path(argv[1])
    for name, variant, state in scenarios():
        render_scenario(out, name, variant, state)
        print(f"rendered {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
