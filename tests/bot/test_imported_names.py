from __future__ import annotations

from tests.bot.conftest import USER, Env
from tgpanel.db import repo


async def imported(env: Env, auto: str, tg_id: int, *, renamed: str | None = None) -> int:
    user = await env.make_user(auto, tg_id=tg_id)

    def mark(conn: object) -> None:
        conn.execute(  # type: ignore[attr-defined]
            "UPDATE users SET imported = 1, source_profile_name = ? WHERE id = ?", (auto, user.id)
        )
        if renamed:
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (renamed, user.id))  # type: ignore[attr-defined]

    env.svc.ctx.db.call(mark)
    return user.id


async def name_of(env: Env, uid: int) -> str:
    user = await env.svc.users.get(uid)
    assert user
    return user.name


async def test_auto_name_replaced_once_without_apply(env: Env) -> None:
    uid = await imported(env, "user_93455874", USER)
    runs = len(env.svc.runs())
    await env.tg.send(USER, "/start", name="Иван Петров", username="ivan")
    assert await name_of(env, uid) == "Иван Петров"
    assert len(env.svc.runs()) == runs  # DB-only: no apply
    extra = env.svc.ctx.db.call(repo.get_user_extra, uid)
    assert extra and extra.tg_username == "ivan"
    assert "user.update_meta" in env.svc.audit_text()


async def test_full_name_first_and_last_trimmed(env: Env) -> None:
    uid = await imported(env, "user_1", USER)
    await env.tg.send(USER, "/start", name="  Анна\n  Смирнова  ")
    assert await name_of(env, uid) == "Анна Смирнова"


async def test_emoji_and_cyrillic_preserved_and_second_start_keeps_it(env: Env) -> None:
    uid = await imported(env, "user_2", USER)
    await env.tg.send(USER, "/start", name="Саша 🚀")
    assert await name_of(env, uid) == "Саша 🚀"
    await env.tg.send(USER, "/start", name="Совсем Другое")
    await env.tg.press(USER, "my", name="Ещё Другое")
    assert await name_of(env, uid) == "Саша 🚀"


async def test_admin_renamed_user_keeps_name(env: Env) -> None:
    uid = await imported(env, "user_3", USER, renamed="Вася (админ)")
    await env.tg.send(USER, "/start", name="Telegram Name")
    assert await name_of(env, uid) == "Вася (админ)"


async def test_not_imported_user_is_never_renamed(env: Env) -> None:
    user = await env.make_user("user_555", tg_id=USER)
    await env.tg.send(USER, "/start", name="Real Name")
    assert await name_of(env, user.id) == "user_555"


async def test_collision_gets_tg_id_suffix(env: Env) -> None:
    await env.make_user("Дубль")
    uid = await imported(env, "user_4", USER)
    await env.tg.send(USER, "/start", name="Дубль")
    assert await name_of(env, uid) == f"Дубль ({USER})"


async def test_username_fallback_when_full_name_empty(env: Env) -> None:
    uid = await imported(env, "user_5", USER)
    await env.tg.send(USER, "/start", name="   ", username="nick")
    assert await name_of(env, uid) == "@nick"


async def test_no_name_at_all_keeps_auto_name(env: Env) -> None:
    uid = await imported(env, "user_6", USER)
    await env.tg.send(USER, "/start", name="  ")
    assert await name_of(env, uid) == "user_6"


async def test_name_is_capped_at_100_chars(env: Env) -> None:
    uid = await imported(env, "user_7", USER)
    await env.tg.send(USER, "/start", name="Ж" * 300)
    assert await name_of(env, uid) == "Ж" * 100


async def test_my_link_also_adopts_name(env: Env) -> None:
    uid = await imported(env, "user_8", USER)
    env.svc.ctx.db.call(repo.update_user, uid, bot_started=True, can_message=True)
    await env.tg.press(USER, "my", name="Через Кнопку")
    assert await name_of(env, uid) == "Через Кнопку"
