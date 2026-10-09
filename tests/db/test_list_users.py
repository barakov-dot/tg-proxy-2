from datetime import timedelta

import pytest

from tests.db.helpers import NOW, add_user, fresh_db
from tgpanel.db import repo
from tgpanel.domain.counters import Counter
from tgpanel.domain.models import UserStatus
from tgpanel.services.api import UserFilter, UserListQuery

D = timedelta(days=1)


def seed() -> dict[str, int]:
    """Five users with distinct values in every sortable/filterable column."""
    global CONN
    conn = fresh_db()
    CONN = conn
    ids = {
        "a": add_user(
            conn, 1, name="alpha", comment="import", tg_id=300, tg_username="zed",
            created_at=NOW - 10 * D, expires_at=NOW + 2 * D, first_seen_at=NOW - 9 * D,
            last_seen_at=NOW - 1 * D, imported=True, bot_started=True, pool_id=1,
        ),
        "b": add_user(
            conn, 2, name="Bravo", comment="vip client", tg_id=100, tg_username="Yan",
            created_at=NOW - 5 * D, expires_at=NOW + 20 * D, first_seen_at=NOW - 4 * D,
            last_seen_at=NOW - 2 * D, status=UserStatus.DISABLED, pool_id=2,
        ),
        "c": add_user(
            conn, 3, name="charlie", comment="", tg_id=None, tg_username=None,
            created_at=NOW - 2 * D, expires_at=None, pool_id=1, status=UserStatus.EXPIRED,
        ),
        "d": add_user(
            conn, 4, name="delta", comment="Тест Русский", tg_id=200, tg_username="xray",
            created_at=NOW - 1 * D, expires_at=NOW + 1 * D, first_seen_at=NOW - 6 * D,
            last_seen_at=NOW - 3 * D, bot_started=True, pool_id=2,
        ),
        "e": add_user(
            conn, 5, name="echo", comment="100% sure_x", tg_id=400, tg_username="wolf",
            created_at=NOW - 20 * D, expires_at=NOW - 1 * D, pool_id=1,
        ),
    }  # fmt: skip
    # traffic: a=1000/500 recent, b=10/10 recent, d=5000/5000 old (35d, day tier), e=0
    repo.add_traffic(
        conn, "minute", ids["a"], NOW - 3600 * timedelta(seconds=1), bytes_up=1000, bytes_down=500
    )
    repo.add_traffic(
        conn, "hour", ids["b"], (NOW - 3 * D).replace(minute=0), bytes_up=10, bytes_down=10
    )
    repo.add_traffic(
        conn, "day", ids["d"], (NOW - 35 * D).replace(hour=0), bytes_up=5000, bytes_down=5000
    )
    repo.add_traffic(conn, "minute", ids["d"], NOW - timedelta(minutes=5), bytes_up=1, bytes_down=1)
    # online: a online (two polls), b only one active, d online
    for key, last, prev in (("a", True, True), ("b", True, False), ("d", True, True)):
        repo.put_counter_state(
            conn, repo.CounterStateRow(ids[key], Counter(0, 0), Counter(0, 0), last, prev, NOW)
        )
    return ids


CONN = fresh_db()


def run(**kw: object) -> list[str]:
    q = UserListQuery(**kw)  # type: ignore[arg-type]
    rows, total = repo.list_users(CONN, q, NOW)
    assert total >= len(rows)
    return [r.user.name for r in rows]


def names(filter_: UserFilter, **kw: object) -> list[str]:
    return run(filter=filter_, **kw)


@pytest.fixture(autouse=True)
def _seed() -> dict[str, int]:
    return seed()


def test_default_listing_all_and_total() -> None:
    rows, total = repo.list_users(CONN, UserListQuery(), NOW)
    assert total == 5 and [r.user.id for r in rows] == [1, 2, 3, 4, 5]


def test_sort_id() -> None:
    assert run(sort="id", descending=True) == ["echo", "delta", "charlie", "Bravo", "alpha"]


def test_sort_name_case_insensitive() -> None:
    assert run(sort="name") == ["alpha", "Bravo", "charlie", "delta", "echo"]
    assert run(sort="name", descending=True)[0] == "echo"


def test_sort_comment() -> None:
    asc = run(sort="comment")
    assert asc[0] == "charlie"  # empty comment first
    assert asc.index("echo") < asc.index("alpha")  # '100%...' < 'import'


def test_sort_tg_id_nulls_last_both_directions() -> None:
    assert run(sort="tg_id") == ["Bravo", "delta", "alpha", "echo", "charlie"]
    assert run(sort="tg_id", descending=True) == ["echo", "alpha", "delta", "Bravo", "charlie"]


def test_sort_tg_username() -> None:
    assert run(sort="tg_username") == ["echo", "delta", "Bravo", "alpha", "charlie"]


def test_sort_status() -> None:
    order = [
        r.user.status.value for r in repo.list_users(CONN, UserListQuery(sort="status"), NOW)[0]
    ]
    assert order == sorted(order)


def test_sort_online() -> None:
    desc = run(sort="online", descending=True)
    assert set(desc[:2]) == {"alpha", "delta"}
    assert set(run(sort="online")[:3]) == {"Bravo", "charlie", "echo"}


def test_sort_created_at() -> None:
    assert run(sort="created_at") == ["echo", "alpha", "Bravo", "charlie", "delta"]
    assert run(sort="created_at", descending=True)[0] == "delta"


def test_sort_expires_at_nulls_last() -> None:
    assert run(sort="expires_at") == ["echo", "delta", "alpha", "Bravo", "charlie"]
    assert run(sort="expires_at", descending=True) == ["Bravo", "alpha", "delta", "echo", "charlie"]


def test_sort_first_seen_at_nulls_last() -> None:
    assert run(sort="first_seen_at") == ["alpha", "delta", "Bravo", "charlie", "echo"]


def test_sort_last_seen_at_nulls_last() -> None:
    assert run(sort="last_seen_at") == ["delta", "Bravo", "alpha", "charlie", "echo"]
    assert run(sort="last_seen_at", descending=True) == [
        "alpha",
        "Bravo",
        "delta",
        "charlie",
        "echo",
    ]


def test_sort_traffic_and_period() -> None:
    assert run(sort="traffic", descending=True, period="24h")[0] == "alpha"
    assert run(sort="traffic", descending=True, period="all")[0] == "delta"
    # 30d excludes d's 35-day-old day bucket; d keeps only its fresh 2 bytes
    assert run(sort="traffic", descending=True, period="30d")[:3] == ["alpha", "Bravo", "delta"]


def test_sort_pool_id() -> None:
    asc = run(sort="pool_id")
    assert set(asc[:3]) == {"alpha", "charlie", "echo"} and set(asc[3:]) == {"Bravo", "delta"}
    assert set(run(sort="pool_id", descending=True)[:2]) == {"Bravo", "delta"}


def test_sort_bot_started() -> None:
    assert set(run(sort="bot_started", descending=True)[:2]) == {"alpha", "delta"}
    assert set(run(sort="bot_started")[:3]) == {"Bravo", "charlie", "echo"}


def test_every_sort_field_from_api_is_supported() -> None:
    from typing import get_args

    from tgpanel.services.api import SortField

    assert set(get_args(SortField)) == set(repo.SORT_EXPRESSIONS)
    for field in get_args(SortField):
        for desc in (False, True):
            rows, total = repo.list_users(CONN, UserListQuery(sort=field, descending=desc), NOW)
            assert total == 5 and len(rows) == 5


def test_unknown_sort_and_period_rejected() -> None:
    for bad in ("name; DROP TABLE users", "u.id", "", "secret", "ID"):
        with pytest.raises(ValueError):
            repo.list_users(CONN, UserListQuery(sort=bad), NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        repo.list_users(CONN, UserListQuery(period="1y"), NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        repo.list_users(CONN, UserListQuery(page=0), NOW)
    assert CONN.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 5


def test_filter_query_matches_name_comment_tgid_username() -> None:
    assert names(UserFilter(query="alp")) == ["alpha"]
    assert names(UserFilter(query="BRAVO")) == ["Bravo"]
    assert names(UserFilter(query="vip")) == ["Bravo"]
    assert names(UserFilter(query="200")) == ["delta"]
    assert names(UserFilter(query="XRAY")) == ["delta"]
    assert names(UserFilter(query="тест")) == ["delta"]  # non-ASCII case-insensitive
    assert names(UserFilter(query="nothing-like-this")) == []


def test_filter_query_escapes_like_wildcards() -> None:
    assert names(UserFilter(query="%")) == ["echo"]
    assert names(UserFilter(query="_x")) == ["echo"]
    assert names(UserFilter(query="sure_")) == ["echo"]
    assert names(UserFilter(query="'; DROP TABLE users; --")) == []


def test_filter_statuses() -> None:
    assert names(UserFilter(statuses=(UserStatus.DISABLED,))) == ["Bravo"]
    assert names(UserFilter(statuses=(UserStatus.DISABLED, UserStatus.EXPIRED))) == [
        "Bravo",
        "charlie",
    ]


def test_filter_online() -> None:
    assert names(UserFilter(online=True)) == ["alpha", "delta"]
    assert names(UserFilter(online=False)) == ["Bravo", "charlie", "echo"]


def test_filter_imported() -> None:
    assert names(UserFilter(imported=True)) == ["alpha"]
    assert len(names(UserFilter(imported=False))) == 4


def test_filter_has_tg_id() -> None:
    assert names(UserFilter(has_tg_id=False)) == ["charlie"]
    assert len(names(UserFilter(has_tg_id=True))) == 4


def test_filter_bot_started() -> None:
    assert names(UserFilter(bot_started=True)) == ["alpha", "delta"]
    assert names(UserFilter(bot_started=False)) == ["Bravo", "charlie", "echo"]


def test_filter_expires_within_days() -> None:
    assert names(UserFilter(expires_within_days=2)) == ["alpha", "delta"]
    assert names(UserFilter(expires_within_days=30)) == ["alpha", "Bravo", "delta"]
    assert names(UserFilter(expires_within_days=0)) == []


def test_filter_created_range() -> None:
    assert names(UserFilter(created_from=NOW - 5 * D)) == ["Bravo", "charlie", "delta"]
    assert names(UserFilter(created_to=NOW - 5 * D)) == ["alpha", "Bravo", "echo"]
    assert names(UserFilter(created_from=NOW - 5 * D, created_to=NOW - 2 * D)) == [
        "Bravo",
        "charlie",
    ]


def test_filter_last_seen_range() -> None:
    assert names(UserFilter(last_seen_from=NOW - 2 * D)) == ["alpha", "Bravo"]
    assert names(UserFilter(last_seen_to=NOW - 2 * D)) == ["Bravo", "delta"]
    assert names(UserFilter(last_seen_from=NOW - 10 * D, last_seen_to=NOW)) == [
        "alpha",
        "Bravo",
        "delta",
    ]


def test_filter_traffic_range_uses_selected_period() -> None:
    assert names(UserFilter(traffic_min=1000), period="24h") == ["alpha"]
    assert names(UserFilter(traffic_min=1000), period="all") == ["alpha", "delta"]
    assert names(UserFilter(traffic_max=100), period="30d") == ["Bravo", "charlie", "delta", "echo"]
    assert names(UserFilter(traffic_min=10, traffic_max=100), period="30d") == ["Bravo"]
    assert names(UserFilter(traffic_max=0), period="30d") == ["charlie", "echo"]


def test_filter_comment_contains() -> None:
    assert names(UserFilter(comment_contains="import")) == ["alpha"]
    assert names(UserFilter(comment_contains="IMP")) == ["alpha"]
    assert names(UserFilter(comment_contains="русский")) == ["delta"]
    assert names(UserFilter(comment_contains="100%")) == ["echo"]


def test_filters_combine_with_and() -> None:
    f = UserFilter(statuses=(UserStatus.ACTIVE,), has_tg_id=True, online=True, imported=True)
    assert names(f) == ["alpha"]


def test_pagination_and_total() -> None:
    q = UserListQuery(per_page=50)
    rows, total = repo.list_users(CONN, q, NOW)
    assert total == 5 and len(rows) == 5
    for i in range(6, 130):
        add_user(
            CONN, i + 100, name=f"bulk{i}", secret=f"{i + 100:032x}", loopback_ip=f"127.64.1.{i}"
        )
    rows, total = repo.list_users(CONN, UserListQuery(per_page=50, page=1), NOW)
    assert total == 129 and len(rows) == 50
    rows3, _ = repo.list_users(CONN, UserListQuery(per_page=50, page=3), NOW)
    assert len(rows3) == 29
    rows4, total4 = repo.list_users(CONN, UserListQuery(per_page=50, page=4), NOW)
    assert rows4 == [] and total4 == 129
    rows_all, _ = repo.list_users(CONN, UserListQuery(per_page=200), NOW)
    assert len(rows_all) == 129


def test_row_values_online_and_traffic() -> None:
    rows, _ = repo.list_users(CONN, UserListQuery(period="24h"), NOW)
    by_name = {r.user.name: r for r in rows}
    a = by_name["alpha"]
    assert (a.online, a.bytes_up, a.bytes_down) == (True, 1000, 500)
    assert a.first_seen_at == NOW - 9 * D and a.last_seen_at == NOW - 1 * D
    assert a.extra.tg_username == "zed" and a.user.tg_id == 300
    assert by_name["Bravo"].online is False and by_name["Bravo"].bytes_up == 0
    rows_all, _ = repo.list_users(CONN, UserListQuery(period="all"), NOW)
    d = {r.user.name: r for r in rows_all}["delta"]
    assert (d.bytes_up, d.bytes_down) == (5001, 5001)


def test_stable_order_ties_broken_by_id() -> None:
    assert run(sort="pool_id") == ["alpha", "charlie", "echo", "Bravo", "delta"]


def test_online_requires_fresh_counter_state() -> None:
    seed()
    rows, _ = repo.list_users(CONN, UserListQuery(), NOW)
    assert {r.user.name for r in rows if r.online} == {"alpha", "delta"}
    later = NOW + timedelta(minutes=10)  # collector stopped 10 minutes ago
    rows, _ = repo.list_users(CONN, UserListQuery(), later)
    assert not any(r.online for r in rows)
    q = UserListQuery(filter=UserFilter(online=True))
    assert repo.list_users(CONN, q, later)[1] == 0
    q = UserListQuery(filter=UserFilter(online=False))
    assert repo.list_users(CONN, q, later)[1] == 5
    # a wide freshness window brings them back
    rows, _ = repo.list_users(CONN, UserListQuery(), later, online_freshness=timedelta(hours=1))
    assert sum(r.online for r in rows) == 2


def test_per_page_validated() -> None:
    seed()
    with pytest.raises(ValueError):
        repo.list_users(CONN, UserListQuery(per_page=7), NOW)  # type: ignore[arg-type]
    for n in (50, 100, 200):
        repo.list_users(CONN, UserListQuery(per_page=n), NOW)


def test_page_traffic_matches_with_and_without_join() -> None:
    seed()
    plain = {
        r.user.name: (r.bytes_up, r.bytes_down)
        for r in repo.list_users(CONN, UserListQuery(), NOW)[0]
    }
    sorted_ = UserListQuery(sort="traffic")
    joined = {
        r.user.name: (r.bytes_up, r.bytes_down) for r in repo.list_users(CONN, sorted_, NOW)[0]
    }
    assert plain == joined and plain["alpha"] == (1000, 500)


def test_list_users_load_300_users_14_days_of_minutes() -> None:
    import time

    conn = fresh_db()
    for i in range(1, 301):
        add_user(conn, i, name=f"load{i}", loopback_ip=f"127.64.{i // 250}.{i % 250 + 1}")
    start = NOW - 14 * D
    minutes = 14 * 24 * 60
    conn.execute(
        "WITH RECURSIVE m(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM m WHERE n < ?)"
        " INSERT INTO traffic_minute (user_id, bucket_ts, bytes_up, bytes_down)"
        " SELECT u.id, ? + m.n * 60, 1000, 2000 FROM users u, m",
        (minutes - 1, int(start.timestamp())),
    )
    assert conn.execute("SELECT COUNT(*) FROM traffic_minute").fetchone()[0] == 300 * minutes
    t0 = time.perf_counter()
    page, total = repo.list_users(conn, UserListQuery(per_page=50), NOW)
    elapsed = time.perf_counter() - t0
    assert total == 300 and len(page) == 50
    assert page[0].bytes_up == 1000 * minutes
    assert elapsed < 0.5, f"list page took {elapsed:.3f}s"
