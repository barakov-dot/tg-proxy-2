from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.render.conftest import make_state, make_user
from tgpanel.domain.models import DesiredState, UserRecord, UserStatus
from tgpanel.render.errors import RenderError
from tgpanel.render.nft import render_firewall_unit, render_nft, render_nft_named_counters


def state_with_users() -> DesiredState:
    return make_state((make_user(2), make_user(1), make_user(3, status=UserStatus.DISABLED)))


def test_nft_golden(golden: Callable[[str, bytes], None]) -> None:
    out = render_nft(state_with_users())
    golden("tgpanel.nft", out)
    text = out.decode()
    assert text.startswith("# Managed by tgpanel")
    lines = text.splitlines()
    assert lines.index("table inet tgpanel") < lines.index("delete table inet tgpanel")
    assert lines.index("delete table inet tgpanel") < lines.index("table inet tgpanel {")
    assert text.count("elements = { 127.64.0.1, 127.64.0.2 }") == 2
    assert "127.64.0.3" not in text
    assert 'iifname != "lo" tcp dport { 2400-2463, 8900-8963 } drop' in text
    assert "hook input priority -10" in text
    assert render_nft(state_with_users()) == out


def test_nft_no_users_has_no_elements() -> None:
    assert b"elements" not in render_nft(make_state(()))


def test_named_counters_golden(golden: Callable[[str, bytes], None]) -> None:
    out = render_nft_named_counters(state_with_users())
    golden("tgpanel_named.nft", out)
    text = out.decode()
    assert "counter u1_up { }" in text and "counter u2_down { }" in text
    assert 'ip daddr 127.64.0.1 counter name "u1_up"' in text
    assert 'ip saddr 127.64.0.2 counter name "u2_down"' in text
    assert "u3_up" not in text


def test_rejects_foreign_ip() -> None:
    u = make_user(1)
    bad = UserRecord(**{**{f: getattr(u, f) for f in u.__slots__}, "loopback_ip": "10.0.0.1"})
    with pytest.raises(RenderError):
        render_nft(make_state((bad,)))
    bad2 = UserRecord(**{**{f: getattr(u, f) for f in u.__slots__}, "loopback_ip": "127.64.0.1; x"})
    with pytest.raises(RenderError):
        render_nft_named_counters(make_state((bad2,)))


def test_firewall_unit(golden: Callable[[str, bytes], None]) -> None:
    out = render_firewall_unit()
    golden("tgpanel-firewall.service", out)
    text = out.decode()
    for needle in (
        "After=nftables.service",
        "PartOf=nftables.service",
        "nft -f /etc/tgpanel/tgpanel.nft",
        "WantedBy=multi-user.target nftables.service",
    ):
        assert needle in text
