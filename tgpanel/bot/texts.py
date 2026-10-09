"""All Russian texts of the bot (PLAN 13.1). Texts with HTML markup take already-escaped values."""

from __future__ import annotations

from tgpanel.bot import icons

# ------------------------------------------------------------------ buttons

BTN_MY_LINK = f"{icons.LINK} " + "Моя ссылка"
BTN_REQUEST = f"{icons.REQUEST} " + "Запросить доступ"
BTN_CONNECT = f"{icons.LINK} " + "Подключиться"
BTN_BACK = f"{icons.BACK} " + "Назад"
BTN_CANCEL = f"{icons.CANCEL} " + "Отмена"
BTN_SKIP = f"{icons.SKIP} " + "Пропустить"
BTN_APPROVE = f"{icons.OK} " + "Одобрить"
BTN_REJECT = f"{icons.FAIL} " + "Отклонить"
BTN_ENABLE = f"{icons.ACTIVE} " + "Включить"
BTN_DISABLE = f"{icons.DISABLED} " + "Выключить"
BTN_EXTEND = f"{icons.TERM} " + "Продлить"
BTN_LINK = f"{icons.LINK} " + "Ссылка"
BTN_QR = f"{icons.QR} " + "QR-код"
BTN_SEND_LINK = f"{icons.SEND} " + "Отправить ссылку"
BTN_COMMENT = f"{icons.COMMENT} " + "Комментарий"
BTN_DELETE = f"{icons.DELETE} " + "Удалить"
BTN_DELETE_CONFIRM = f"{icons.DELETE} " + "Да, удалить"
BTN_TO_LIST = f"{icons.USERS} " + "К списку"
BTN_SEARCH = f"{icons.SEARCH} " + "Поиск"
BTN_SEARCH_RESET = f"{icons.REFRESH} " + "Сбросить поиск"
BTN_PREV = f"{icons.BACK} Назад"
BTN_NEXT = f"Вперёд {icons.NEXT}"
BTN_SELECT = f"{icons.BROADCAST} " + "В рассылку"
BTN_UNSELECT = f"{icons.REMOVE} " + "Убрать из рассылки"
BTN_START_BROADCAST = f"{icons.BROADCAST} " + "Отправить"
BTN_FULL_REPORT = f"{icons.REPORT} " + "Полный отчёт"
BTN_MENU = f"{icons.MENU} " + "Меню"

MENU_USERS = f"{icons.USERS} " + "Пользователи"
MENU_REQUESTS = f"{icons.REQUESTS} " + "Заявки"
MENU_CREATE = f"{icons.ADD} " + "Создать пользователя"
MENU_MODE = f"{icons.MODE} " + "Режим выдачи"
MENU_APPLY = f"{icons.APPLY} " + "Статус apply"
MENU_BACKUP = f"{icons.BACKUP} " + "Бэкап сейчас"
MENU_BROADCAST = f"{icons.BROADCAST} " + "Рассылка"
MENU_BLACKLIST = f"{icons.BLACKLIST} " + "Чёрный список"

TERM_BUTTONS = {
    "1d": f"{icons.TERM} 1 день",
    "1m": f"{icons.TERM} 1 месяц",
    "1y": f"{icons.TERM} 1 год",
    "df": f"{icons.TERM} По умолчанию",
}
EXTEND_BUTTONS = {
    7: f"{icons.TERM} +7 дней",
    30: f"{icons.TERM} +30 дней",
    90: f"{icons.TERM} +90 дней",
    365: f"{icons.TERM} +1 год",
}
FILTER_NAMES = {"a": "все", "A": "активные", "d": "отключённые", "e": "истёкшие"}
SORT_NAMES = {"n": "имя", "l": "активность", "t": "трафик", "x": "срок"}

# ------------------------------------------------------------------ user part

NO_ACCESS = f"{icons.WARN} " + "Доступ ограничен."
START_NO_PROFILE = f"{icons.HELLO} " + "Здравствуйте! У вас пока нет доступа к прокси."
START_NO_PROFILE_OPEN = (
    f"{icons.HELLO} " + "Здравствуйте! Нажмите кнопку, и я подготовлю ссылку для подключения."
)
REQUEST_PENDING = (
    f"{icons.TERM} " + "Ваша заявка на рассмотрении. Я напишу, как только её рассмотрят."
)
REQUEST_SENT = (
    f"{icons.OK} " + "Заявка отправлена администраторам. Я напишу, как только её рассмотрят."
)
REQUEST_RATE_LIMITED = f"{icons.WARN} " + "Слишком много заявок. Попробуйте позже."
REQUEST_DENIED = f"{icons.WARN} " + "Не удалось принять заявку. Обратитесь к администратору."
PREPARING = f"{icons.TERM} " + "Готовим доступ, это займёт несколько секунд…"
PREPARE_FAILED = (
    f"{icons.WARN} "
    + "Не удалось подготовить доступ. Администраторы уведомлены, попробуйте чуть позже."
)
LINK_UNAVAILABLE = f"{icons.WARN} " + "Ссылка сейчас недоступна. Попробуйте позже."
STATUS_DISABLED_USER = f"{icons.DISABLED} " + "Ваш доступ отключён. Обратитесь к администратору."
STATUS_EXPIRED_USER = (
    f"{icons.EXPIRED} " + "Срок вашего доступа истёк. Обратитесь к администратору для продления."
)
ACCESS_READY = f"{icons.OK} " + "Доступ готов. Нажмите кнопку или откройте ссылку:"
REQUEST_APPROVED = f"{icons.OK} " + "Ваша заявка одобрена. Ваша ссылка:"
REQUEST_REJECTED = f"{icons.FAIL} " + "Ваша заявка отклонена."
REQUEST_REJECTED_ADMIN = f"{icons.FAIL} " + "Заявка отклонена."
LINK_OF = f"{icons.LINK} " + "Ссылка пользователя {name}:"
QR_CAPTION = f"{icons.QR} " + "QR-код ссылки"
LINK_FROM_ADMIN = f"{icons.LINK} " + "Ваша ссылка для подключения:"
UNKNOWN_ACTION = f"{icons.WARN} " + "Действие недоступно."
ERROR_GENERIC = f"{icons.WARN} " + "Произошла ошибка. Попробуйте ещё раз позже."
NOT_ADMIN = f"{icons.WARN} " + "Недостаточно прав."


def status_active(expires: str | None, *, imported: bool = False) -> str:
    until = f"до {expires}" if expires else "без ограничения срока"
    tail = "\nВаш профиль перенесён из прежнего бота." if imported else ""
    return f"{icons.ACTIVE} Ваш доступ активен, {until}.{tail}"


# Default message templates (settings msg.* override them). Plain text, placeholders:
# {name} {link} {tg_link} {expires} {days}; the whole text is HTML-escaped when sent.
DEFAULT_LINK = f"{icons.LINK} " + "{intro}\n\n{link}\n\nЕсли кнопка не открывается: {tg_link}"
DEFAULT_APPROVED = (
    f"{icons.OK} "
    + "Ваша заявка одобрена. Ваша ссылка:\n\n{link}\n\nЕсли кнопка не открывается: {tg_link}"
)
DEFAULT_REJECTED = f"{icons.FAIL} " + "Ваша заявка отклонена."
DEFAULT_WELCOME = START_NO_PROFILE
DEFAULT_EXPIRING = (
    f"{icons.TERM} "
    + "Срок вашего доступа заканчивается {expires} (осталось дней: {days}). "
    + "Для продления обратитесь к администратору."
)
DEFAULT_EXPIRED = (
    f"{icons.EXPIRED} " + "Срок вашего доступа истёк. Для продления обратитесь к администратору."
)
DEFAULT_BROADCAST = (
    f"{icons.HELLO} " + "Здравствуйте, {name}!\nВаша ссылка для подключения:\n{tg_link}"
)

TOO_FAST = f"{icons.WARN} " + "Слишком часто, подождите секунду."
BTN_BLACKLIST = f"{icons.BLACKLIST} " + "Чёрный список"
BLACKLIST_TEXT = f"{icons.BLACKLIST} " + "Чёрный список ({count}):\n{ids}"
BLACKLIST_EMPTY = "пусто"
BLACKLIST_ADD = f"{icons.ADD} " + "Добавить ID"
BLACKLIST_REMOVE = f"{icons.REMOVE} " + "Убрать ID"
BLACKLIST_ASK_ADD = (
    f"{icons.BLACKLIST} " + "Отправьте Telegram ID, который нужно заблокировать, или /cancel."
)
BLACKLIST_ASK_REMOVE = (
    f"{icons.BLACKLIST} " + "Отправьте Telegram ID, который нужно разблокировать, или /cancel."
)
BLACKLIST_BAD_ID = f"{icons.WARN} " + "Нужен положительный Telegram ID из цифр."
BLACKLIST_USAGE = f"{icons.WARN} " + "Использование: /ban ID или /unban ID"


# ------------------------------------------------------------------ admin part

ADMIN_MENU = f"{icons.ADMIN} " + "Админ-панель"
ADMIN_HINT = "\n\nКоманда /admin открывает админ-панель."
CANCELLED = f"{icons.CANCEL} " + "Отменено."
NOTHING_TO_CANCEL = f"{icons.WARN} " + "Нечего отменять."
USER_NOT_FOUND = f"{icons.WARN} " + "Пользователь не найден."
LIST_EMPTY = f"{icons.WARN} " + "Никого не найдено."
SEARCH_PROMPT = f"{icons.SEARCH} " + "Введите часть имени, комментария или Telegram ID."
COMMENT_PROMPT = (
    f"{icons.COMMENT} "
    + "Отправьте новый комментарий одним сообщением (до 2000 символов) или /cancel."
)
COMMENT_SAVED = f"{icons.OK} " + "Комментарий сохранён."
CONFIRM_DELETE = (
    f"{icons.WARN} " + "Удалить пользователя «{name}» вместе со статистикой? Это необратимо."
)
DELETED = f"{icons.OK} " + "Пользователь удалён."
OPERATION_RUNNING = f"{icons.TERM} " + "Применяю изменения…"
DONE = f"{icons.OK} " + "Готово."
SENT_TO_USER = f"{icons.OK} " + "Ссылка отправлена пользователю."
CANNOT_SEND = f"{icons.WARN} " + "Пользователю нельзя написать: {reason}."
REASON_NO_TG = "не указан Telegram ID"
REASON_NOT_STARTED = "бот не запущен"
REASON_BLOCKED = "бот заблокирован"
NO_REQUESTS = f"{icons.REQUESTS} " + "Заявок нет."
REQUEST_DECIDED = f"{icons.WARN} " + "Заявка уже обработана ({status})."
REQUEST_NOT_FOUND = f"{icons.WARN} " + "Заявка не найдена."
CHOOSE_TERM = f"{icons.TERM} " + "Выберите срок для «{name}»:"
MODE_TEXT = f"{icons.MODE} " + "Режим выдачи: {mode}."
MODE_OPEN = "всем без одобрения"
MODE_APPROVAL = "по одобрению"
BACKUP_RUNNING = f"{icons.BACKUP} " + "Создаю бэкап…"
SELECTION_CLEARED = f"{icons.CLEAR} " + "Выбор очищен."
BROADCAST_ASK = (
    f"{icons.BROADCAST} Отправьте текст рассылки одним сообщением "
    "или «-» для текста по умолчанию.\n"
    "Подстановки: {name}, {link}, {tg_link}, {expires}. /cancel для отмены."
)
BROADCAST_NO_DRAFT = f"{icons.WARN} " + "Нет подготовленной рассылки."
BROADCAST_NO_ONE = f"{icons.WARN} " + "В рассылке некому получать сообщения."
BROADCAST_NO_SELECTION = f"{icons.WARN} " + "Никто не выбран."

CREATE_NAME = f"{icons.USER} " + "Имя нового пользователя (до 100 символов) или /cancel:"
CREATE_TG_ID = f"{icons.TELEGRAM} " + "Telegram ID пользователя (число) или «Пропустить»:"
CREATE_TERM = f"{icons.TERM} " + "Срок действия:"
CREATE_COMMENT = f"{icons.COMMENT} " + "Комментарий или «Пропустить»:"
CREATE_BAD_TG_ID = f"{icons.WARN} " + "Нужно положительное число. Попробуйте ещё раз."
CREATE_BAD_TEXT = f"{icons.WARN} " + "Нужен обычный текст. Попробуйте ещё раз."
CREATING = f"{icons.TERM} " + "Создаю пользователя, применяю изменения…"
CREATED = f"{icons.OK} " + "Пользователь «{name}» создан."


def mode_label(mode: str) -> str:
    return MODE_OPEN if mode == "open" else MODE_APPROVAL


def request_card(name: str, username: str | None, tg_id: int, created: str) -> str:
    uname = f"@{username}" if username else "нет username"
    return (
        f"{icons.REQUEST} Заявка на доступ\n{icons.USER} Имя: {name}\n{uname}\n"
        f"{icons.TELEGRAM} Telegram ID: {tg_id}\n{icons.DATE} Создана: {created}"
    )


STATUS_RU = {"active": "активен", "disabled": "отключён", "expired": "срок истёк"}
REQUEST_STATUS_RU = {"approved": "одобрена", "rejected": "отклонена", "pending": "ожидает"}
