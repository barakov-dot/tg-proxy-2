"""All Russian texts of the bot (PLAN 13.1). Texts with HTML markup take already-escaped values."""

from __future__ import annotations

# ------------------------------------------------------------------ buttons

BTN_MY_LINK = "Моя ссылка"
BTN_REQUEST = "Запросить доступ"
BTN_CONNECT = "Подключиться"
BTN_BACK = "Назад"
BTN_CANCEL = "Отмена"
BTN_SKIP = "Пропустить"
BTN_APPROVE = "Одобрить"
BTN_REJECT = "Отклонить"
BTN_ENABLE = "Включить"
BTN_DISABLE = "Выключить"
BTN_EXTEND = "Продлить"
BTN_LINK = "Ссылка"
BTN_QR = "QR-код"
BTN_SEND_LINK = "Отправить ссылку"
BTN_COMMENT = "Комментарий"
BTN_DELETE = "Удалить"
BTN_DELETE_CONFIRM = "Да, удалить"
BTN_TO_LIST = "К списку"
BTN_SEARCH = "Поиск"
BTN_SEARCH_RESET = "Сбросить поиск"
BTN_PREV = "← Назад"
BTN_NEXT = "Вперёд →"
BTN_SELECT = "В рассылку"
BTN_UNSELECT = "Убрать из рассылки"
BTN_START_BROADCAST = "Отправить"
BTN_FULL_REPORT = "Полный отчёт"
BTN_MENU = "Меню"

MENU_USERS = "Пользователи"
MENU_REQUESTS = "Заявки"
MENU_CREATE = "Создать пользователя"
MENU_MODE = "Режим выдачи"
MENU_APPLY = "Статус apply"
MENU_BACKUP = "Бэкап сейчас"
MENU_BROADCAST = "Рассылка"
MENU_BLACKLIST = "Чёрный список"

TERM_BUTTONS = {"1d": "1 день", "1m": "1 месяц", "1y": "1 год", "df": "По умолчанию"}
EXTEND_BUTTONS = {7: "+7 дней", 30: "+30 дней", 90: "+90 дней", 365: "+1 год"}
FILTER_NAMES = {"a": "все", "A": "активные", "d": "отключённые", "e": "истёкшие"}
SORT_NAMES = {"n": "имя", "l": "активность", "t": "трафик", "x": "срок"}

# ------------------------------------------------------------------ user part

NO_ACCESS = "Доступ ограничен."
START_NO_PROFILE = "Здравствуйте! У вас пока нет доступа к прокси."
START_NO_PROFILE_OPEN = "Здравствуйте! Нажмите кнопку, и я подготовлю ссылку для подключения."
REQUEST_PENDING = "Ваша заявка на рассмотрении. Я напишу, как только её рассмотрят."
REQUEST_SENT = "Заявка отправлена администраторам. Я напишу, как только её рассмотрят."
REQUEST_RATE_LIMITED = "Слишком много заявок. Попробуйте позже."
REQUEST_DENIED = "Не удалось принять заявку. Обратитесь к администратору."
PREPARING = "Готовим доступ, это займёт несколько секунд…"
PREPARE_FAILED = "Не удалось подготовить доступ. Администраторы уведомлены, попробуйте чуть позже."
LINK_UNAVAILABLE = "Ссылка сейчас недоступна. Попробуйте позже."
STATUS_DISABLED_USER = "Ваш доступ отключён. Обратитесь к администратору."
STATUS_EXPIRED_USER = "Срок вашего доступа истёк. Обратитесь к администратору для продления."
ACCESS_READY = "Доступ готов. Нажмите кнопку или откройте ссылку:"
REQUEST_APPROVED = "Ваша заявка одобрена. Ваша ссылка:"
REQUEST_REJECTED = "Ваша заявка отклонена."
REQUEST_REJECTED_ADMIN = "Заявка отклонена."
LINK_OF = "Ссылка пользователя {name}:"
QR_CAPTION = "QR-код ссылки"
LINK_FROM_ADMIN = "Ваша ссылка для подключения:"
UNKNOWN_ACTION = "Действие недоступно."
ERROR_GENERIC = "Произошла ошибка. Попробуйте ещё раз позже."
NOT_ADMIN = "Недостаточно прав."


def status_active(expires: str | None, *, imported: bool = False) -> str:
    until = f"до {expires}" if expires else "без ограничения срока"
    tail = "\nВаш профиль перенесён из прежнего бота." if imported else ""
    return f"Ваш доступ активен, {until}.{tail}"


# Default message templates (settings msg.* override them). Plain text, placeholders:
# {name} {link} {tg_link} {expires} {days}; the whole text is HTML-escaped when sent.
DEFAULT_LINK = "{intro}\n\n{link}\n\nЕсли кнопка не открывается: {tg_link}"
DEFAULT_APPROVED = (
    "Ваша заявка одобрена. Ваша ссылка:\n\n{link}\n\nЕсли кнопка не открывается: {tg_link}"
)
DEFAULT_REJECTED = "Ваша заявка отклонена."
DEFAULT_WELCOME = START_NO_PROFILE
DEFAULT_EXPIRING = (
    "Срок вашего доступа заканчивается {expires} (осталось дней: {days}). "
    "Для продления обратитесь к администратору."
)
DEFAULT_EXPIRED = "Срок вашего доступа истёк. Для продления обратитесь к администратору."
DEFAULT_BROADCAST = "Здравствуйте, {name}!\nВаша ссылка для подключения:\n{tg_link}"

TOO_FAST = "Слишком часто, подождите секунду."
BTN_BLACKLIST = "Чёрный список"
BLACKLIST_TEXT = "Чёрный список ({count}):\n{ids}"
BLACKLIST_EMPTY = "пусто"
BLACKLIST_ADD = "Добавить ID"
BLACKLIST_REMOVE = "Убрать ID"
BLACKLIST_ASK_ADD = "Отправьте Telegram ID, который нужно заблокировать, или /cancel."
BLACKLIST_ASK_REMOVE = "Отправьте Telegram ID, который нужно разблокировать, или /cancel."
BLACKLIST_BAD_ID = "Нужен положительный Telegram ID из цифр."
BLACKLIST_USAGE = "Использование: /ban ID или /unban ID"


# ------------------------------------------------------------------ admin part

ADMIN_MENU = "Админ-панель"
ADMIN_HINT = "\n\nКоманда /admin открывает админ-панель."
CANCELLED = "Отменено."
NOTHING_TO_CANCEL = "Нечего отменять."
USER_NOT_FOUND = "Пользователь не найден."
LIST_EMPTY = "Никого не найдено."
SEARCH_PROMPT = "Введите часть имени, комментария или Telegram ID."
COMMENT_PROMPT = "Отправьте новый комментарий одним сообщением (до 2000 символов) или /cancel."
COMMENT_SAVED = "Комментарий сохранён."
CONFIRM_DELETE = "Удалить пользователя «{name}» вместе со статистикой? Это необратимо."
DELETED = "Пользователь удалён."
OPERATION_RUNNING = "Применяю изменения…"
DONE = "Готово."
SENT_TO_USER = "Ссылка отправлена пользователю."
CANNOT_SEND = "Пользователю нельзя написать: {reason}."
REASON_NO_TG = "не указан Telegram ID"
REASON_NOT_STARTED = "бот не запущен"
REASON_BLOCKED = "бот заблокирован"
NO_REQUESTS = "Заявок нет."
REQUEST_DECIDED = "Заявка уже обработана ({status})."
REQUEST_NOT_FOUND = "Заявка не найдена."
CHOOSE_TERM = "Выберите срок для «{name}»:"
MODE_TEXT = "Режим выдачи: {mode}."
MODE_OPEN = "всем без одобрения"
MODE_APPROVAL = "по одобрению"
BACKUP_RUNNING = "Создаю бэкап…"
SELECTION_CLEARED = "Выбор очищен."
BROADCAST_ASK = (
    "Отправьте текст рассылки одним сообщением или «-» для текста по умолчанию.\n"
    "Подстановки: {name}, {link}, {tg_link}, {expires}. /cancel для отмены."
)
BROADCAST_NO_DRAFT = "Нет подготовленной рассылки."
BROADCAST_NO_ONE = "В рассылке некому получать сообщения."
BROADCAST_NO_SELECTION = "Никто не выбран."

CREATE_NAME = "Имя нового пользователя (до 100 символов) или /cancel:"
CREATE_TG_ID = "Telegram ID пользователя (число) или «Пропустить»:"
CREATE_TERM = "Срок действия:"
CREATE_COMMENT = "Комментарий или «Пропустить»:"
CREATE_BAD_TG_ID = "Нужно положительное число. Попробуйте ещё раз."
CREATE_BAD_TEXT = "Нужен обычный текст. Попробуйте ещё раз."
CREATING = "Создаю пользователя, применяю изменения…"
CREATED = "Пользователь «{name}» создан."


def mode_label(mode: str) -> str:
    return MODE_OPEN if mode == "open" else MODE_APPROVAL


def request_card(name: str, username: str | None, tg_id: int, created: str) -> str:
    uname = f"@{username}" if username else "нет username"
    return f"Заявка на доступ\nИмя: {name}\n{uname}\nTelegram ID: {tg_id}\nСоздана: {created}"


STATUS_RU = {"active": "активен", "disabled": "отключён", "expired": "срок истёк"}
REQUEST_STATUS_RU = {"approved": "одобрена", "rejected": "отклонена", "pending": "ожидает"}
