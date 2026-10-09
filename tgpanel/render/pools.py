"""MTProxy pool env files and the template unit ``tgpanel-mtproxy@.service``."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field

from tgpanel.domain.models import DesiredState, PoolRecord
from tgpanel.domain.pools import MAX_SECRETS_PER_PROCESS
from tgpanel.domain.secrets_ import base_secret
from tgpanel.render.errors import RenderError
from tgpanel.render.profiles import needs_sentinel, sentinel_pool

MAX_SECRETS_HARD_LIMIT = MAX_SECRETS_PER_PROCESS
_HEX32 = re.compile(r"[0-9a-f]{32}")
_SAFE_ARG = re.compile(r"[A-Za-z0-9_.:/@=+,-]+")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9_.+@/-]*")
_SAFE_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


@dataclass(frozen=True, slots=True)
class MtproxyFacts:
    binary: str
    user: str
    aes_pwd: str
    proxy_multi_conf: str
    nat_args: str = ""
    working_directory: str | None = None
    # ExecStart references $MTPROXY_NAT_ARGS but no value was found: callers should warn.
    nat_args_unresolved: bool = False
    # The effective ExecStart is a wrapper; facts come from an earlier direct ExecStart. Callers
    # should warn: the wrapper may add arguments (e.g. NAT) that we cannot see.
    exec_overridden: bool = False


# ---------------------------------------------------------------- env files


def pool_secrets(state: DesiredState, pool: PoolRecord) -> list[str]:
    """Base secrets for a pool: every user of the pool (any status), ordered by user id.

    The sentinel secret is appended to the pool that hosts it when no user is active.
    """
    try:
        secrets = [
            u.mtproxy_secret
            for u in sorted(state.users, key=lambda u: u.id)
            if u.pool_id == pool.id
        ]
    except ValueError as exc:
        raise RenderError(f"pool {pool.id}: invalid user secret format") from exc
    if needs_sentinel(state) and sentinel_pool(state).id == pool.id:
        try:
            secrets.append(base_secret(state.sentinel_secret))
        except ValueError as exc:
            raise RenderError("sentinel secret has invalid format") from exc
    return secrets


def render_pool_env(
    pool: PoolRecord,
    secrets: Iterable[str],
    workers: int,
    max_connections: int,
    nat_args: str,
) -> bytes:
    secret_list = list(secrets)
    if len(secret_list) > MAX_SECRETS_HARD_LIMIT:
        raise RenderError(
            f"pool {pool.id} has {len(secret_list)} secrets, MTProxy supports at most "
            f"{MAX_SECRETS_HARD_LIMIT}"
        )
    for secret in secret_list:
        if not _HEX32.fullmatch(secret):
            raise RenderError(f"pool {pool.id}: invalid MTProxy secret (need 32 lowercase hex)")
    if any(c in nat_args for c in "\"'$\\`\n\r%"):
        raise RenderError("NAT arguments contain forbidden characters")
    secret_args = " ".join(f"-S {s}" for s in secret_list)
    text = (
        f"MTP_PORT={pool.port}\n"
        f"MTP_STATS_PORT={pool.stats_port}\n"
        f'MTP_SECRET_ARGS="{secret_args}"\n'
        f"MTP_WORKERS={workers}\n"
        f"MTP_MAX_CONNECTIONS={max_connections}\n"
        f'MTP_NAT_ARGS="{nat_args}"\n'
    )
    return text.encode()


# ---------------------------------------------------------- fact extraction

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if buf:
            line = buf + " " + line.lstrip()
            buf = ""
        if line.endswith("\\"):
            buf = line[:-1].rstrip()
            continue
        lines.append(line)
    if buf:
        lines.append(buf)
    return lines


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_env_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _logical_lines(text):
        if line.lstrip().startswith(("#", ";")):
            continue
        m = _ENV_LINE.match(line)
        if m:
            result[m.group(1)] = _unquote(m.group(2))
    return result


def _parse_environment_directive(value: str) -> dict[str, str]:
    """Parse the value of a systemd ``Environment=`` line (space separated, quotable)."""
    result: dict[str, str] = {}
    try:
        parts = shlex.split(value)
    except ValueError:
        return result
    for part in parts:
        if "=" in part:
            key, _, val = part.partition("=")
            result[key] = val
    return result


@dataclass
class _UnitView:
    exec_start: str | None = None
    exec_history: list[str] = field(default_factory=list)
    user: str | None = None
    working_directory: str | None = None


def _scan_unit(text: str, view: _UnitView, environment: dict[str, str]) -> None:
    section = ""
    for line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "Service" or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if key == "ExecStart":
            view.exec_start = value or None
            if value:
                view.exec_history.append(value)
        elif key == "User":
            view.user = value or None
        elif key == "WorkingDirectory":
            view.working_directory = value or None
        elif key == "Environment":
            if value == "":
                environment.clear()
            else:
                environment.update(_parse_environment_directive(value))


def _expand(token: str, env: dict[str, str], what: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if name not in env:
            raise RenderError(f"{what}: variable {name} cannot be resolved from unit/env files")
        return env[name]

    return _VAR.sub(repl, token)


def extract_mtproxy_facts(
    unit_text: str,
    dropin_texts: list[str],
    env_text: str,
    environment_file_texts: list[str] | None = None,
) -> MtproxyFacts:
    """Extract the few facts we reuse from the installed ``mtproxy.service``.

    ``environment_file_texts``: contents of every existing ``EnvironmentFile=`` named in the
    unit/drop-ins (phase 2 reads and passes them); their KEY=VALUE lines are searched too,
    so ``MTPROXY_NAT_ARGS`` defined there is found. Later files override earlier ones.
    """
    view = _UnitView()
    environment: dict[str, str] = {}
    for text in [unit_text, *dropin_texts]:
        _scan_unit(text, view, environment)
    # Per systemd, EnvironmentFile= values override Environment= values.
    environment.update(_parse_env_text(env_text))
    for extra in environment_file_texts or []:
        environment.update(_parse_env_text(extra))

    if not view.exec_start:
        raise RenderError("mtproxy.service: no ExecStart= found")
    overridden = False
    tokens: list[str] = []
    binary: str | None = None
    # The effective ExecStart may be a wrapper installed by a previous tool (drop-in override).
    # Then fall back to the most recent ExecStart in the unit history that runs the binary directly.
    candidates = [view.exec_start, *reversed(view.exec_history)]
    for index, candidate in enumerate(candidates):
        try:
            parsed = shlex.split(candidate.lstrip("-@+!:"))
        except ValueError as exc:
            if index == 0:
                raise RenderError(f"mtproxy.service: cannot parse ExecStart: {exc}") from exc
            continue
        found = next((t for t in parsed if t.rsplit("/", 1)[-1] == "mtproto-proxy"), None)
        if found is not None:
            tokens, binary, overridden = parsed, found, index > 0
            view.exec_start = candidate
            break
    if binary is None:
        raise RenderError("mtproxy.service: ExecStart does not run mtproto-proxy directly")

    user = view.user
    aes_pwd: str | None = None
    multi: str | None = None
    inline_nat: list[str] = []
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok == "-u" and nxt:
            user = nxt
        elif tok == "--aes-pwd" and nxt:
            aes_pwd = nxt
            if i + 2 < len(tokens) and not tokens[i + 2].startswith("-"):
                multi = tokens[i + 2]
        elif tok == "--nat-info" and nxt:
            inline_nat += [tok, nxt]
    for tok in tokens:
        if tok.endswith("proxy-multi.conf"):
            multi = tok
    if not user:
        raise RenderError("mtproxy.service: user not found (-u option or User=)")
    if not aes_pwd:
        raise RenderError("mtproxy.service: --aes-pwd path not found in ExecStart")
    if not multi:
        raise RenderError("mtproxy.service: proxy-multi.conf path not found in ExecStart")

    aes_pwd = _expand(aes_pwd, environment, "--aes-pwd")
    multi = _expand(multi, environment, "proxy-multi.conf")
    binary = _expand(binary, environment, "binary")
    nat = environment.get("MTPROXY_NAT_ARGS")
    nat_args = nat.strip() if nat is not None else " ".join(inline_nat)
    unresolved = nat is None and not inline_nat and "MTPROXY_NAT_ARGS" in view.exec_start
    workdir = view.working_directory
    return MtproxyFacts(
        binary, user, aes_pwd, multi, nat_args, workdir, unresolved, exec_overridden=overridden
    )


# ------------------------------------------------------------------- unit


def _check_path(value: str, what: str) -> None:
    if not _SAFE_PATH.fullmatch(value):
        raise RenderError(f"unsafe {what} for unit file: {value!r}")


def render_pool_unit(facts: MtproxyFacts) -> bytes:
    _check_path(facts.binary, "binary path")
    _check_path(facts.aes_pwd, "--aes-pwd path")
    _check_path(facts.proxy_multi_conf, "proxy-multi.conf path")
    if not _SAFE_USER.fullmatch(facts.user):
        raise RenderError(f"unsafe user for unit file: {facts.user!r}")
    workdir_line = ""
    if facts.working_directory:
        _check_path(facts.working_directory, "WorkingDirectory")
        workdir_line = f"WorkingDirectory={facts.working_directory}\n"
    text = (
        "# Managed by tgpanel. Do not edit: regenerated on every apply.\n"
        "[Unit]\n"
        "Description=Telegram MTProxy pool %i (tgpanel)\n"
        # the port guard (tgpanel-firewall) must be loaded before a pool port is opened
        "After=network-online.target tgpanel-firewall.service\n"
        "Wants=network-online.target tgpanel-firewall.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{workdir_line}"
        "EnvironmentFile=/etc/tgpanel/mtproxy/%i.env\n"
        # $VAR (unbraced) is word-split by systemd; ${VAR} is not. The secret and NAT
        # argument lists must be split, the numeric values need no splitting.
        f"ExecStart={facts.binary} -u {facts.user} -p ${{MTP_STATS_PORT}} -H ${{MTP_PORT}} "
        "$MTP_SECRET_ARGS $MTP_NAT_ARGS "
        f"--aes-pwd {facts.aes_pwd} {facts.proxy_multi_conf} "
        "-M ${MTP_WORKERS} -C ${MTP_MAX_CONNECTIONS}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "NoNewPrivileges=true\n"
        "ProtectSystem=strict\n"
        "ProtectHome=true\n"
        "PrivateTmp=true\n"
        "PrivateDevices=true\n"
        "ProtectKernelTunables=true\n"
        "ProtectKernelModules=true\n"
        "ProtectControlGroups=true\n"
        "RestrictSUIDSGID=true\n"
        "LockPersonality=true\n"
        "LimitNOFILE=1048576\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return text.encode()
