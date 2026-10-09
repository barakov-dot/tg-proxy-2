from tests.domain.helpers import secret_for
from tgpanel.domain.import_ import (
    CsvRow,
    SourceProfile,
    parse_csv_rows,
    parse_mtproxy_secrets_text,
    plan_import,
)
from tgpanel.domain.models import CarrierMode


def prof(name: str, i: int, mode: str | None = "https", dd: bool = False) -> SourceProfile:
    return SourceProfile(name, ("dd" if dd else "") + secret_for(i), mode, "127.0.0.1:2398")


def fifteen() -> list[SourceProfile]:
    return [prof(f"user_{93455870 + i}", i) for i in range(1, 16)]


def test_import_fifteen_profiles() -> None:
    profiles = fifteen()
    mt = {secret_for(i) for i in range(1, 16)}
    plan = plan_import(profiles, mtproxy_secrets=mt)
    assert not plan.blocked and plan.warnings == ()
    assert len(plan.importable) == 15
    r = plan.rows[0]
    assert r.tg_id == 93455871 and r.display_name == "user_93455871"
    assert r.comment == "import" and r.carrier_mode is CarrierMode.HTTPS
    assert r.secret == secret_for(1) and r.source_backend == "127.0.0.1:2398"


def test_dd_secret_kept_as_is_with_base() -> None:
    plan = plan_import([prof("user_12345", 1, dd=True)], mtproxy_secrets={secret_for(1)})
    row = plan.rows[0]
    assert row.secret == "dd" + secret_for(1)
    assert row.base_secret == secret_for(1)
    assert plan.warnings == ()


def test_sentinel_ignored() -> None:
    plan = plan_import([prof("_tgpanel_sentinel", 7), prof("user_12345", 1)])
    assert plan.sentinel_ignored and [r.source_name for r in plan.rows] == ["user_12345"]


def test_idempotent_known_secret_skipped() -> None:
    plan = plan_import(fifteen(), existing_secrets=[secret_for(i) for i in range(1, 16)])
    assert plan.importable == () and not plan.blocked
    assert all(r.skip_reason for r in plan.rows)
    # dd-prefixed known secret also matches by base secret
    plan = plan_import([prof("user_12345", 1, dd=True)], existing_secrets=[secret_for(1)])
    assert plan.importable == ()


def test_secret_not_in_mtproxy_warns_but_imports() -> None:
    plan = plan_import(fifteen(), mtproxy_secrets={secret_for(i) for i in range(2, 16)})
    assert len(plan.importable) == 15
    assert len(plan.warnings) == 1 and "user_93455871" in plan.warnings[0]
    assert secret_for(1) not in plan.warnings[0]


def test_unused_mtproxy_secret_reported_masked() -> None:
    mt = {secret_for(1), secret_for(50)}
    plan = plan_import([prof("user_12345", 1)], mtproxy_secrets=mt)
    assert len(plan.importable) == 1
    assert plan.unused_mtproxy_secrets == (secret_for(50)[:4] + "...",)
    assert any("unused" in w for w in plan.warnings)
    assert all(secret_for(50) not in w for w in plan.warnings)


def test_no_mtproxy_info_no_warnings() -> None:
    assert plan_import(fifteen(), mtproxy_secrets=None).warnings == ()


def test_duplicate_secrets_block() -> None:
    plan = plan_import([prof("user_11111", 1), prof("user_22222", 1, dd=True)])
    assert plan.blocked and len(plan.errors) == 2


def test_duplicate_tg_ids_block() -> None:
    plan = plan_import([prof("user_11111", 1), prof("user_11111", 2)])
    assert any("duplicate telegram id" in e for e in plan.errors)


def test_tg_id_conflict_with_existing_user() -> None:
    plan = plan_import([prof("user_11111", 1)], existing_tg_ids=[11111])
    assert plan.blocked


def test_unrecognized_id_and_custom_regex() -> None:
    plan = plan_import([prof("alpha", 1), prof("user_42", 2)])
    assert [r.tg_id for r in plan.rows] == [None, None]
    assert len(plan.warnings) == 2
    plan = plan_import([prof("tg-777", 1)], id_regex=r"^tg-(\d+)$")
    assert plan.rows[0].tg_id == 777 and plan.warnings == ()


def test_csv_overrides_and_unknown_names() -> None:
    rows, errs = parse_csv_rows("# header\nalpha;555;Alice;vip\nghost;;;\n\nbad;x;;\n;1;;\n")
    assert [r.profile_name for r in rows] == ["alpha", "ghost"]
    assert len(errs) == 2
    plan = plan_import([prof("alpha", 1)], csv_rows=rows)
    r = plan.rows[0]
    assert (r.tg_id, r.display_name, r.comment) == (555, "Alice", "import; vip")
    assert any("ghost" in w for w in plan.warnings)


def test_csv_row_without_id_keeps_regex_id() -> None:
    plan = plan_import([prof("user_12345", 1)], csv_rows=[CsvRow("user_12345", None, "Bob")])
    assert plan.rows[0].tg_id == 12345 and plan.rows[0].display_name == "Bob"


def test_invalid_secret_and_carrier_mode_are_errors() -> None:
    plan = plan_import([SourceProfile("x", "not-a-secret", "https", "127.0.0.1:2398")])
    assert plan.blocked and plan.rows == ()
    plan = plan_import([prof("user_12345", 1, mode="bogus")])
    assert plan.blocked


def test_missing_carrier_mode_is_none_and_modes_preserved() -> None:
    plan = plan_import([prof("a1", 1, None), prof("a2", 2, "websocket-lanes")])
    assert [r.carrier_mode for r in plan.rows] == [None, CarrierMode.WEBSOCKET_LANES]


def test_parse_mtproxy_secrets_text() -> None:
    text = f"{secret_for(1)}\n\n# c\n  dd{secret_for(2).upper()} \n"
    assert parse_mtproxy_secrets_text(text) == {secret_for(1), secret_for(2)}


def test_bad_regex_is_reported_not_raised() -> None:
    plan = plan_import(fifteen(), id_regex="([unclosed")
    assert plan.blocked and any("Выражение" in e for e in plan.errors)


def test_regex_without_group_and_non_digit_group() -> None:
    plan = plan_import([prof("user_123456", 1)], id_regex=r"^user_\d+$")
    assert plan.blocked and any("Выражение" in e for e in plan.errors)
    assert plan.rows[0].tg_id is None
    plan = plan_import([prof("user_abc", 1)], id_regex=r"^user_(\d+)$")
    assert not plan.blocked
    assert plan.rows[0].tg_id is None and any("not recognized" in w for w in plan.warnings)


def test_pathological_regex_length_rejected() -> None:
    plan = plan_import([prof("user_123456", 1)], id_regex="(" + "a" * 300 + ")")
    assert plan.blocked and any("длиннее" in e for e in plan.errors)


def test_import_plan_repr_hides_secrets() -> None:
    p = prof("user_93455871", 1)
    plan = plan_import([p])
    assert p.secret not in repr(p)
    assert p.secret not in repr(plan.rows[0])
