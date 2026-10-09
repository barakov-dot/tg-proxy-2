"""Paths and timing of the apply pipeline (everything overridable for tests)."""

from __future__ import annotations

from dataclasses import dataclass, field

PROFILES_NAME = "profiles.json"
CONFIG_NAME = "config.json"


@dataclass(frozen=True, slots=True)
class ApplyPaths:
    lock: str = "/run/tgpanel/apply.lock"
    backups_dir: str = "/var/backups/tgpanel"
    tproxy_dir: str = "/etc/tproxy-server"
    mtproxy_env: str = "/etc/mtproxy/mtproxy.env"
    mtproxy_secrets: str = "/etc/mtproxy/mtproxy.secrets"
    tgpanel_dir: str = "/etc/tgpanel"
    caddyfile: str = "/etc/caddy/Caddyfile"
    systemd_dir: str = "/etc/systemd/system"
    legacy_unit: str = "mtproxy.service"

    @property
    def profiles(self) -> str:
        return f"{self.tproxy_dir}/{PROFILES_NAME}"

    @property
    def config(self) -> str:
        return f"{self.tproxy_dir}/{CONFIG_NAME}"

    @property
    def pools_dir(self) -> str:
        return f"{self.tgpanel_dir}/mtproxy"

    @property
    def nft_file(self) -> str:
        return f"{self.tgpanel_dir}/tgpanel.nft"

    @property
    def pool_unit(self) -> str:
        return f"{self.systemd_dir}/tgpanel-mtproxy@.service"

    def pool_env(self, pool_id: int) -> str:
        return f"{self.pools_dir}/{pool_id}.env"

    @property
    def check_profiles(self) -> str:
        return f"{self.tproxy_dir}/.tgpanel-check-{PROFILES_NAME}"

    @property
    def check_config(self) -> str:
        return f"{self.tproxy_dir}/.tgpanel-check-{CONFIG_NAME}"


@dataclass(frozen=True, slots=True)
class ApplyTiming:
    lock_timeout_s: float = 120.0
    port_timeout_s: float = 15.0
    http_timeout_s: float = 5.0
    healthz_attempts: int = 40
    healthz_interval_s: float = 0.5
    readyz_attempts: int = 3  # never a polling loop: a few spaced retries within ~20 s
    readyz_interval_s: float = 3.0


@dataclass(frozen=True, slots=True)
class ApplyConfig:
    paths: ApplyPaths = field(default_factory=ApplyPaths)
    timing: ApplyTiming = field(default_factory=ApplyTiming)
    admin_url: str = "http://127.0.0.1:8081"
    relay_unit: str = "tproxy-server"
    legacy_port: int = 2398
    tproxy_group: str = "tproxy"

    @property
    def pool_unit_prefix(self) -> str:
        return "tgpanel-mtproxy@"

    def pool_unit_name(self, pool_id: int) -> str:
        return f"tgpanel-mtproxy@{pool_id}"
