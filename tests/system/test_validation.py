from __future__ import annotations

import io
import tarfile

import pytest

from tgpanel.system.ops import SystemOpsError
from tgpanel.system.validation import (
    build_tar_gz,
    parse_tar_gz,
    scrub,
    validate_env,
    validate_ipv4,
    validate_nft_ident,
    validate_path,
    validate_systemctl,
    validate_unit,
)


@pytest.mark.parametrize("unit", ["tproxy-server", "tgpanel-mtproxy@3.service", "a.b_c-d"])
def test_unit_ok(unit: str) -> None:
    assert validate_unit(unit) == unit


@pytest.mark.parametrize("unit", ["", "a b", "a;b", "-x", "a/b", "$(id)", "a\nb", "ü"])
def test_unit_bad(unit: str) -> None:
    with pytest.raises(SystemOpsError):
        validate_unit(unit)


def test_systemctl_whitelist() -> None:
    assert validate_systemctl("enable-now", "x.service") == ("enable", "--now", "x.service")
    assert validate_systemctl("mask-now", "x") == ("mask", "--now", "x")
    assert validate_systemctl("daemon-reload", "") == ("daemon-reload",)
    for bad in ("kill", "reboot", "--now", "edit", "restart --no-block", ""):
        with pytest.raises(SystemOpsError):
            validate_systemctl(bad, "x")


@pytest.mark.parametrize("path", ["relative", "/a/../b", "/a//b", "/a/", "/a/./b", "/a\x00b"])
def test_path_bad(path: str) -> None:
    with pytest.raises(SystemOpsError):
        validate_path(path)


def test_path_ok() -> None:
    assert validate_path("/etc/tproxy-server/config.json")
    assert validate_path("/")


def test_nft_and_ip_and_env() -> None:
    assert validate_nft_ident("tgpanel", "table")
    for bad in ("", "a b", "a;b", "1abc", "a-b", "x" * 65):
        with pytest.raises(SystemOpsError):
            validate_nft_ident(bad, "table")
    assert validate_ipv4("127.64.0.2") == "127.64.0.2"
    for bad in ("::1", "127.0.0.1; drop", "256.0.0.1", "1.2.3", "a"):
        with pytest.raises(SystemOpsError):
            validate_ipv4(bad)
    assert validate_env({"ACME_EMAIL": "a@b.c"})
    with pytest.raises(SystemOpsError):
        validate_env({"BAD-NAME": "x"})


def test_scrub_redacts_and_truncates() -> None:
    s32 = "1e3b4842bde9088cc96a5fafa7fc134e"
    out = scrub(f"bad secret {s32} and dd{s32} and DD{s32.upper()} end")
    assert s32 not in out
    assert "[redacted]" in out
    assert "dd[redacted]" not in out  # dd prefix swallowed
    assert len(scrub("x" * 10000)) == 4096
    assert scrub("short") == "short"
    # a secret straddling the truncation boundary cannot leak either
    assert s32[:20] not in scrub("y" * 4080 + s32)


def test_tar_roundtrip_and_deterministic() -> None:
    members = {"b/two.txt": b"2", "a.txt": b"1"}
    blob = build_tar_gz(members)
    assert parse_tar_gz(blob) == members
    assert build_tar_gz(members) == blob


@pytest.mark.parametrize("name", ["/abs", "../x", "a/../b", "", "a//b", "a\\b", "./x"])
def test_tar_build_rejects_bad_names(name: str) -> None:
    with pytest.raises(SystemOpsError):
        build_tar_gz({name: b"x"})


def _raw_tar(add: object) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        assert callable(add)
        add(tar)
    return buf.getvalue()


def test_tar_read_rejects_traversal_and_non_regular() -> None:
    def traversal(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("../evil")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    def absolute(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("/etc/passwd")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    def symlink(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    def directory(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("d")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)

    for fn in (traversal, absolute, symlink, directory):
        with pytest.raises(SystemOpsError):
            parse_tar_gz(_raw_tar(fn))
    with pytest.raises(SystemOpsError):
        parse_tar_gz(b"not a tar")


def test_tar_read_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import tgpanel.system.validation as v

    blob = build_tar_gz({"a": b"x" * 100})
    monkeypatch.setattr(v, "MAX_TAR_TOTAL_BYTES", 50)
    with pytest.raises(SystemOpsError):
        parse_tar_gz(blob)
