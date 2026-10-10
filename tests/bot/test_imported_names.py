# ruff: noqa: RUF001
"""Telegram name -> display_name (never the technical `name`), on /start and "Моя ссылка"."""

from __future__ import annotations

from tests.bot.conftest import ADMIN, USER, Env
from tgpanel.db import repo
from tgpanel.domain.models import UserRecord, shown_name
from tgpanel.domain.queries import UserFilter, UserListQuery


async def imported(env: Env, auto: str, tg_id: int, *, display: str = "") -> int:
    user = await env.make_user(auto, tg_id=tg_id, display_name=display)

    def mark(conn: object) -> None:
        conn.execute(  # type: ignore[attr-defined]
            "UPDATE users SET imported = 1, source_profile_name = ? WHERE id = ?", (auto, user.id)
        )

    env.svc.ctx.db.call(mark)
    return user.id


async def get(env: Env, uid: int) -> UserRecord:
    user = await env.svc.users.get(uid)
    assert user
    return user


async def test_display_name_filled_once_name_untouched_without_apply(env: Env) -> None:
    uid = await imported(env, "user_93455874", USER)
    runs = len(env.svc.runs())
    await env.tg.send(USER, "/start", name="Иван Петров", username="ivan")
    user = await get(env, uid)
    assert (user.name, user.display_name) == ("user_93455874", "Иван Петров")
    assert len(env.svc.runs()) == runs  # DB-only: no apply
    extra = env.svc.ctx.db.call(repo.get_user_extra, uid)
    assert extra and extra.tg_username == "ivan"
    assert "user.update_meta" in env.svc.audit_text()


async def test_full_name_first_and_last_trimmed(env: Env) -> None:
    uid = await imported(env, "user_1", USER)
    await env.tg.send(USER, "/start", name="  Анна\n  Смирнова  ")
    assert (await get(env, uid)).display_name == "Анна Смирнова"


async def test_emoji_and_cjk_preserved_and_second_start_keeps_it(env: Env) -> None:
    uid = await imported(env, "user_2", USER)
    await env.tg.send(USER, "/start", name="Инга Базанова 🐉")
    assert (await get(env, uid)).display_name == "Инга Базанова 🐉"
    await env.tg.send(USER, "/start", name="Совсем Другое")
    await env.tg.press(USER, "my", name="Ещё Другое")
    assert (await get(env, uid)).display_name == "Инга Базанова 🐉"
    uid2 = await imported(env, "user_22", USER + 1)
    await env.tg.send(USER + 1, "/start", name="山田 太郎")
    assert (await get(env, uid2)).display_name == "山田 太郎"


async def test_admin_set_display_name_is_never_overwritten(env: Env) -> None:
    uid = await imported(env, "user_3", USER, display="Вася (админ)")
    await env.tg.send(USER, "/start", name="Telegram Name")
    user = await get(env, uid)
    assert user.display_name == "Вася (админ)" and user.name == "user_3"


async def test_not_imported_user_also_gets_display_name_but_keeps_name(env: Env) -> None:
    user = await env.make_user("user_555", tg_id=USER)
    await env.tg.send(USER, "/start", name="Real Name")
    got = await get(env, user.id)
    assert (got.name, got.display_name) == ("user_555", "Real Name")


async def test_duplicate_telegram_names_do_not_collide(env: Env) -> None:
    await env.make_user("Дубль", display_name="Дубль")
    uid = await imported(env, "user_4", USER)
    await env.tg.send(USER, "/start", name="Дубль")
    user = await get(env, uid)
    assert (user.name, user.display_name) == ("user_4", "Дубль")  # display names need not be unique


async def test_username_fallback_when_full_name_empty(env: Env) -> None:
    uid = await imported(env, "user_5", USER)
    await env.tg.send(USER, "/start", name="   ", username="nick")
    assert (await get(env, uid)).display_name == "@nick"


async def test_no_name_at_all_keeps_it_empty(env: Env) -> None:
    uid = await imported(env, "user_6", USER)
    await env.tg.send(USER, "/start", name="  ")
    user = await get(env, uid)
    assert user.display_name == "" and shown_name(user) == "user_6"


async def test_name_is_capped_at_100_chars(env: Env) -> None:
    uid = await imported(env, "user_7", USER)
    await env.tg.send(USER, "/start", name="Ж" * 300)
    assert (await get(env, uid)).display_name == "Ж" * 100


async def test_my_link_also_adopts_display_name(env: Env) -> None:
    uid = await imported(env, "user_8", USER)
    env.svc.ctx.db.call(repo.update_user, uid, bot_started=True, can_message=True)
    await env.tg.press(USER, "my", name="Через Кнопку")
    user = await get(env, uid)
    assert (user.name, user.display_name) == ("user_8", "Через Кнопку")


async def test_card_shows_display_name_escaped_and_profile(env: Env) -> None:
    user = await env.make_user("user_9", display_name="<b>X</b> & Co 🐉")
    await env.tg.press(ADMIN, f"c:{user.id}")
    card = env.session.texts(ADMIN)[-1]
    assert "&lt;b&gt;X&lt;/b&gt; &amp; Co 🐉" in card
    assert "<b>X</b> &" not in card
    assert "user_9" in card  # technical name shown as "Профиль"


async def test_admin_edits_display_name_in_card(env: Env) -> None:
    user = await env.make_user("user_10")
    runs = len(env.svc.runs())
    await env.tg.press(ADMIN, f"en:{user.id}")
    await env.tg.send(ADMIN, "Дмитрий Жабкин")
    got = await get(env, user.id)
    assert (got.name, got.display_name) == ("user_10", "Дмитрий Жабкин")
    assert len(env.svc.runs()) == runs
    await env.tg.press(ADMIN, f"en:{user.id}")
    await env.tg.send(ADMIN, "x" * 101)  # too long: error, still waiting
    assert (await get(env, user.id)).display_name == "Дмитрий Жабкин"
    await env.tg.send(ADMIN, "-")  # clears
    assert (await get(env, user.id)).display_name == ""


async def test_list_shows_display_name_and_search_finds_it(env: Env) -> None:
    user = await env.make_user("user_11", display_name="Макс 🚀")
    await env.tg.press(ADMIN, "l:0:a:n:0")
    markup = env.session.last_markup(ADMIN)
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Макс 🚀" in x and "user_11" not in x for x in labels)
    page = await env.svc.users.list(UserListQuery(filter=UserFilter(query="МАКС")))
    assert [r.user.id for r in page.rows] == [user.id]


async def test_html_in_display_name_is_escaped_in_bot_messages(env: Env) -> None:
    evil = "<b>Z</b> & <a href='x'>q</a>"
    user = await env.make_user("user_12", tg_id=USER, started=True, display_name=evil)
    await env.tg.press(ADMIN, f"k:{user.id}")  # link intro
    await env.tg.press(ADMIN, f"d:{user.id}")  # delete confirmation
    await env.tg.press(USER, "my")  # {name} placeholder of the user message
    for text in env.session.texts(ADMIN) + env.session.texts(USER):
        assert "<b>Z</b>" not in text and "<a href='x'>" not in text
    joined = "\n".join(env.session.texts(ADMIN) + env.session.texts(USER))
    assert "&lt;b&gt;Z&lt;/b&gt; &amp;" in joined
