import pytest

from tgpanel.domain.addresses import (
    AddressExhaustedError,
    allocate_addresses,
    capacity,
    is_valid_loopback_ip,
)


def test_first_addresses_skip_zero() -> None:
    assert allocate_addresses([], 3) == ["127.64.0.1", "127.64.0.2", "127.64.0.3"]


def test_skips_dot_255_and_wraps_to_next_hi() -> None:
    used = [f"127.64.0.{i}" for i in range(1, 254)]
    assert allocate_addresses(used, 2) == ["127.64.0.254", "127.64.1.1"]


def test_lowest_free_first_fills_gaps() -> None:
    assert allocate_addresses(["127.64.0.1", "127.64.0.3"], 2) == ["127.64.0.2", "127.64.0.4"]


def test_unique_and_ignores_foreign_addresses() -> None:
    got = allocate_addresses(["10.0.0.1"], 600)
    assert len(set(got)) == 600
    assert all(is_valid_loopback_ip(a) for a in got)


def test_exhaustion() -> None:
    every = allocate_addresses([], capacity())
    assert len(every) == 65024
    assert every[-1] == "127.64.255.254"
    with pytest.raises(AddressExhaustedError):
        allocate_addresses(every)
    with pytest.raises(AddressExhaustedError):
        allocate_addresses(every[:-1], 2)


def test_validity() -> None:
    assert is_valid_loopback_ip("127.64.3.4")
    for bad in (
        "127.64.3.0",
        "127.64.3.255",
        "127.65.0.1",
        "10.0.0.1",
        "127.64.3",
        "x",
        "127.064.0.1",
    ):
        assert not is_valid_loopback_ip(bad)


def test_quarantined_addresses_are_skipped() -> None:
    assert allocate_addresses(["127.64.0.1"], 2, quarantined=["127.64.0.2"]) == [
        "127.64.0.3",
        "127.64.0.4",
    ]


def test_quarantine_roundtrip_and_ttl() -> None:
    from datetime import UTC, datetime, timedelta

    from tgpanel.domain.addresses import parse_quarantine, quarantine_released

    t0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    raw = quarantine_released(None, ["127.64.0.5"], t0)
    raw = quarantine_released(raw, ["127.64.0.6"], t0 + timedelta(hours=20))
    assert set(parse_quarantine(raw, t0 + timedelta(hours=23))) == {"127.64.0.5", "127.64.0.6"}
    assert set(parse_quarantine(raw, t0 + timedelta(hours=25))) == {"127.64.0.6"}
    # expired entries are pruned when the value is rewritten
    raw2 = quarantine_released(raw, ["127.64.0.7"], t0 + timedelta(hours=25))
    assert "127.64.0.5" not in raw2


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"x": "y"}', '{"127.64.0.1": "bad"}'])
def test_malformed_quarantine_is_ignored(raw: str | None) -> None:
    from datetime import UTC, datetime

    from tgpanel.domain.addresses import parse_quarantine

    assert parse_quarantine(raw, datetime(2026, 1, 1, tzinfo=UTC)) == {}
