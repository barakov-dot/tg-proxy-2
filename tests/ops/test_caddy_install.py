from __future__ import annotations

from tests.ops.conftest import CADDYFILE, DOMAIN, PANEL_PATH, Ops, good_cert
from tgpanel.render.caddy import has_panel_block

ARGS = ("caddy-install", f"--domain={DOMAIN}", f"--path={PANEL_PATH}")
STORAGE_DIR = f"/var/lib/caddy/.local/share/caddy/certificates/acme-v02/{DOMAIN}"


def test_inserts_block_validates_and_restarts(ops: Ops) -> None:
    original = ops.fake.get_text(CADDYFILE)
    code, out = ops(*ARGS)
    assert code == 0
    text = ops.fake.get_text(CADDYFILE)
    assert has_panel_block(text) and text.startswith(original.rstrip("\n"))
    assert ops.fake.restart_count("caddy") == 1
    assert ops.fake.last_caddy_env is not None
    assert ops.fake.last_caddy_env["TPROXY_HOSTNAME"] == "proxy.example.com"
    assert "Блок панели добавлен" in out


def test_second_run_is_idempotent_no_restart(ops: Ops) -> None:
    ops(*ARGS)
    code, out = ops(*ARGS)
    assert code == 0
    assert ops.fake.restart_count("caddy") == 1
    assert "уже на месте" in out


def test_existing_valid_certificate_is_reused(ops: Ops) -> None:
    ops.fake.put_file(f"{STORAGE_DIR}/{DOMAIN}.crt", b"x")
    code, out = ops(*ARGS)
    assert code == 0
    assert "использован существующий сертификат" in out
    assert "Let's Encrypt" in out


def test_certificate_issued_after_wait(ops: Ops) -> None:
    ops.fake.queue_cert(DOMAIN, [None, None, good_cert(issued_days_ago=0)])
    code, out = ops(*ARGS)
    assert code == 0
    assert "выпущен только что" in out
    assert ops.fake.call_count("tls_cert_info") == 3


def test_certificate_not_obtained(ops: Ops) -> None:
    ops.fake.set_cert(DOMAIN, None)
    ops.tools.journal["caddy"] = "obtaining certificate: port 80 unreachable"
    code, out = ops(*ARGS, "--wait-cert", "20")
    assert code == 3
    assert "port 80 unreachable" in out
    assert "продолжит попытки сам" in out
    assert has_panel_block(ops.fake.get_text(CADDYFILE))  # block stays, Caddy keeps trying


def test_validate_failure_leaves_caddyfile_untouched(ops: Ops) -> None:
    original = ops.fake.get_text(CADDYFILE)
    ops.fake.fail_check("caddy_validate", "bad config")
    code, out = ops(*ARGS)
    assert code == 1
    assert ops.fake.get_text(CADDYFILE) == original
    assert ops.fake.restart_count("caddy") == 0
    assert "bad config" in out


def test_restart_failure_rolls_back(ops: Ops) -> None:
    original = ops.fake.get_text(CADDYFILE)
    ops.fake.fail_on("systemctl", "restart caddy", times=1)
    code, out = ops(*ARGS)
    assert code == 1
    assert ops.fake.get_text(CADDYFILE) == original
    assert "Caddyfile возвращён" in out
    assert ops.fake.restart_count("caddy") == 1  # the rollback restart


def test_path_with_leading_dash_or_underscore(ops: Ops) -> None:
    """A random path may start with - or _; --path=VALUE must not break argparse."""
    for path in ("-" + "a" * 30, "_" + "b" * 30):
        code, _ = ops("caddy-install", f"--domain={DOMAIN}", f"--path={path}")
        assert code == 0
        assert f"/{path}/*" in ops.fake.get_text(CADDYFILE)


def test_certificate_wait_uses_monotonic_deadline(ops: Ops) -> None:
    ops.fake.set_cert(DOMAIN, None)
    code, _ = ops(*ARGS, "--wait-cert", "20")
    assert code == 3
    assert 20 <= ops.time.t <= 25
