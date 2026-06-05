# main.py
import asyncio
import html
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl import functions


# ==========================
# ВПИШИ СВОИ ДАННЫЕ ЗДЕСЬ
# ==========================

API_ID = 26259835
API_HASH = "3fa32264398920f001dd2428b42060f6"
# PHONE_NUMBER больше не нужен: номер вводит админ через чат с ботом.

BOT_TOKEN = "8740807130:AAEXt1_6ynUsMkJZWqH112iV07g6agTMbMA"
ADMIN_ID = 8002472821

# ==========================
# НАСТРОЙКИ
# ==========================

SESSION_NAME = "telethon_market_userbot"

GIFTS_PER_PAGE = 8
SEARCH_RESULT_LIMIT = 5
REQUEST_PAGE_LIMIT = 50
MAX_MARKET_PAGES = 30

OWNERS_BLACKLIST_FILE = "owners_blacklist.json"
SEEN_GIFTS_FILE = "seen_gifts.json"

# ==========================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("nft-gift-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

BASE_GIFTS: List["BaseGift"] = []
BASE_GIFTS_BY_ID: Dict[int, "BaseGift"] = {}

USER_SELECTED_GIFT: Dict[int, int] = {}
LAST_RESULTS_BY_USER: Dict[int, List["MarketGift"]] = {}
LAST_SEARCH_BY_USER: Dict[int, Dict[str, int]] = {}

OWNERS_BLACKLIST: Dict[str, str] = {}

# key: "gift_id:min:max", value: список slug, которые уже показывали
SEEN_GIFTS_BY_QUERY: Dict[str, List[str]] = {}

# Состояние авторизации Telethon через чат с админом.
# step: phone -> code -> password
AUTH_STATES_BY_USER: Dict[int, Dict[str, Any]] = {}


@dataclass
class BaseGift:
    gift_id: int
    title: str
    stars: Optional[int]
    availability_resale: Optional[int]
    resell_min_stars: Optional[int]
    sold_out: Optional[bool]


@dataclass
class OwnerInfo:
    key: Optional[str]
    label: str
    username: Optional[str]
    link: Optional[str]

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.label


@dataclass
class MarketGift:
    title: str
    num: Optional[int]
    slug: str
    price: int
    owner: OwnerInfo

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}"


# ==========================
# ОБЩИЕ УТИЛИТЫ
# ==========================

def is_admin_user(user_id: Optional[int]) -> bool:
    return user_id == ADMIN_ID




# ==========================
# АВТОРИЗАЦИЯ TELETHON ЧЕРЕЗ ЧАТ
# ==========================

def normalize_login_code(text: str) -> str:
    """
    Админ может отправить код как обычно или через #:
    1#2#3#4#5 -> 12345
    """
    return "".join(ch for ch in text if ch.isdigit())


def session_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Добавить / обновить сессию",
                    callback_data="auth_start",
                )
            ]
        ]
    )


def auth_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="auth_cancel",
                )
            ]
        ]
    )


async def ensure_user_client_connected():
    if not user_client.is_connected():
        await user_client.connect()


async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    return bool(await user_client.is_user_authorized())


async def send_session_required_message(message_or_callback_message):
    await message_or_callback_message.answer(
        "Сессия Telegram ещё не добавлена.\n\n"
        "Нажми кнопку ниже, введи номер телефона, потом код из Telegram. "
        "Код можно отправить через #, например:\n"
        "<code>1#2#3#4#5</code>",
        reply_markup=session_required_keyboard(),
        parse_mode="HTML",
    )


async def ensure_session_for_message(message: Message) -> bool:
    if await is_user_client_authorized():
        return True

    await send_session_required_message(message)
    return False


async def ensure_session_for_callback(callback: CallbackQuery) -> bool:
    if await is_user_client_authorized():
        return True

    await callback.answer("Сначала добавь сессию", show_alert=True)
    await callback.message.answer(
        "Сессия Telegram ещё не добавлена.\n\n"
        "Добавь её через чат с ботом, чтобы парсер мог работать.",
        reply_markup=session_required_keyboard(),
    )
    return False


async def finish_successful_auth(message: Message):
    user_id = message.from_user.id
    AUTH_STATES_BY_USER.pop(user_id, None)

    me = await user_client.get_me()
    shown_name = html.escape(
        str(
            getattr(me, "first_name", None)
            or getattr(me, "username", None)
            or getattr(me, "id", "аккаунт")
        )
    )

    await message.answer(
        f"✅ Сессия добавлена. Telethon вошёл как: <b>{shown_name}</b>\n\n"
        f"Загружаю модели подарков...",
        parse_mode="HTML",
    )

    global BASE_GIFTS, BASE_GIFTS_BY_ID

    try:
        BASE_GIFTS = await load_base_gifts()
        BASE_GIFTS_BY_ID = {gift.gift_id: gift for gift in BASE_GIFTS}

        await message.answer(
            f"Готово. Загружено моделей с ресейлом: <b>{len(BASE_GIFTS)}</b>",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("Could not load models after auth")
        await message.answer(
            "Сессия добавлена, но модели пока не загрузились.\n\n"
            f"Ошибка:\n<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>\n\n"
            "Попробуй нажать /reload.",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )


async def handle_auth_message(message: Message):
    user_id = message.from_user.id
    state = AUTH_STATES_BY_USER.get(user_id)

    if not state:
        return

    text = (message.text or "").strip()
    step = state.get("step")

    if step == "phone":
        phone = text.replace(" ", "")

        if not phone.startswith("+") or len(phone) < 8:
            await message.answer(
                "Отправь номер телефона в международном формате.\n\n"
                "Пример:\n<code>+79991234567</code>",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return

        try:
            await ensure_user_client_connected()
            await user_client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await message.answer(
                "Telegram не принял этот номер. Проверь формат и отправь ещё раз.",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except FloodWaitError as e:
            await message.answer(
                f"Telegram просит подождать <b>{int(e.seconds)}</b> сек. перед новой попыткой.",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except Exception as e:
            log.exception("send_code_request error")
            await message.answer(
                "Не смог отправить код.\n\n"
                f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return

        AUTH_STATES_BY_USER[user_id] = {
            "step": "code",
            "phone": phone,
        }

        await message.answer(
            "Код отправлен в Telegram.\n\n"
            "Отправь его одним сообщением. Можно через #, например:\n"
            "<code>1#2#3#4#5</code>",
            parse_mode="HTML",
            reply_markup=auth_cancel_keyboard(),
        )
        return

    if step == "code":
        phone = state.get("phone")
        code = normalize_login_code(text)

        if len(code) < 4:
            await message.answer(
                "Код слишком короткий. Отправь код из Telegram, например:\n"
                "<code>1#2#3#4#5</code>",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return

        try:
            await ensure_user_client_connected()
            await user_client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            AUTH_STATES_BY_USER[user_id] = {
                "step": "password",
                "phone": phone,
            }
            await message.answer(
                "На аккаунте включена облачная 2FA.\n\n"
                "Отправь пароль от двухэтапной проверки одним сообщением.",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except PhoneCodeInvalidError:
            await message.answer(
                "Код неверный. Отправь код ещё раз.\n\n"
                "Формат через # тоже подходит: <code>1#2#3#4#5</code>",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except PhoneCodeExpiredError:
            AUTH_STATES_BY_USER[user_id] = {"step": "phone"}
            await message.answer(
                "Код истёк. Отправь номер телефона ещё раз, я запрошу новый код.",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except FloodWaitError as e:
            await message.answer(
                f"Telegram просит подождать <b>{int(e.seconds)}</b> сек. перед новой попыткой.",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except Exception as e:
            log.exception("sign_in by code error")
            await message.answer(
                "Не смог войти по коду.\n\n"
                f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return

        await finish_successful_auth(message)
        return

    if step == "password":
        try:
            await ensure_user_client_connected()
            await user_client.sign_in(password=text)
        except PasswordHashInvalidError:
            await message.answer(
                "Пароль 2FA неверный. Отправь пароль ещё раз.",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except FloodWaitError as e:
            await message.answer(
                f"Telegram просит подождать <b>{int(e.seconds)}</b> сек. перед новой попыткой.",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except Exception as e:
            log.exception("sign_in by password error")
            await message.answer(
                "Не смог войти по 2FA-паролю.\n\n"
                f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return

        await finish_successful_auth(message)
        return

    AUTH_STATES_BY_USER.pop(user_id, None)
    await message.answer("Авторизация сброшена. Нажми /start и попробуй снова.")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(name, default)

    return getattr(obj, name, default)


def extract_stars_amount(value: Any) -> int:
    """
    Достаёт цену в звёздах из resell_amount.
    Возможные варианты:
    - StarsAmount(amount=...)
    - список StarsAmount
    - int
    """

    if value is None:
        return 0

    if isinstance(value, int):
        return value

    if isinstance(value, list) or isinstance(value, tuple):
        for item in value:
            amount = extract_stars_amount(item)
            if amount:
                return amount
        return 0

    amount = get_field(value, "amount", None)
    if amount is not None:
        return safe_int(amount)

    stars = get_field(value, "stars", None)
    if stars is not None:
        return safe_int(stars)

    value_field = get_field(value, "value", None)
    if value_field is not None:
        return safe_int(value_field)

    return 0


# ==========================
# BLACKLIST ВЛАДЕЛЬЦЕВ
# ==========================

def load_owners_blacklist() -> Dict[str, str]:
    try:
        with open(OWNERS_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}

    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("Could not load owners blacklist")

    return {}


def save_owners_blacklist():
    try:
        with open(OWNERS_BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(OWNERS_BLACKLIST, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Could not save owners blacklist")


def is_owner_blacklisted(owner_key: Optional[str]) -> bool:
    if not owner_key:
        return False

    return owner_key in OWNERS_BLACKLIST


# ==========================
# ИСТОРИЯ ПОКАЗАННЫХ ПОДАРКОВ
# ==========================

def load_seen_gifts() -> Dict[str, List[str]]:
    try:
        with open(SEEN_GIFTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            fixed = {}
            for key, value in data.items():
                if isinstance(value, list):
                    fixed[str(key)] = [str(x) for x in value]
            return fixed

    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("Could not load seen gifts")

    return {}


def save_seen_gifts():
    try:
        with open(SEEN_GIFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(SEEN_GIFTS_BY_QUERY, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Could not save seen gifts")


def make_seen_query_key(gift_id: int, min_stars: int, max_stars: int) -> str:
    return f"{gift_id}:{min_stars}:{max_stars}"


def get_seen_slugs(gift_id: int, min_stars: int, max_stars: int) -> set[str]:
    key = make_seen_query_key(gift_id, min_stars, max_stars)
    return set(SEEN_GIFTS_BY_QUERY.get(key, []))


def remember_seen_results(
    gift_id: int,
    min_stars: int,
    max_stars: int,
    results: List["MarketGift"],
):
    key = make_seen_query_key(gift_id, min_stars, max_stars)

    old = SEEN_GIFTS_BY_QUERY.get(key, [])
    old_set = set(old)

    for gift in results:
        if gift.slug not in old_set:
            old.append(gift.slug)
            old_set.add(gift.slug)

    SEEN_GIFTS_BY_QUERY[key] = old
    save_seen_gifts()


def clear_seen_for_query(gift_id: int, min_stars: int, max_stars: int):
    key = make_seen_query_key(gift_id, min_stars, max_stars)
    SEEN_GIFTS_BY_QUERY.pop(key, None)
    save_seen_gifts()


# ==========================
# ВЛАДЕЛЬЦЫ
# ==========================

def get_peer_key_and_raw_id(
    owner_id: Any,
    owner_name: Optional[str],
) -> Tuple[Optional[str], Optional[int], str]:
    """
    owner_id может быть:
    - PeerUser(user_id=...)
    - PeerChannel(channel_id=...)
    - PeerChat(chat_id=...)
    """

    if owner_id is None and not owner_name:
        return None, None, "не указан"

    peer_type = owner_id.__class__.__name__ if owner_id is not None else "OwnerNameOnly"

    raw_id = None

    for field_name in ("user_id", "channel_id", "chat_id"):
        value = get_field(owner_id, field_name, None)
        if value is not None:
            raw_id = safe_int(value)
            break

    if raw_id is not None:
        key = f"{peer_type}:{raw_id}"
        label = f"{peer_type}:{raw_id}"
        return key, raw_id, label

    if owner_name:
        key = f"name:{owner_name}"
        label = str(owner_name)
        return key, None, label

    key = f"unknown:{repr(owner_id)}"
    label = repr(owner_id)
    return key, None, label


async def resolve_owner_info(raw_gift: Any) -> OwnerInfo:
    """
    Пытается достать владельца подарка:
    - owner_name
    - owner_id
    - username через get_entity, если Telethon сможет его резолвнуть

    Username Telegram не всегда отдаёт.
    Если владелец скрыт/недоступен — будет owner_name или peer id.
    """

    owner_name = get_field(raw_gift, "owner_name", None)
    owner_id = get_field(raw_gift, "owner_id", None)

    key, raw_id, fallback_label = get_peer_key_and_raw_id(owner_id, owner_name)

    username = None
    link = None
    label = str(owner_name) if owner_name else fallback_label

    direct_username = (
        get_field(raw_gift, "owner_username", None)
        or get_field(raw_gift, "username", None)
    )

    if direct_username:
        username = str(direct_username).lstrip("@")
        link = f"https://t.me/{username}"
        return OwnerInfo(
            key=f"username:{username.lower()}",
            label=f"@{username}",
            username=username,
            link=link,
        )

    if owner_id is not None:
        try:
            entity = await user_client.get_entity(owner_id)

            ent_username = get_field(entity, "username", None)
            first_name = get_field(entity, "first_name", None)
            last_name = get_field(entity, "last_name", None)
            title = get_field(entity, "title", None)

            if ent_username:
                username = str(ent_username).lstrip("@")
                link = f"https://t.me/{username}"
                key = f"username:{username.lower()}"

            name_parts = []

            if first_name:
                name_parts.append(str(first_name))

            if last_name:
                name_parts.append(str(last_name))

            if title:
                label = str(title)
            elif name_parts:
                label = " ".join(name_parts)
            elif username:
                label = f"@{username}"

        except Exception as e:
            log.debug("Could not resolve owner entity %r: %s", owner_id, e)

    if username:
        shown_label = f"@{username}"
    elif owner_name:
        shown_label = str(owner_name)
    else:
        shown_label = label

    return OwnerInfo(
        key=key,
        label=shown_label,
        username=username,
        link=link,
    )


# ==========================
# КЛАВИАТУРЫ
# ==========================

def make_button_rows(buttons: List[InlineKeyboardButton], row_size: int = 1):
    rows = []

    for i in range(0, len(buttons), row_size):
        rows.append(buttons[i:i + row_size])

    return rows


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Выбрать модель подарка",
                    callback_data="models:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Чёрный список владельцев",
                    callback_data="owners_blacklist",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить модели",
                    callback_data="refresh_models",
                )
            ],
        ]
    )


def models_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total = len(BASE_GIFTS)

    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE

    page_gifts = BASE_GIFTS[start:end]

    buttons = []

    for gift in page_gifts:
        resale = gift.availability_resale or 0
        min_price = gift.resell_min_stars or 0

        text = gift.title

        if min_price:
            text += f" · от {min_price}⭐"

        if resale:
            text += f" · {resale} шт."

        buttons.append(
            InlineKeyboardButton(
                text=text[:64],
                callback_data=f"gift:{gift.gift_id}",
            )
        )

    rows = make_button_rows(buttons, row_size=1)

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"models:{page - 1}",
            )
        )

    if end < total:
        nav.append(
            InlineKeyboardButton(
                text="➡️ Далее",
                callback_data=f"models:{page + 1}",
            )
        )

    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_keyboard(results: List[MarketGift]) -> InlineKeyboardMarkup:
    rows = []

    rows.append(
        [
            InlineKeyboardButton(
                text="🔁 Найти ещё 5 новых",
                callback_data="repeat_search",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🧹 Сбросить показанные для этого запроса",
                callback_data="clear_seen_current",
            )
        ]
    )

    for i, gift in enumerate(results, 1):
        if gift.owner.key:
            owner_text = gift.owner.display
            text = f"🚫 Забанить владельца #{i}"

            if owner_text and owner_text != "не указан":
                text += f" · {owner_text}"

            rows.append(
                [
                    InlineKeyboardButton(
                        text=text[:64],
                        callback_data=f"ban_owner:{i - 1}",
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                text="🚫 Чёрный список",
                callback_data="owners_blacklist",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def blacklist_keyboard() -> InlineKeyboardMarkup:
    rows = []

    items = list(OWNERS_BLACKLIST.items())[:20]

    for i, (owner_key, owner_label) in enumerate(items, 1):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Удалить #{i} · {owner_label}"[:64],
                    callback_data=f"unban_owner:{i - 1}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🧹 Очистить чёрный список",
                callback_data="clear_owners_blacklist",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==========================
# ЗАГРУЗКА МОДЕЛЕЙ
# ==========================

async def load_base_gifts() -> List[BaseGift]:
    log.info("Loading base star gifts...")

    result = await user_client(
        functions.payments.GetStarGiftsRequest(hash=0)
    )

    raw_gifts = getattr(result, "gifts", []) or []

    gifts: List[BaseGift] = []

    for raw_gift in raw_gifts:
        gift_id = safe_int(get_field(raw_gift, "id", None))
        title = get_field(raw_gift, "title", None)

        if not gift_id or not title:
            continue

        availability_resale = get_field(raw_gift, "availability_resale", None)
        resell_min_stars = get_field(raw_gift, "resell_min_stars", None)

        if not availability_resale:
            continue

        gift = BaseGift(
            gift_id=gift_id,
            title=str(title),
            stars=get_field(raw_gift, "stars", None),
            availability_resale=safe_int(availability_resale),
            resell_min_stars=safe_int(resell_min_stars),
            sold_out=get_field(raw_gift, "sold_out", None),
        )

        gifts.append(gift)

    gifts.sort(
        key=lambda g: (
            g.resell_min_stars if g.resell_min_stars is not None else 10**18,
            g.title.lower(),
        )
    )

    log.info("Loaded %s base gifts with resale", len(gifts))

    return gifts


async def ensure_models_loaded():
    global BASE_GIFTS, BASE_GIFTS_BY_ID

    if BASE_GIFTS:
        return

    BASE_GIFTS = await load_base_gifts()
    BASE_GIFTS_BY_ID = {gift.gift_id: gift for gift in BASE_GIFTS}


# ==========================
# ПОИСК НА МАРКЕТЕ
# ==========================

def build_resale_request(
    gift_id: int,
    offset: str,
    limit: int,
):
    """
    В разных версиях Telethon аргументы могут отличаться.
    Поэтому подставляем только те kwargs, которые реально есть в конструкторе.
    """

    cls = functions.payments.GetResaleStarGiftsRequest
    sig = inspect.signature(cls)

    possible_kwargs = {
        "gift_id": gift_id,
        "offset": offset,
        "limit": limit,
        "sort_by_price": True,
        "sort_by_num": False,
        "attributes": None,
        "attributes_hash": 0,
        "stars_only": True,
        "for_craft": False,
    }

    kwargs = {}

    for name in sig.parameters:
        if name in possible_kwargs:
            kwargs[name] = possible_kwargs[name]

    return cls(**kwargs)


async def find_market_gifts(
    gift_id: int,
    min_stars: int,
    max_stars: int,
    need: int = SEARCH_RESULT_LIMIT,
    skip_slugs: Optional[set[str]] = None,
) -> List[MarketGift]:
    found: List[MarketGift] = []

    if skip_slugs is None:
        skip_slugs = set()

    offset = ""
    pages = 0

    while len(found) < need and pages < MAX_MARKET_PAGES:
        pages += 1

        try:
            request = build_resale_request(
                gift_id=gift_id,
                offset=offset,
                limit=REQUEST_PAGE_LIMIT,
            )

            result = await user_client(request)

        except FloodWaitError as e:
            wait_seconds = int(getattr(e, "seconds", 5))
            log.warning("FloodWait %s seconds", wait_seconds)
            await asyncio.sleep(wait_seconds)
            continue

        gifts = getattr(result, "gifts", []) or []

        if not gifts:
            break

        for raw_gift in gifts:
            slug = get_field(raw_gift, "slug", None)
            title = get_field(raw_gift, "title", None)
            num = get_field(raw_gift, "num", None)

            price = extract_stars_amount(
                get_field(raw_gift, "resell_amount", None)
            )

            if not slug:
                continue

            slug = str(slug)

            if slug in skip_slugs:
                continue

            if not price:
                continue

            if price < min_stars:
                continue

            if price > max_stars:
                return found

            owner = await resolve_owner_info(raw_gift)

            if is_owner_blacklisted(owner.key):
                log.info(
                    "Skip blacklisted owner: %s | username=%s | gift=%s",
                    owner.label,
                    owner.username,
                    slug,
                )
                continue

            found.append(
                MarketGift(
                    title=str(title or "Gift"),
                    num=safe_int(num) if num is not None else None,
                    slug=slug,
                    price=price,
                    owner=owner,
                )
            )

            if len(found) >= need:
                return found

        next_offset = getattr(result, "next_offset", None)

        if not next_offset:
            break

        offset = next_offset

    return found


# ==========================
# ФОРМАТИРОВАНИЕ ОТВЕТОВ
# ==========================

def format_owner_line(owner: OwnerInfo) -> str:
    if owner.username and owner.link:
        return f'Владелец: <a href="{html.escape(owner.link)}">@{html.escape(owner.username)}</a>'

    if owner.label:
        return f"Владелец: <code>{html.escape(owner.label)}</code>"

    return "Владелец: <code>не указан</code>"


def format_market_results(base_gift: BaseGift, results: List[MarketGift]) -> str:
    if not results:
        return (
            f"По модели <b>{html.escape(base_gift.title)}</b> ничего не найдено "
            f"в указанном диапазоне с учётом чёрного списка и уже показанных ссылок."
        )

    lines = [
        f"Нашёл <b>{len(results)}</b> подарков по модели "
        f"<b>{html.escape(base_gift.title)}</b>:\n"
    ]

    for i, gift in enumerate(results, 1):
        num_part = f" #{gift.num}" if gift.num is not None else ""
        username_text = f"@{gift.owner.username}" if gift.owner.username else "нет username / скрыт"

        lines.append(
            f"{i}. <b>{html.escape(gift.title)}{num_part}</b>\n"
            f"Цена: <b>{gift.price} ⭐</b>\n"
            f"{format_owner_line(gift.owner)}\n"
            f"Username: <code>{html.escape(username_text)}</code>\n"
            f"Подарок: {html.escape(gift.link)}\n"
        )

    return "\n".join(lines)


# ==========================
# КОМАНДЫ
# ==========================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin_user(message.from_user.id if message.from_user else None):
        return

    if not await ensure_session_for_message(message):
        return

    await ensure_models_loaded()

    await message.answer(
        "Привет. Я готов искать подарки на официальном Telegram-маркете.\n\n"
        "Нажми кнопку ниже, выбери модель подарка и введи диапазон цены.\n\n"
        "Пример диапазона:\n"
        "<code>500 800</code>",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )



@dp.callback_query(F.data == "auth_start")
async def cb_auth_start(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    await ensure_user_client_connected()

    if await is_user_client_authorized():
        await callback.message.edit_text(
            "Сессия уже добавлена. Можно пользоваться ботом.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return

    AUTH_STATES_BY_USER[callback.from_user.id] = {"step": "phone"}

    await callback.message.edit_text(
        "Отправь номер телефона аккаунта Telegram в международном формате.\n\n"
        "Пример:\n"
        "<code>+79991234567</code>",
        parse_mode="HTML",
        reply_markup=auth_cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "auth_cancel")
async def cb_auth_cancel(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    AUTH_STATES_BY_USER.pop(callback.from_user.id, None)

    if await is_user_client_authorized():
        await callback.message.edit_text(
            "Авторизация отменена. Текущая сессия активна.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await callback.message.edit_text(
            "Авторизация отменена. Без сессии парсер не сможет искать подарки.",
            reply_markup=session_required_keyboard(),
        )

    await callback.answer()


@dp.message(Command("reload"))
async def cmd_reload(message: Message):
    if not is_admin_user(message.from_user.id if message.from_user else None):
        return

    if not await ensure_session_for_message(message):
        return

    global BASE_GIFTS, BASE_GIFTS_BY_ID

    await message.answer("Обновляю список моделей...")

    try:
        BASE_GIFTS = await load_base_gifts()
        BASE_GIFTS_BY_ID = {gift.gift_id: gift for gift in BASE_GIFTS}

        await message.answer(
            f"Готово. Загружено моделей с ресейлом: <b>{len(BASE_GIFTS)}</b>",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("reload error")
        await message.answer(
            f"Ошибка обновления:\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>",
            parse_mode="HTML",
        )


@dp.message(Command("blacklist"))
async def cmd_blacklist(message: Message):
    if not is_admin_user(message.from_user.id if message.from_user else None):
        return

    await send_blacklist_text(message)


# ==========================
# CALLBACK: МЕНЮ И МОДЕЛИ
# ==========================

@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    if not await ensure_session_for_callback(callback):
        return

    await callback.message.edit_text(
        "Главное меню.",
        reply_markup=main_menu_keyboard(),
    )

    await callback.answer()


@dp.callback_query(F.data == "refresh_models")
async def cb_refresh_models(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    if not await ensure_session_for_callback(callback):
        return

    global BASE_GIFTS, BASE_GIFTS_BY_ID

    await callback.message.edit_text("Обновляю список моделей...")

    try:
        BASE_GIFTS = await load_base_gifts()
        BASE_GIFTS_BY_ID = {gift.gift_id: gift for gift in BASE_GIFTS}

        await callback.message.edit_text(
            f"Готово. Загружено моделей: <b>{len(BASE_GIFTS)}</b>",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("refresh error")
        await callback.message.edit_text(
            f"Ошибка:\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>",
            parse_mode="HTML",
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("models:"))
async def cb_models(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    if not await ensure_session_for_callback(callback):
        return

    await ensure_models_loaded()

    try:
        page = int(callback.data.split(":", 1)[1])
    except Exception:
        page = 0

    if not BASE_GIFTS:
        await callback.message.edit_text(
            "Модели с ресейлом не найдены. Попробуй /reload.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return

    total_pages = max(1, (len(BASE_GIFTS) + GIFTS_PER_PAGE - 1) // GIFTS_PER_PAGE)

    await callback.message.edit_text(
        f"Выбери модель подарка.\n\n"
        f"Страница <b>{page + 1}</b> из <b>{total_pages}</b>.",
        reply_markup=models_keyboard(page),
        parse_mode="HTML",
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("gift:"))
async def cb_select_gift(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    if not await ensure_session_for_callback(callback):
        return

    await ensure_models_loaded()

    try:
        gift_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Ошибка gift_id")
        return

    gift = BASE_GIFTS_BY_ID.get(gift_id)

    if not gift:
        await callback.message.edit_text(
            "Эта модель не найдена в кэше. Нажми /reload и попробуй снова.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return

    USER_SELECTED_GIFT[callback.from_user.id] = gift_id

    min_hint = gift.resell_min_stars or 0

    await callback.message.edit_text(
        f"Выбрана модель:\n"
        f"<b>{html.escape(gift.title)}</b>\n\n"
        f"Минимальная цена на ресейле сейчас примерно: <b>{min_hint} ⭐</b>\n"
        f"Доступно на ресейле: <b>{gift.availability_resale}</b>\n\n"
        f"Теперь отправь диапазон цены одним сообщением.\n\n"
        f"Пример:\n"
        f"<code>500 800</code>",
        parse_mode="HTML",
    )

    await callback.answer()


# ==========================
# CALLBACK: BLACKLIST
# ==========================

async def send_blacklist_text(message_or_callback_message):
    if not OWNERS_BLACKLIST:
        await message_or_callback_message.answer(
            "Чёрный список владельцев пуст.",
            reply_markup=main_menu_keyboard(),
        )
        return

    lines = ["🚫 <b>Чёрный список владельцев:</b>\n"]

    for i, (owner_key, owner_label) in enumerate(OWNERS_BLACKLIST.items(), 1):
        lines.append(
            f"{i}. <code>{html.escape(owner_label)}</code>\n"
            f"key: <code>{html.escape(owner_key)}</code>\n"
        )

    text = "\n".join(lines)

    if len(text) > 3900:
        text = text[:3900] + "\n\n...список обрезан."

    await message_or_callback_message.answer(
        text,
        parse_mode="HTML",
        reply_markup=blacklist_keyboard(),
    )


@dp.callback_query(F.data == "owners_blacklist")
async def cb_owners_blacklist(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    if not OWNERS_BLACKLIST:
        await callback.message.edit_text(
            "Чёрный список владельцев пуст.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return

    lines = ["🚫 <b>Чёрный список владельцев:</b>\n"]

    for i, (owner_key, owner_label) in enumerate(OWNERS_BLACKLIST.items(), 1):
        lines.append(
            f"{i}. <code>{html.escape(owner_label)}</code>\n"
            f"key: <code>{html.escape(owner_key)}</code>\n"
        )

    text = "\n".join(lines)

    if len(text) > 3900:
        text = text[:3900] + "\n\n...список обрезан."

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=blacklist_keyboard(),
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("ban_owner:"))
async def cb_ban_owner(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Ошибка индекса")
        return

    results = LAST_RESULTS_BY_USER.get(callback.from_user.id, [])

    if index < 0 or index >= len(results):
        await callback.answer("Результат уже устарел")
        return

    gift = results[index]

    if not gift.owner.key:
        await callback.answer("У этого подарка нет владельца")
        return

    OWNERS_BLACKLIST[gift.owner.key] = gift.owner.display
    save_owners_blacklist()

    await callback.answer("Владелец добавлен в чёрный список")

    username_line = ""

    if gift.owner.username:
        username_line = f"\nUsername: <code>@{html.escape(gift.owner.username)}</code>"

    await callback.message.answer(
        f"🚫 Добавил владельца в чёрный список:\n"
        f"<code>{html.escape(gift.owner.display)}</code>"
        f"{username_line}\n\n"
        f"Теперь подарки этого владельца будут пропускаться.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@dp.callback_query(F.data.startswith("unban_owner:"))
async def cb_unban_owner(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Ошибка индекса")
        return

    items = list(OWNERS_BLACKLIST.items())

    if index < 0 or index >= len(items):
        await callback.answer("Запись не найдена")
        return

    owner_key, owner_label = items[index]
    OWNERS_BLACKLIST.pop(owner_key, None)
    save_owners_blacklist()

    await callback.answer("Владелец удалён")

    await callback.message.edit_text(
        f"✅ Удалил из чёрного списка:\n"
        f"<code>{html.escape(owner_label)}</code>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@dp.callback_query(F.data == "clear_owners_blacklist")
async def cb_clear_owners_blacklist(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    OWNERS_BLACKLIST.clear()
    save_owners_blacklist()

    await callback.answer("Чёрный список очищен")

    await callback.message.edit_text(
        "Чёрный список владельцев очищен.",
        reply_markup=main_menu_keyboard(),
    )


# ==========================
# CALLBACK: ПОВТОР ПОИСКА
# ==========================

@dp.callback_query(F.data == "repeat_search")
async def cb_repeat_search(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    if not await ensure_session_for_callback(callback):
        return

    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)

    if not search:
        await callback.answer("Нет прошлого поиска")
        await callback.message.answer(
            "Сначала выбери модель и введи диапазон.",
            reply_markup=main_menu_keyboard(),
        )
        return

    gift_id = search["gift_id"]
    min_stars = search["min_stars"]
    max_stars = search["max_stars"]

    base_gift = BASE_GIFTS_BY_ID.get(gift_id)

    if not base_gift:
        await callback.answer("Модель не найдена")
        await callback.message.answer(
            "Модель потерялась из кэша. Нажми /reload и выбери заново.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await callback.answer("Ищу ещё 5 новых...")

    status = await callback.message.answer(
        f"Ищу ещё 5 новых <b>{html.escape(base_gift.title)}</b> "
        f"от <b>{min_stars}</b> до <b>{max_stars}</b> ⭐...\n\n"
        f"Уже показано по этому запросу: "
        f"<b>{len(get_seen_slugs(gift_id, min_stars, max_stars))}</b>",
        parse_mode="HTML",
    )

    try:
        seen_slugs = get_seen_slugs(gift_id, min_stars, max_stars)

        results = await find_market_gifts(
            gift_id=gift_id,
            min_stars=min_stars,
            max_stars=max_stars,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen_slugs,
        )

        LAST_RESULTS_BY_USER[user_id] = results
        remember_seen_results(gift_id, min_stars, max_stars, results)

        if not results:
            await status.edit_text(
                f"Новых подарков больше не нашёл по модели "
                f"<b>{html.escape(base_gift.title)}</b> "
                f"в диапазоне <b>{min_stars}-{max_stars}</b> ⭐.\n\n"
                f"Можно сбросить историю показанных ссылок и начать заново.",
                parse_mode="HTML",
                reply_markup=search_results_keyboard([]),
            )
            return

        answer = format_market_results(base_gift, results)

        await status.edit_text(
            answer,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=search_results_keyboard(results),
        )

    except Exception as e:
        log.exception("repeat search error")

        await status.edit_text(
            "Ошибка повторного поиска.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )


@dp.callback_query(F.data == "clear_seen_current")
async def cb_clear_seen_current(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return

    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)

    if not search:
        await callback.answer("Нет активного запроса")
        return

    gift_id = search["gift_id"]
    min_stars = search["min_stars"]
    max_stars = search["max_stars"]

    clear_seen_for_query(gift_id, min_stars, max_stars)

    await callback.answer("История показанных ссылок сброшена")

    await callback.message.answer(
        "🧹 Сбросил показанные ссылки для текущей модели и диапазона.\n"
        "Теперь можно снова искать с начала.",
        reply_markup=main_menu_keyboard(),
    )


# ==========================
# ОБРАБОТКА ДИАПАЗОНА ЦЕН
# ==========================

@dp.message()
async def handle_price_range(message: Message):
    if not is_admin_user(message.from_user.id if message.from_user else None):
        return

    user_id = message.from_user.id

    if user_id in AUTH_STATES_BY_USER:
        await handle_auth_message(message)
        return

    if not await ensure_session_for_message(message):
        return

    if user_id not in USER_SELECTED_GIFT:
        await message.answer(
            "Сначала выбери модель подарка.",
            reply_markup=main_menu_keyboard(),
        )
        return

    text = (message.text or "").strip().replace("-", " ")
    parts = text.split()

    if len(parts) != 2:
        await message.answer(
            "Отправь диапазон двумя числами.\n\n"
            "Пример:\n"
            "<code>500 800</code>",
            parse_mode="HTML",
        )
        return

    try:
        min_stars = int(parts[0])
        max_stars = int(parts[1])
    except ValueError:
        await message.answer(
            "Диапазон должен быть числами.\n\n"
            "Пример:\n"
            "<code>500 800</code>",
            parse_mode="HTML",
        )
        return

    if min_stars < 0 or max_stars < 0 or min_stars > max_stars:
        await message.answer(
            "Неверный диапазон. Минимум должен быть меньше максимума.\n\n"
            "Пример:\n"
            "<code>500 800</code>",
            parse_mode="HTML",
        )
        return

    gift_id = USER_SELECTED_GIFT[user_id]
    base_gift = BASE_GIFTS_BY_ID.get(gift_id)

    if not base_gift:
        await message.answer(
            "Модель потерялась из кэша. Нажми /reload и выбери заново.",
            reply_markup=main_menu_keyboard(),
        )
        return

    LAST_SEARCH_BY_USER[user_id] = {
        "gift_id": gift_id,
        "min_stars": min_stars,
        "max_stars": max_stars,
    }

    status = await message.answer(
        f"Ищу <b>{html.escape(base_gift.title)}</b> "
        f"от <b>{min_stars}</b> до <b>{max_stars}</b> ⭐...\n\n"
        f"Чёрный список владельцев: <b>{len(OWNERS_BLACKLIST)}</b>\n"
        f"Уже показано по этому запросу: "
        f"<b>{len(get_seen_slugs(gift_id, min_stars, max_stars))}</b>",
        parse_mode="HTML",
    )

    try:
        seen_slugs = get_seen_slugs(gift_id, min_stars, max_stars)

        results = await find_market_gifts(
            gift_id=gift_id,
            min_stars=min_stars,
            max_stars=max_stars,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen_slugs,
        )

        LAST_RESULTS_BY_USER[user_id] = results
        remember_seen_results(gift_id, min_stars, max_stars, results)

        answer = format_market_results(base_gift, results)

        await status.edit_text(
            answer,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=search_results_keyboard(results),
        )

    except Exception as e:
        log.exception("search error")

        await status.edit_text(
            "Ошибка поиска.\n\n"
            f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>\n\n"
            "Скинь мне этот лог, я подправлю код под твою структуру ответа.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )


# ==========================
# ЗАПУСК
# ==========================

async def main():
    global OWNERS_BLACKLIST, SEEN_GIFTS_BY_QUERY

    OWNERS_BLACKLIST = load_owners_blacklist()
    SEEN_GIFTS_BY_QUERY = load_seen_gifts()

    log.info("Loaded owners blacklist: %s", len(OWNERS_BLACKLIST))
    log.info("Loaded seen gift queries: %s", len(SEEN_GIFTS_BY_QUERY))

    log.info("Connecting Telethon userbot...")

    # Важно: здесь больше нет user_client.start(phone=...).
    # Если session-файл уже есть — бот сразу работает.
    # Если session-файла нет — админ добавит его через /start в чате с ботом.
    await ensure_user_client_connected()

    if await is_user_client_authorized():
        me = await user_client.get_me()

        log.info(
            "Telethon signed in as %s (%s)",
            getattr(me, "first_name", None),
            getattr(me, "id", None),
        )

        log.info("Preloading gift models...")

        try:
            await ensure_models_loaded()
            log.info("Models loaded: %s", len(BASE_GIFTS))
        except Exception:
            log.exception("Could not preload models. Bot will still start.")
    else:
        log.info("Telethon session is not authorized yet. Admin must add session via bot chat.")

    log.info("Starting aiogram polling...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())