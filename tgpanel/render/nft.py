"""Render ``/etc/tgpanel/tgpanel.nft`` (traffic accounting + pool port guard).

nftables syntax assumptions (verify on the server, see PLAN 3.4): per-element counters in a
set (``counter`` set option) need nftables >= 0.9.5 and kernel >= 5.7.
"""

from __future__ import annotations

import ipaddress

from tgpanel.domain.models import DesiredState, UserRecord
from tgpanel.render.errors import RenderError
from tgpanel.render.profiles import active_users, foreign_loopback_ips

POOL_PORTS = "2400-2463"
STATS_PORTS = "8900-8963"
_LOOPBACK_NET = ipaddress.ip_network("127.64.0.0/16")


def _ip(user: UserRecord) -> str:
    try:
        addr = ipaddress.IPv4Address(user.loopback_ip)
    except ValueError as exc:
        raise RenderError(f"user {user.id}: invalid loopback ip") from exc
    if addr not in _LOOPBACK_NET:
        raise RenderError(f"user {user.id}: loopback ip outside {_LOOPBACK_NET}")
    return str(addr)


_HEADER = (
    "# Managed by tgpanel. Do not edit: regenerated on every apply.\n"
    "# add + delete + create makes loading idempotent (works whether or not the table exists).\n"
    "table inet tgpanel\n"
    "delete table inet tgpanel\n"
)

_GUARD = (
    "\tchain guard {\n"
    "\t\ttype filter hook input priority -10; policy accept;\n"
    f'\t\tiifname != "lo" tcp dport {{ {POOL_PORTS}, {STATS_PORTS} }} drop\n'
    "\t}\n"
)


def _elements(ips: list[str]) -> str:
    if not ips:
        return ""
    body = ", ".join(ips)
    return f"\t\telements = {{ {body} }}\n"


def render_nft(state: DesiredState) -> bytes:
    """Main variant: one set per direction, counters on set elements."""
    ips = [_ip(u) for u in active_users(state)]
    ips += [i for i in dict.fromkeys(foreign_loopback_ips(state)) if i not in ips]
    elems = _elements(ips)
    text = (
        _HEADER
        + "table inet tgpanel {\n"
        + "\tset up {\n\t\ttype ipv4_addr\n\t\tcounter\n"
        + elems
        + "\t}\n"
        + "\tset down {\n\t\ttype ipv4_addr\n\t\tcounter\n"
        + elems
        + "\t}\n"
        + "\tchain acct {\n"
        + "\t\ttype filter hook output priority 0; policy accept;\n"
        + '\t\toifname "lo" meta l4proto tcp ip daddr @up\n'
        + '\t\toifname "lo" meta l4proto tcp ip saddr @down\n'
        + "\t}\n"
        + _GUARD
        + "}\n"
    )
    return text.encode()


def render_nft_named_counters(state: DesiredState) -> bytes:
    """Fallback for old nftables: named counters ``u<id>_up`` / ``u<id>_down`` per user."""
    users = [(u, _ip(u)) for u in active_users(state)]
    lines = [_HEADER + "table inet tgpanel {\n"]
    for user, _ in users:
        lines.append(f"\tcounter u{user.id}_up {{ }}\n")
        lines.append(f"\tcounter u{user.id}_down {{ }}\n")
    lines.append("\tchain acct {\n")
    lines.append("\t\ttype filter hook output priority 0; policy accept;\n")
    for user, ip in users:
        lines.append(
            f'\t\toifname "lo" meta l4proto tcp ip daddr {ip} counter name "u{user.id}_up"\n'
        )
        lines.append(
            f'\t\toifname "lo" meta l4proto tcp ip saddr {ip} counter name "u{user.id}_down"\n'
        )
    lines.append("\t}\n")
    lines.append(_GUARD)
    lines.append("}\n")
    return "".join(lines).encode()


def render_firewall_unit() -> bytes:
    """``tgpanel-firewall.service``: reload our table whenever nftables.service (re)loads.

    Mirrors the way upstream's tproxy-firewall is bound: the distribution's nftables.service
    runs ``flush ruleset`` which would remove our table (and open the pool ports), so we are
    ordered after it, restarted with it, and wanted by it.
    """
    text = (
        "# Managed by tgpanel. Do not edit.\n"
        "[Unit]\n"
        "Description=tgpanel nftables rules (traffic accounting, pool port guard)\n"
        "After=nftables.service\n"
        "PartOf=nftables.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        "ExecStart=/usr/sbin/nft -f /etc/tgpanel/tgpanel.nft\n"
        "ExecReload=/usr/sbin/nft -f /etc/tgpanel/tgpanel.nft\n"
        "ExecStop=-/usr/sbin/nft delete table inet tgpanel\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target nftables.service\n"
    )
    return text.encode()
