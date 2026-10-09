import pytest

from tgpanel.domain.secrets_ import (
    base_secret,
    generate_secret,
    is_valid_secret,
    parse_imported_secret,
)

HEX = "0123456789abcdef0123456789abcdef"


def test_generate_format_and_uniqueness() -> None:
    values = {generate_secret() for _ in range(50)}
    assert len(values) == 50
    assert all(len(v) == 32 for v in values)
    assert all(is_valid_secret(v) for v in values)


def test_validation() -> None:
    assert is_valid_secret(HEX)
    assert is_valid_secret("dd" + HEX)
    assert is_valid_secret(HEX.upper())
    for bad in ("", HEX[:-1], HEX + "0", "ee" + HEX, "dd" + HEX[:-1], "g" * 32, HEX + "\n"):
        assert not is_valid_secret(bad)


def test_parse_keeps_dd_and_exposes_base() -> None:
    p = parse_imported_secret(" dd" + HEX.upper() + " ")
    assert p.raw == "dd" + HEX.upper()
    assert p.base == HEX
    assert p.has_dd_prefix
    q = parse_imported_secret(HEX)
    assert q.raw == HEX and q.base == HEX and not q.has_dd_prefix


def test_parse_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        parse_imported_secret("nope")
    with pytest.raises(ValueError):
        base_secret("nope")


def test_uppercase_dd_prefix_and_repr_hidden() -> None:
    assert is_valid_secret("DD" + HEX.upper())
    assert base_secret("DD" + HEX.upper()) == HEX
    p = parse_imported_secret("DD" + HEX.upper())
    assert p.has_dd_prefix and p.base == HEX
    assert HEX not in repr(p) and HEX.upper() not in repr(p)
