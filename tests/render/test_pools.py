from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from tests.render.conftest import SENTINEL, fixture_text, make_state, make_user, secret
from tgpanel.domain.models import PoolRecord, UserStatus
from tgpanel.render.errors import RenderError
from tgpanel.render.pools import (
    MtproxyFacts,
    extract_mtproxy_facts,
    pool_secrets,
    render_pool_env,
    render_pool_unit,
)

POOL = PoolRecord(1, 2400, 8900)


def clean_facts() -> MtproxyFacts:
    return extract_mtproxy_facts(
        fixture_text("clean", "mtproxy.service"), [], fixture_text("clean", "mtproxy.env")
    )


def owner_facts() -> MtproxyFacts:
    return extract_mtproxy_facts(
        fixture_text("owner", "mtproxy.service"),
        [fixture_text("owner", "mtproxy.service.d/nat.conf")],
        fixture_text("owner", "mtproxy.env"),
    )


def test_extract_clean() -> None:
    assert clean_facts() == MtproxyFacts(
        binary="/opt/MTProxy/objs/bin/mtproto-proxy",
        user="mtproxy",
        aes_pwd="/etc/mtproxy/proxy-secret",
        proxy_multi_conf="/etc/mtproxy/proxy-multi.conf",
        nat_args="",
        working_directory="/var/lib/mtproxy",
        nat_args_unresolved=True,  # ExecStart uses $MTPROXY_NAT_ARGS, nothing defines it
    )


def test_extract_owner() -> None:
    facts = owner_facts()
    assert facts.binary == "/opt/MTProxy/objs/bin/mtproto-proxy"
    assert facts.user == "mtproxy"
    assert facts.proxy_multi_conf == "/etc/mtproxy/proxy-multi.conf"
    assert facts.nat_args == "--nat-info 10.0.0.5:203.0.113.5"
    assert facts.working_directory is None


def test_nat_from_dropin_only_and_env_override() -> None:
    unit = fixture_text("owner", "mtproxy.service")
    drop = fixture_text("owner", "mtproxy.service.d/nat.conf")
    assert extract_mtproxy_facts(unit, [drop], "").nat_args == "--nat-info 10.0.0.5:203.0.113.5"
    env = 'MTPROXY_NAT_ARGS="--nat-info 1.1.1.1:2.2.2.2"\n'
    assert extract_mtproxy_facts(unit, [drop], env).nat_args == "--nat-info 1.1.1.1:2.2.2.2"
    plain = "[Service]\nEnvironment=MTPROXY_NAT_ARGS=--nat-info\\x201.1.1.1:2.2.2.2\n"
    assert extract_mtproxy_facts(unit, [plain], "").nat_args.startswith("--nat-info")


def test_extract_user_from_user_directive() -> None:
    unit = (
        "[Service]\nUser=svc\n"
        "ExecStart=/x/mtproto-proxy -p 1 --aes-pwd /a/secret /a/proxy-multi.conf\n"
    )
    assert extract_mtproxy_facts(unit, [], "").user == "svc"


@pytest.mark.parametrize(
    "unit",
    [
        "[Service]\n",  # no ExecStart
        "[Service]\nExecStart=/usr/bin/wrapper --foo\n",  # not mtproto-proxy
        "[Service]\nExecStart=/x/mtproto-proxy -u m --aes-pwd /a/s\n",  # no proxy-multi.conf
        "[Service]\nExecStart=/x/mtproto-proxy --aes-pwd /a/s /a/proxy-multi.conf\n",  # no user
        "[Service]\nExecStart=/x/mtproto-proxy -u m /a/proxy-multi.conf\n",  # no aes-pwd
        "[Service]\nExecStart=/x/mtproto-proxy -u m --aes-pwd ${DIR}/s /a/proxy-multi.conf\n",
    ],
)
def test_extract_errors(unit: str) -> None:
    with pytest.raises(RenderError):
        extract_mtproxy_facts(unit, [], "")


def test_dropin_can_reset_execstart() -> None:
    unit = fixture_text("clean", "mtproxy.service")
    drop = (
        "[Service]\nExecStart=\n"
        "ExecStart=/opt/m/mtproto-proxy -u zz --aes-pwd /s /p/proxy-multi.conf\n"
    )
    facts = extract_mtproxy_facts(unit, [drop], "")
    assert facts.binary == "/opt/m/mtproto-proxy" and facts.user == "zz"


@pytest.mark.parametrize("variant", ["clean", "owner"])
def test_pool_unit_golden(variant: str, golden: Callable[[str, bytes], None]) -> None:
    facts = clean_facts() if variant == "clean" else owner_facts()
    out = render_pool_unit(facts)
    golden(f"tgpanel-mtproxy@_{variant}.service", out)
    text = out.decode()
    assert "EnvironmentFile=/etc/tgpanel/mtproxy/%i.env" in text
    assert "$MTP_SECRET_ARGS $MTP_NAT_ARGS" in text
    assert "-p ${MTP_STATS_PORT} -H ${MTP_PORT}" in text
    assert "-M ${MTP_WORKERS} -C ${MTP_MAX_CONNECTIONS}" in text
    assert "Restart=on-failure" in text and "NoNewPrivileges=true" in text
    assert "WantedBy=multi-user.target" in text and "After=network-online.target" in text
    assert "MTPROXY_" not in text  # legacy variables never leak into our unit


def test_pool_unit_rejects_unsafe_values() -> None:
    bad = MtproxyFacts("/x/mtproto-proxy\nExecStartPost=/bin/evil", "m", "/a", "/b")
    with pytest.raises(RenderError):
        render_pool_unit(bad)
    with pytest.raises(RenderError):
        render_pool_unit(MtproxyFacts("/x/m", "m;x", "/a", "/b"))


def test_env_golden(golden: Callable[[str, bytes], None]) -> None:
    out = render_pool_env(POOL, [secret(1), secret(2)], 1, 4096, "--nat-info 10.0.0.5:203.0.113.5")
    golden("pool1.env", out)
    assert out.decode().splitlines()[2] == f'MTP_SECRET_ARGS="-S {secret(1)} -S {secret(2)}"'


@pytest.mark.parametrize("count", [15, 16])
def test_env_full_pool(count: int) -> None:
    secrets = [secret(i) for i in range(1, count + 1)]
    out = render_pool_env(POOL, secrets, 1, 4096, "").decode()
    assert out.count("-S ") == count


def test_env_seventeen_rejected() -> None:
    with pytest.raises(RenderError):
        render_pool_env(POOL, [secret(i) for i in range(17)], 1, 4096, "")


def test_env_rejects_bad_secret_and_nat() -> None:
    with pytest.raises(RenderError):
        render_pool_env(POOL, ["dd" + secret(1)], 1, 1, "")
    with pytest.raises(RenderError):
        render_pool_env(POOL, [secret(1)], 1, 1, '"\nMTP_PORT=1')


def test_pool_secrets_base_ordered_includes_disabled() -> None:
    state = make_state(
        (
            make_user(5, sec="dd" + secret(5)),
            make_user(2, status=UserStatus.DISABLED),
            make_user(9, pool_id=2),
            make_user(1),
        )
    )
    assert pool_secrets(state, state.pools[0]) == [secret(1), secret(2), secret(5)]
    assert pool_secrets(state, state.pools[1]) == [secret(9)]


def test_pool_secrets_sentinel() -> None:
    state = make_state((make_user(1, status=UserStatus.DISABLED),))
    assert pool_secrets(state, state.pools[0]) == [secret(1), SENTINEL]
    assert pool_secrets(state, state.pools[1]) == []


def test_pool_secrets_lowercase_for_uppercase_and_sentinel() -> None:
    upper = "DD" + secret(1).upper()
    state = make_state((make_user(1, status=UserStatus.DISABLED, sec=upper),))
    state = replace(state, sentinel_secret="AB" * 16)
    assert pool_secrets(state, state.pools[0]) == [secret(1), "ab" * 16]


def test_pool_secrets_invalid_sentinel_is_render_error() -> None:
    state = replace(make_state((make_user(1, status=UserStatus.DISABLED),)), sentinel_secret="zz")
    with pytest.raises(RenderError):
        pool_secrets(state, state.pools[0])


def test_env_trailing_newline_secret_rejected_by_fullmatch() -> None:
    with pytest.raises(RenderError):
        render_pool_env(POOL, [secret(1) + "\n"], 1, 1, "")
    with pytest.raises(RenderError):
        render_pool_unit(MtproxyFacts("/x/m", "m\n", "/a", "/b"))


def test_nat_args_from_environment_file_texts() -> None:
    unit = fixture_text("owner", "mtproxy.service")
    drop = fixture_text("owner", "mtproxy.service.d/nat.conf")
    env_file = 'MTPROXY_NAT_ARGS="--nat-info 9.9.9.9:8.8.8.8"\n'
    facts = extract_mtproxy_facts(unit, [drop], "", [env_file])
    assert facts.nat_args == "--nat-info 9.9.9.9:8.8.8.8" and not facts.nat_args_unresolved


def test_nat_args_unresolved_flag() -> None:
    unit = fixture_text("clean", "mtproxy.service")  # ExecStart uses $MTPROXY_NAT_ARGS
    assert extract_mtproxy_facts(unit, [], "").nat_args_unresolved
    resolved = extract_mtproxy_facts(unit, [], "", ['MTPROXY_NAT_ARGS=""\n'])
    assert not resolved.nat_args_unresolved
    no_ref = "[Service]\nExecStart=/x/mtproto-proxy -u m --aes-pwd /a/s /a/proxy-multi.conf\n"
    assert not extract_mtproxy_facts(no_ref, [], "").nat_args_unresolved


def test_extract_facts_when_dropin_replaces_execstart_with_wrapper() -> None:
    base = Path("tests/fixtures/upstream/owner2")
    unit = (base / "mtproxy.service").read_text()
    dropin = (base / "mtproxy.service.d" / "tgproxy-panel.conf").read_text()
    facts = extract_mtproxy_facts(unit, [dropin], "MTPROXY_SECRET=" + "a" * 32 + "\n")
    assert facts.binary == "/opt/MTProxy/objs/bin/mtproto-proxy"
    assert facts.user == "mtproxy"
    assert facts.aes_pwd == "/etc/mtproxy/proxy-secret"
    assert facts.proxy_multi_conf == "/etc/mtproxy/proxy-multi.conf"
    assert facts.exec_overridden is True


def test_extract_facts_direct_execstart_is_not_flagged_overridden() -> None:
    base = Path("tests/fixtures/upstream/owner2")
    facts = extract_mtproxy_facts((base / "mtproxy.service").read_text(), [], "")
    assert facts.exec_overridden is False


def test_pool_unit_requires_the_firewall_and_is_hardened() -> None:
    text = render_pool_unit(clean_facts()).decode()
    lines = text.splitlines()
    assert "Requires=tgpanel-firewall.service" in lines
    assert any(
        ln.startswith("After=") and "tgpanel-firewall.service" in ln.split("=", 1)[1].split()
        for ln in lines
    )
    assert not any(ln.startswith("Wants=") and "tgpanel-firewall" in ln for ln in lines)
    for needed in (
        "User=mtproxy",
        "Group=mtproxy",
        "Restart=on-failure",
        "RestartSec=3s",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "ReadOnlyPaths=/etc/mtproxy /etc/tgpanel/mtproxy",
        "RestrictAddressFamilies=AF_INET AF_INET6",
        "RestrictNamespaces=true",
        "RestrictRealtime=true",
        "CapabilityBoundingSet=",
    ):
        assert needed in lines, needed
    assert "MemoryDenyWriteExecute" not in text  # unverified against mtproto-proxy
    assert " -u mtproxy " in text  # -u stays in ExecStart as upstream has it
