"""`tgpanel doctor`: read-mostly diagnostics (PLAN 8.2 steps 2-3 and section 3).

All checks go through SystemOps / ShellTools / the database, so the whole report runs on the
fakes. Output is Russian; ``DoctorReport.exit_code`` is non-zero when any check FAILs.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TextIO

from tgpanel.apply.backup import PRE_INSTALL, scan_disk_backups
from tgpanel.apply.config import ApplyConfig
from tgpanel.db import repo
from tgpanel.render.caddy import has_panel_block
from tgpanel.render.errors import RenderError
from tgpanel.services.container import AppContext
from tgpanel.system.ops import CertInfo, SystemOps, SystemOpsError
from tgpanel.system.tools import ShellTools
from tgpanel.system.validation import scrub

UPSTREAM_README = "https://github.com/telegramdesktop/tproxy-server#readme"
ENV_FILE_NAME = "tgpanel.env"
PANEL_PORT = 8090
CERT_WARN_DAYS = 14
RELAY_BINARY = "/usr/local/bin/tproxy-server"
MTPROXY_BINARY = "/opt/MTProxy/objs/bin/mtproto-proxy"
CADDY_BINARY = "/usr/local/bin/caddy"
CADDY_DROPIN_DIR = "/etc/systemd/system/caddy.service.d"
CADDY_STORAGE_CERTS = "/var/lib/caddy/.local/share/caddy/certificates"
VERIFIED_RELAY_FILE = "/opt/tgpanel/deploy/verified-relay.sha256"
PROBE_TABLE = "tgpanel_probe"
PROBE_FILE = "/run/tgpanel-probe.nft"
MIN_DISK_WARN = 1024**3
MIN_DISK_FAIL = 200 * 1024**2

PROXY_FILES = (
    RELAY_BINARY,
    "/etc/tproxy-server/config.json",
    "/etc/tproxy-server/profiles.json",
    MTPROXY_BINARY,
    CADDY_BINARY,
    "/etc/caddy/Caddyfile",
)
# The legacy mtproxy unit is judged separately (it may be switched off after the import).
PROXY_UNITS = ("tproxy-server", "caddy")


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"


_LABEL = {
    Status.OK: "[  OK   ]",
    Status.WARN: "[ ВНИМ. ]",
    Status.FAIL: "[ОШИБКА ]",
    Status.INFO: "[ ИНФО  ]",
}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.WARN]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def render(self, out: TextIO) -> None:
        for check in self.checks:
            tail = f": {check.detail}" if check.detail else ""
            print(f"{_LABEL[check.status]} {check.name}{tail}", file=out)
        print("", file=out)
        print(
            f"Итого: ошибок {len(self.failed)}, предупреждений {len(self.warnings)}, "
            f"проверок {len(self.checks)}",
            file=out,
        )


# ------------------------------------------------------------------------ shared helpers


def parse_env_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines (systemd EnvironmentFile subset; no expansion)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


async def read_install_env(ops: SystemOps, config: ApplyConfig) -> dict[str, str]:
    """Contents of /etc/tgpanel/tgpanel.env (empty dict if unreadable). Never logged."""
    try:
        raw = await ops.read_file(f"{config.paths.tgpanel_dir}/{ENV_FILE_NAME}")
    except SystemOpsError:
        return {}
    return parse_env_text(raw.decode("utf-8", "replace"))


async def read_caddy_env(ops: SystemOps, config: ApplyConfig) -> dict[str, str]:
    """`Environment=` assignments of the caddy drop-ins (needed by `caddy validate`)."""
    env: dict[str, str] = {}
    base = f"{config.paths.systemd_dir}/caddy.service.d"
    try:
        names = sorted(n for n in await ops.list_dir(base) if n.endswith(".conf"))
    except SystemOpsError:
        return env
    for name in names:
        try:
            text = (await ops.read_file(f"{base}/{name}")).decode("utf-8", "replace")
        except SystemOpsError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("Environment="):
                continue
            try:
                tokens = shlex.split(line[len("Environment=") :])
            except ValueError:
                continue
            for token in tokens:
                key, sep, value = token.partition("=")
                if sep and key.isidentifier():
                    env[key] = value
    return env


async def load_verified_relay_hashes(ops: SystemOps, path: str = VERIFIED_RELAY_FILE) -> set[str]:
    try:
        text = (await ops.read_file(path)).decode("utf-8", "replace")
    except SystemOpsError:
        return set()
    hashes: set[str] = set()
    for raw in text.splitlines():
        token = raw.split("#", 1)[0].strip().split()
        if token and len(token[0]) == 64:
            hashes.add(token[0].lower())
    return hashes


async def relay_sha256(ops: SystemOps) -> str | None:
    try:
        data = await ops.read_file(RELAY_BINARY)
    except SystemOpsError:
        return None
    return hashlib.sha256(data).hexdigest()


def _parse_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def cert_days_left(cert: CertInfo, now: datetime) -> float | None:
    end = _parse_dt(cert.not_after)
    return None if end is None else (end - now).total_seconds() / 86400


def describe_cert(cert: CertInfo, now: datetime) -> str:
    days = cert_days_left(cert, now)
    left = "срок неизвестен" if days is None else f"осталось {days:.0f} дн."
    return f"издатель {cert.issuer or '—'}, действует до {cert.not_after or '—'} ({left})"


async def stored_cert_exists(ops: SystemOps, domain: str) -> bool | None:
    """Does Caddy's storage already hold a certificate for ``domain``? None = cannot tell."""
    try:
        issuers = await ops.list_dir(CADDY_STORAGE_CERTS)
    except SystemOpsError:
        return None
    for issuer in issuers:
        if await ops.exists(f"{CADDY_STORAGE_CERTS}/{issuer}/{domain}"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class CertWait:
    cert: CertInfo | None
    waited_s: float
    reused: bool | None  # True: existing certificate, False: issued now, None: unknown


async def wait_for_certificate(
    ops: SystemOps,
    domain: str,
    *,
    stored_before: bool | None,
    timeout_s: float = 120.0,
    interval_s: float = 5.0,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CertWait:
    """Poll https://<domain>/ (chain + name verified) until a valid certificate is served."""
    now_fn = clock or (lambda: datetime.now(UTC))
    waited = 0.0
    while True:
        cert: CertInfo | None
        try:
            cert = await ops.tls_cert_info(domain)
        except SystemOpsError:
            cert = None
        if cert is not None and cert.valid_chain:
            end = _parse_dt(cert.not_after)
            if end is None or end > now_fn():
                return CertWait(cert, waited, _reused(cert, stored_before, now_fn()))
        if waited >= timeout_s:
            return CertWait(None, waited, None)
        await sleep(interval_s)
        waited += interval_s


def _reused(cert: CertInfo, stored_before: bool | None, now: datetime) -> bool | None:
    if stored_before is not None:
        return stored_before
    start = _parse_dt(cert.not_before)  # storage unreadable: judge by the start date
    if start is None:
        return None
    return now - start > timedelta(hours=2)


# ------------------------------------------------------------------------------- checks


@dataclass
class DoctorEnv:
    ops: SystemOps
    tools: ShellTools
    config: ApplyConfig
    ctx: AppContext | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    verified_relay_file: str = VERIFIED_RELAY_FILE


async def check_proxy_installed(env: DoctorEnv, report: DoctorReport) -> bool:
    """PLAN 8.2 step 2. Returns True when everything the panel depends on is present."""
    ops, cfg = env.ops, env.config
    missing = [p for p in PROXY_FILES if not await _exists(ops, p)]
    inactive = [u for u in PROXY_UNITS if not await _active(ops, u)]
    health = await ops.http_get(f"{cfg.admin_url}/healthz", cfg.timing.http_timeout_s)
    ok = not missing and not inactive and health.status == 200
    if ok:
        report.add("WEB proxy установлен", Status.OK)
    else:
        parts = []
        if missing:
            parts.append("нет файлов: " + ", ".join(missing))
        if inactive:
            parts.append("не активны службы: " + ", ".join(inactive))
        if health.status != 200:
            parts.append("relay /healthz не отвечает")
        report.add("WEB proxy установлен", Status.FAIL, "; ".join(parts) + f" ({UPSTREAM_README})")
    return ok


async def _exists(ops: SystemOps, path: str) -> bool:
    try:
        return await ops.exists(path)
    except SystemOpsError:
        return False


async def _active(ops: SystemOps, unit: str) -> bool:
    try:
        return await ops.is_active(unit)
    except SystemOpsError:
        return False


async def check_services(env: DoctorEnv, report: DoctorReport) -> None:
    ops = env.ops
    for unit, label in (
        ("tproxy-server", "relay tproxy-server"),
        ("caddy", "Caddy"),
        ("tgpanel", "панель tgpanel"),
        ("tgpanel-firewall", "правила nft (tgpanel-firewall)"),
    ):
        if await _active(ops, unit):
            report.add(f"Служба {label}", Status.OK, "работает")
        else:
            report.add(f"Служба {label}", Status.FAIL, "не запущена (tgpanel repair / journalctl)")
    if await _active(ops, "tgpanel-mtproxy-refresh.path"):
        report.add("Служба обновления конфигурации MTProxy", Status.OK, "работает")
    else:
        report.add(
            "Служба обновления конфигурации MTProxy",
            Status.WARN,
            "tgpanel-mtproxy-refresh.path не активен (tgpanel repair)",
        )
    if await _tcp_open(ops, PANEL_PORT):
        report.add("Панель слушает 127.0.0.1:8090", Status.OK)
    else:
        report.add("Панель слушает 127.0.0.1:8090", Status.FAIL, "порт закрыт")


async def _tcp_open(ops: SystemOps, port: int) -> bool:
    try:
        return await ops.wait_tcp_open("127.0.0.1", port, 1.0)
    except SystemOpsError:
        return False


async def check_relay(env: DoctorEnv, report: DoctorReport) -> None:
    cfg = env.config
    res = await env.ops.http_get(f"{cfg.admin_url}/healthz", cfg.timing.http_timeout_s)
    if res.status == 200:
        report.add("relay /healthz", Status.OK)
    else:
        report.add("relay /healthz", Status.FAIL, "нет ответа")  # /readyz is never polled


async def check_pools(env: DoctorEnv, report: DoctorReport) -> None:
    if env.ctx is None:
        report.add("Пулы MTProxy", Status.WARN, "база данных недоступна")
        return
    ops, cfg, ctx = env.ops, env.config, env.ctx
    pools = await ctx.db.run(repo.list_pools)
    occupancy = await ctx.db.run(repo.pool_occupancy)
    if not pools:
        report.add("Пулы MTProxy", Status.INFO, "пулов нет")
    for pool in pools:
        unit = cfg.pool_unit_name(pool.id)
        name = f"Пул {pool.id} (порт {pool.port})"
        if occupancy.get(pool.id, 0) == 0 and not await _active(ops, unit):
            report.add(name, Status.INFO, "пуст и остановлен")
        elif not await _active(ops, unit):
            report.add(name, Status.FAIL, "служба не запущена")
        elif not await ops.wait_tcp_open("127.0.0.1", pool.port, 2.0):
            report.add(name, Status.FAIL, "порт не принимает соединения")
        else:
            report.add(name, Status.OK, f"секретов: {occupancy.get(pool.id, 0)}")
    known = {p.id for p in pools}
    orphans: list[str] = []
    try:
        for fname in await ops.list_dir(cfg.paths.pools_dir):
            stem, dot, suffix = fname.partition(".")
            if dot and suffix == "env" and stem.isdigit() and int(stem) not in known:
                orphans.append(stem)
    except SystemOpsError:
        pass
    if orphans:
        report.add(
            "Лишние пулы",
            Status.WARN,
            f"неизвестны базе данных: {', '.join(sorted(orphans))} (tgpanel apply --prune-orphans)",
        )
    else:
        report.add("Лишние пулы", Status.OK, "нет")


async def probe_nft_counters(ops: SystemOps) -> tuple[bool, str]:
    """Throwaway table with a counter-carrying set (PLAN 3.4): works => per-element counters."""
    text = (
        f"table inet {PROBE_TABLE}\n"
        f"delete table inet {PROBE_TABLE}\n"
        f"table inet {PROBE_TABLE} {{\n"
        "\tset s {\n\t\ttype ipv4_addr\n\t\tcounter\n"
        "\t\telements = { 127.64.255.254 }\n\t}\n}\n"
    )
    try:
        await ops.write_atomic(PROBE_FILE, text.encode(), mode=0o600, owner="root", group="root")
        await ops.nft_load_file(PROBE_FILE)
        await ops.nft_list_set(PROBE_TABLE, "s")
        return True, "счётчики на элементах наборов работают"
    except SystemOpsError as exc:
        return False, scrub(str(exc), 300)
    finally:
        try:
            await ops.nft_delete_table(PROBE_TABLE)
            await ops.remove(PROBE_FILE)
        except SystemOpsError:
            pass


async def check_nft(env: DoctorEnv, report: DoctorReport) -> None:
    ops = env.ops
    try:
        await ops.nft_list_set("tgpanel", "up")
        await ops.nft_list_set("tgpanel", "down")
        report.add("Таблица nft inet tgpanel", Status.OK)
    except SystemOpsError:
        report.add("Таблица nft inet tgpanel", Status.FAIL, "отсутствует (tgpanel repair)")
    ok, detail = await probe_nft_counters(ops)
    report.add("nftables: счётчики элементов", Status.OK if ok else Status.FAIL, detail)


async def check_caddy(env: DoctorEnv, report: DoctorReport, install_env: dict[str, str]) -> None:
    ops, cfg = env.ops, env.config
    try:
        text = (await ops.read_file(cfg.paths.caddyfile)).decode("utf-8", "replace")
    except SystemOpsError:
        report.add("Caddyfile", Status.FAIL, "не читается")
        return
    try:
        present = has_panel_block(text)
    except RenderError:
        report.add("Блок панели в Caddyfile", Status.FAIL, "маркеры повреждены (tgpanel repair)")
        return
    if present:
        report.add("Блок панели в Caddyfile", Status.OK)
    else:
        report.add("Блок панели в Caddyfile", Status.FAIL, "отсутствует (tgpanel repair)")
    caddy_env = await read_caddy_env(ops, cfg)
    result = await ops.caddy_validate(cfg.paths.caddyfile, caddy_env)
    if result.ok:
        report.add("caddy validate", Status.OK)
    else:
        report.add("caddy validate", Status.FAIL, result.output[:300])
    proxy_host = caddy_env.get("TPROXY_HOSTNAME", "")
    domain = install_env.get("TGPANEL_PANEL_DOMAIN", "")
    if domain and proxy_host and domain == proxy_host:
        report.add("Домен панели", Status.FAIL, "совпадает с именем хоста прокси")


async def check_panel_cert(
    env: DoctorEnv, report: DoctorReport, install_env: dict[str, str]
) -> None:
    domain = install_env.get("TGPANEL_PANEL_DOMAIN", "")
    if not domain:
        report.add("Сертификат панели", Status.WARN, "домен панели неизвестен")
        return
    try:
        cert = await env.ops.tls_cert_info(domain)
    except SystemOpsError:
        cert = None
    if cert is None:
        report.add("Сертификат панели", Status.FAIL, f"{domain}: сертификат не получен")
        return
    if not cert.valid_chain:
        report.add("Сертификат панели", Status.FAIL, f"{domain}: недействительная цепочка или имя")
        return
    now = env.clock()
    days = cert_days_left(cert, now)
    text = describe_cert(cert, now)
    if days is not None and days < 0:
        report.add("Сертификат панели", Status.FAIL, f"истёк; {text}")
    elif days is not None and days < CERT_WARN_DAYS:
        report.add(
            "Сертификат панели",
            Status.WARN,
            f"осталось меньше {CERT_WARN_DAYS} дней: автопродление не работает? {text}",
        )
    else:
        report.add("Сертификат панели", Status.OK, text)


async def check_dns(env: DoctorEnv, report: DoctorReport, install_env: dict[str, str]) -> None:
    domain = install_env.get("TGPANEL_PANEL_DOMAIN", "")
    if not domain:
        return
    ops = env.ops
    try:
        a_records, aaaa_records = await ops.resolve(domain)
        public = await ops.public_ipv4()
    except SystemOpsError as exc:
        report.add("DNS домена панели", Status.WARN, scrub(str(exc), 200))
        return
    problems = dns_problems(a_records, aaaa_records, public)
    if problems:
        report.add("DNS домена панели", Status.WARN, "; ".join(problems))
    else:
        report.add("DNS домена панели", Status.OK, f"A → {public}")


def dns_problems(a_records: list[str], aaaa_records: list[str], public_ip: str | None) -> list[str]:
    """Reasons why Let's Encrypt validation would fail (PLAN 3.8); empty list = fine."""
    problems: list[str] = []
    if public_ip is None:
        problems.append("не удалось определить публичный IPv4 сервера")
    elif not a_records:
        problems.append("A-запись отсутствует")
    elif public_ip not in a_records:
        problems.append(
            f"A-запись ({', '.join(a_records)}) не указывает на этот сервер ({public_ip})"
        )
    if aaaa_records:
        problems.append(
            "есть AAAA-запись (" + ", ".join(aaaa_records) + "): если она не ведёт на этот "
            "сервер, выпуск сертификата сломается — удалите её или направьте на сервер"
        )
    return problems


async def check_relay_version(env: DoctorEnv, report: DoctorReport) -> None:
    digest = await relay_sha256(env.ops)
    if digest is None:
        report.add("Версия relay", Status.WARN, "не удалось прочитать бинарник")
        return
    known = await load_verified_relay_hashes(env.ops, env.verified_relay_file)
    if not known:
        report.add("Версия relay", Status.WARN, f"список проверенных версий пуст; sha256 {digest}")
    elif digest in known:
        report.add("Версия relay", Status.OK, "входит в список проверенных")
    else:
        report.add("Версия relay", Status.WARN, f"не из списка проверенных; sha256 {digest}")


async def check_database(env: DoctorEnv, report: DoctorReport) -> None:
    if env.ctx is None:
        report.add("База данных", Status.FAIL, "не открывается")
        return
    try:
        verdict = await env.ctx.db.run(lambda c: str(c.execute("PRAGMA quick_check").fetchone()[0]))
    except Exception as exc:  # sqlite3.Error and friends: report, never crash the diagnostics
        report.add("Целостность БД", Status.FAIL, type(exc).__name__)
        return
    if verdict == "ok":
        report.add("Целостность БД", Status.OK)
    else:
        report.add("Целостность БД", Status.FAIL, scrub(verdict, 200))


async def check_drift(env: DoctorEnv, report: DoctorReport) -> None:
    if env.ctx is None:
        return
    drift = await env.ctx.pipeline.detect_drift()
    if drift is None:
        report.add("profiles.json", Status.OK, "изменений вне панели нет")
    else:
        report.add(
            "profiles.json",
            Status.WARN,
            f"изменён вне панели: {drift.description}. Остановлен ли прежний бот? "
            "Импортируйте новые профили (tgpanel import) или tgpanel apply --adopt",
        )


async def check_backups(env: DoctorEnv, report: DoctorReport) -> None:
    ops, paths = env.ops, env.config.paths
    probe = f"{paths.backups_dir}/.doctor-probe"
    try:
        await ops.write_atomic(probe, b"ok", mode=0o600, owner="root", group="root")
        await ops.remove(probe)
        report.add("Каталог резервных копий", Status.OK, "доступен для записи")
    except SystemOpsError as exc:
        report.add("Каталог резервных копий", Status.FAIL, scrub(str(exc), 200))
    found = await scan_disk_backups(ops, paths)
    if any(b.reason == PRE_INSTALL for b in found):
        report.add("Копия pre-install", Status.OK, "есть")
    else:
        report.add("Копия pre-install", Status.WARN, "не найдена: uninstall не сможет откатить")
    try:
        free = await env.tools.disk_free(paths.backups_dir)
    except SystemOpsError:
        report.add("Место на диске", Status.WARN, "не удалось определить")
        return
    mib = free // (1024 * 1024)
    if free < MIN_DISK_FAIL:
        report.add("Место на диске", Status.FAIL, f"свободно {mib} МиБ")
    elif free < MIN_DISK_WARN:
        report.add("Место на диске", Status.WARN, f"свободно {mib} МиБ")
    else:
        report.add("Место на диске", Status.OK, f"свободно {mib} МиБ")


async def check_env_file(env: DoctorEnv, report: DoctorReport) -> None:
    path = f"{env.config.paths.tgpanel_dir}/{ENV_FILE_NAME}"
    try:
        st = await env.ops.stat(path)
    except SystemOpsError:
        report.add("Файл tgpanel.env", Status.FAIL, "не найден")
        return
    if st.mode & 0o077:
        report.add("Файл tgpanel.env", Status.FAIL, f"права {st.mode:04o}, нужны 0600")
    else:
        report.add("Файл tgpanel.env", Status.OK, "права 0600")


async def check_legacy(env: DoctorEnv, report: DoctorReport) -> None:
    ops = env.ops
    unit = env.config.paths.legacy_unit.removesuffix(".service")
    if await _active(ops, unit):
        report.add(
            "Старый процесс MTProxy",
            Status.INFO,
            "работает вхолостую после импорта; отключить: tgpanel legacy-mtproxy off",
        )
        return
    try:
        state = await ops.unit_property(unit, "UnitFileState")
    except SystemOpsError:
        state = ""
    report.add("Старый процесс MTProxy", Status.INFO, f"выключен ({state or 'остановлен'})")


async def check_system(env: DoctorEnv, report: DoctorReport) -> None:
    import sys

    report.add("Python", Status.OK, f"{sys.version_info.major}.{sys.version_info.minor}")
    try:
        osr = parse_env_text(
            (await env.ops.read_file("/etc/os-release")).decode("utf-8", "replace")
        )
    except SystemOpsError:
        report.add("Операционная система", Status.WARN, "/etc/os-release не читается")
        return
    ident, version = osr.get("ID", ""), osr.get("VERSION_ID", "")
    supported = (ident == "ubuntu" and version == "24.04") or (
        ident == "debian" and version == "13"
    )
    report.add(
        "Операционная система",
        Status.OK if supported else Status.WARN,
        f"{ident} {version}" + ("" if supported else " (поддерживаются Ubuntu 24.04, Debian 13)"),
    )


async def run_doctor(env: DoctorEnv) -> DoctorReport:
    report = DoctorReport()
    install_env = await read_install_env(env.ops, env.config)
    await check_system(env, report)
    await check_proxy_installed(env, report)
    await check_relay_version(env, report)
    await check_services(env, report)
    await check_relay(env, report)
    await check_pools(env, report)
    await check_nft(env, report)
    await check_caddy(env, report, install_env)
    await check_dns(env, report, install_env)
    await check_panel_cert(env, report, install_env)
    await check_env_file(env, report)
    await check_database(env, report)
    await check_drift(env, report)
    await check_backups(env, report)
    await check_legacy(env, report)
    return report
