import asyncio
import html
import inspect
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
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
# НАСТРОЙКИ (ВПИШИ СВОИ ДАННЫЕ)
# ==========================

API_ID = 26259835
API_HASH = "3fa32264398920f001dd2428b42060f6"
BOT_TOKEN = "8740807130:AAEXt1_6ynUsMkJZWqH112iV07g6agTMbMA"
ADMIN_ID = 8002472821

# Канал, на который должен быть подписан пользователь
REQUIRED_CHANNEL = "@fcklole"
REQUIRED_CHANNEL_URL = "https://t.me/fcklole"

# ID группы для мониторинга (оставь None если не нужен)
MONITOR_CHAT_ID = -1004223195405

# Интервал проверки мониторинга (секунды)
MONITOR_INTERVAL = 60

# ==========================

SESSION_NAME = "telethon_market_userbot"

GIFTS_PER_PAGE = 8
SEARCH_RESULT_LIMIT = 10
REQUEST_PAGE_LIMIT = 50
MAX_MARKET_PAGES = 30

OWNERS_BLACKLIST_FILE = "owners_blacklist.json"
SEEN_GIFTS_FILE = "seen_gifts.json"
SENT_MONITOR_SLUGS_FILE = "sent_monitor_slugs.json"
SETTINGS_FILE = "bot_settings.json"

# ==========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("nft-gift-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

BASE_GIFTS: List["BaseGift"] = []
BASE_GIFTS_BY_ID: Dict[int, "BaseGift"] = {}

USER_SELECTED_GIFT: Dict[int, int] = {}
LAST_RESULTS_BY_USER: Dict[int, List["MarketGift"]] = {}
LAST_SEARCH_BY_USER: Dict[int, Dict[str, int]] = {}

OWNERS_BLACKLIST: Dict[str, str] = {}
SEEN_GIFTS_BY_QUERY: Dict[str, List[str]] = {}
SENT_MONITOR_SLUGS: set = set()

AUTH_STATES_BY_USER: Dict[int, Dict[str, Any]] = {}

monitor_running = False
monitor_task = None

# Настройки отправки
bot_settings = {
    "links_per_message": 10,
    "delay_between_batches": 30,
}
LINKS_PER_MESSAGE = bot_settings["links_per_message"]
DELAY_BETWEEN_BATCHES = bot_settings["delay_between_batches"]
last_send_time_by_model: Dict[str, float] = {}


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
# ЗАГРУЗКА/СОХРАНЕНИЕ
# ==========================

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {**bot_settings, **data}
    except:
        return bot_settings.copy()


def save_settings(settings: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except:
        pass


def load_owners_blacklist() -> Dict[str, str]:
    try:
        with open(OWNERS_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): str(v) for k, v in data.items()}
    except:
        return {}


def save_owners_blacklist():
    try:
        with open(OWNERS_BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(OWNERS_BLACKLIST, f, ensure_ascii=False, indent=2)
    except:
        pass


def load_seen_gifts() -> Dict[str, List[str]]:
    try:
        with open(SEEN_GIFTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
    except:
        return {}


def save_seen_gifts():
    try:
        with open(SEEN_GIFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(SEEN_GIFTS_BY_QUERY, f, ensure_ascii=False, indent=2)
    except:
        pass


def load_sent_monitor_slugs() -> set:
    try:
        with open(SENT_MONITOR_SLUGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except:
        return set()


def save_sent_monitor_slugs(slugs: set):
    try:
        with open(SENT_MONITOR_SLUGS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(slugs), f, ensure_ascii=False, indent=2)
    except:
        pass


# ==========================
# ПРОВЕРКА ПОДПИСКИ
# ==========================

def is_admin_user(user_id: Optional[int]) -> bool:
    return user_id == ADMIN_ID


async def is_user_subscribed(user_id: Optional[int]) -> bool:
    if user_id is None or is_admin_user(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        status = str(getattr(member, "status", "")).lower()
        return status not in {"left", "kicked"}
    except:
        return False


async def ensure_access(message: Message) -> bool:
    if await is_user_subscribed(message.from_user.id if message.from_user else None):
        return True
    await message.answer(
        f"⚠️ Подпишись на канал: {REQUIRED_CHANNEL_URL}\n\nПосле подписки нажми /start",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 ПОДПИСАТЬСЯ", url=REQUIRED_CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ ПРОВЕРИТЬ", callback_data="check_sub")]
        ])
    )
    return False


@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery):
    if await is_user_subscribed(callback.from_user.id):
        await callback.message.edit_text("✅ Подписка подтверждена! Нажми /start", reply_markup=main_menu_keyboard(callback.from_user.id))
    else:
        await callback.answer("❌ Подписка не найдена", show_alert=True)


# ==========================
# АВТОРИЗАЦИЯ TELETHON
# ==========================

def normalize_login_code(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def session_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔐 Добавить / обновить сессию", callback_data="auth_start")]]
    )


def auth_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="auth_cancel")]]
    )


async def ensure_user_client_connected():
    if not user_client.is_connected():
        await user_client.connect()


async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    return bool(await user_client.is_user_authorized())


async def send_session_required_message(message_or_callback_message, user_id: Optional[int] = None):
    if is_admin_user(user_id):
        await message_or_callback_message.answer(
            "Сессия Telegram ещё не добавлена.\n\n"
            "Нажми кнопку ниже, введи номер телефона, потом код из Telegram. "
            "Код можно отправить через #, например:\n"
            "<code>1#2#3#4#5</code>",
            reply_markup=session_required_keyboard(),
            parse_mode="HTML",
        )
        return
    await message_or_callback_message.answer(
        "Парсер пока не подключён к Telegram-сессии. Админ должен один раз добавить сессию через /start."
    )


async def ensure_session_for_message(message: Message) -> bool:
    if await is_user_client_authorized():
        return True
    user_id = message.from_user.id if message.from_user else None
    await send_session_required_message(message, user_id)
    return False


async def ensure_session_for_callback(callback: CallbackQuery) -> bool:
    if await is_user_client_authorized():
        return True
    user_id = callback.from_user.id if callback.from_user else None
    if is_admin_user(user_id):
        await callback.answer("Сначала добавь сессию", show_alert=True)
        await callback.message.answer(
            "Сессия Telegram ещё не добавлена.\n\nДобавь её через чат с ботом, чтобы парсер мог работать.",
            reply_markup=session_required_keyboard(),
        )
    else:
        await callback.answer("Парсер пока не настроен админом", show_alert=True)
        await callback.message.answer(
            "Парсер пока не подключён к Telegram-сессии. Админ должен один раз добавить сессию."
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
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("Could not load models after auth")
        await message.answer(
            "Сессия добавлена, но модели пока не загрузились.\n\n"
            f"Ошибка:\n<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>\n\n"
            "Попробуй нажать /reload.",
            reply_markup=main_menu_keyboard(user_id),
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
                "Отправь номер телефона в международном формате.\n\nПример:\n<code>+79991234567</code>",
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
        AUTH_STATES_BY_USER[user_id] = {"step": "code", "phone": phone}
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
                "Код слишком короткий. Отправь код из Telegram, например:\n<code>1#2#3#4#5</code>",
                parse_mode="HTML",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        try:
            await ensure_user_client_connected()
            await user_client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            AUTH_STATES_BY_USER[user_id] = {"step": "password", "phone": phone}
            await message.answer(
                "На аккаунте включена облачная 2FA.\n\nОтправь пароль от двухэтапной проверки одним сообщением.",
                reply_markup=auth_cancel_keyboard(),
            )
            return
        except PhoneCodeInvalidError:
            await message.answer(
                "Код неверный. Отправь код ещё раз.\n\nФормат через # тоже подходит: <code>1#2#3#4#5</code>",
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
        return int(value) if value is not None else default
    except:
        return default


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_stars_amount(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        for item in value:
            amt = extract_stars_amount(item)
            if amt:
                return amt
        return 0
    amt = get_field(value, "amount")
    if amt is not None:
        return safe_int(amt)
    stars = get_field(value, "stars")
    if stars is not None:
        return safe_int(stars)
    return 0


# ==========================
# BLACKLIST ВЛАДЕЛЬЦЕВ
# ==========================

def is_owner_blacklisted(key: Optional[str]) -> bool:
    return key in OWNERS_BLACKLIST if key else False


# ==========================
# ИСТОРИЯ ПОКАЗАННЫХ ПОДАРКОВ
# ==========================

def get_seen_slugs(gift_id: int, min_stars: int, max_stars: int) -> set:
    key = f"{gift_id}:{min_stars}:{max_stars}"
    return set(SEEN_GIFTS_BY_QUERY.get(key, []))


def remember_seen_results(gift_id: int, min_stars: int, max_stars: int, results: List[MarketGift]):
    key = f"{gift_id}:{min_stars}:{max_stars}"
    old = set(SEEN_GIFTS_BY_QUERY.get(key, []))
    for gift in results:
        if gift.slug not in old:
            SEEN_GIFTS_BY_QUERY.setdefault(key, []).append(gift.slug)
    save_seen_gifts()


def clear_seen_for_query(gift_id: int, min_stars: int, max_stars: int):
    key = f"{gift_id}:{min_stars}:{max_stars}"
    SEEN_GIFTS_BY_QUERY.pop(key, None)
    save_seen_gifts()


# ==========================
# ВЛАДЕЛЬЦЫ
# ==========================

async def resolve_owner_info(raw_gift: Any) -> OwnerInfo:
    owner_name = get_field(raw_gift, "owner_name")
    owner_id = get_field(raw_gift, "owner_id")
    
    direct_username = get_field(raw_gift, "owner_username") or get_field(raw_gift, "username")
    if direct_username:
        username = str(direct_username).lstrip("@")
        return OwnerInfo(
            key=f"username:{username.lower()}",
            label=f"@{username}",
            username=username,
            link=f"https://t.me/{username}"
        )
    
    if owner_id is not None:
        try:
            entity = await user_client.get_entity(owner_id)
            username = getattr(entity, "username", None)
            if username:
                return OwnerInfo(
                    key=f"username:{username.lower()}",
                    label=f"@{username}",
                    username=username,
                    link=f"https://t.me/{username}"
                )
            name = getattr(entity, "first_name", "") or getattr(entity, "title", "") or str(owner_id)
            return OwnerInfo(key=f"id:{owner_id}", label=name[:30], username=None, link=None)
        except:
            pass
    
    label = str(owner_name) if owner_name else "не указан"
    return OwnerInfo(key=owner_name, label=label, username=None, link=None)


# ==========================
# КЛАВИАТУРЫ
# ==========================

def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📦 ВЫБРАТЬ МОДЕЛЬ", callback_data="models:0")],
        [InlineKeyboardButton(text="🚫 ЧЁРНЫЙ СПИСОК", callback_data="owners_blacklist")],
    ]
    if MONITOR_CHAT_ID and is_admin_user(user_id):
        rows.insert(1, [InlineKeyboardButton(text="📡 МОНИТОРИНГ", callback_data="monitor_admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total = len(BASE_GIFTS)
    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE
    page_gifts = BASE_GIFTS[start:end]
    buttons = []
    for gift in page_gifts:
        text = f"{gift.title}"
        if gift.resell_min_stars:
            text += f" · от {gift.resell_min_stars}⭐"
        if gift.availability_resale:
            text += f" · {gift.availability_resale} шт."
        buttons.append(InlineKeyboardButton(text=text[:60], callback_data=f"gift:{gift.gift_id}"))
    rows = [[b] for b in buttons]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"models:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"models:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_keyboard(results: List[MarketGift]) -> InlineKeyboardMarkup:
    rows = []
    for i, gift in enumerate(results):
        if gift.owner.key and is_admin_user(ADMIN_ID):
            rows.append([InlineKeyboardButton(text=f"🚫 ЗАБАНИТЬ {gift.owner.display}", callback_data=f"ban_owner:{i}")])
    rows.append([InlineKeyboardButton(text="🔁 ПОВТОРИТЬ ПОИСК", callback_data="repeat_search")])
    rows.append([InlineKeyboardButton(text="🧹 СБРОСИТЬ ИСТОРИЮ", callback_data="clear_seen_current")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def blacklist_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(OWNERS_BLACKLIST.items())
    for i, (key, label) in enumerate(items[:20]):
        rows.append([InlineKeyboardButton(text=f"❌ {label}", callback_data=f"unban_owner:{key}")])
    if rows:
        rows.append([InlineKeyboardButton(text="🧹 ОЧИСТИТЬ ВСЁ", callback_data="clear_owners_blacklist")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def monitor_admin_keyboard() -> InlineKeyboardMarkup:
    status = "🟢 РАБОТАЕТ" if monitor_running else "🔴 ОСТАНОВЛЕН"
    buttons = [
        [InlineKeyboardButton(text=f"📊 СТАТУС: {status}", callback_data="monitor_status")],
        [InlineKeyboardButton(text="▶️ ЗАПУСТИТЬ", callback_data="monitor_start")] if not monitor_running else [],
        [InlineKeyboardButton(text="⏹️ ОСТАНОВИТЬ", callback_data="monitor_stop")] if monitor_running else [],
        [InlineKeyboardButton(text="🔄 СБРОСИТЬ ИСТОРИЮ", callback_data="monitor_reset")],
        [InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=[b for b in buttons if b])


# ==========================
# ЗАГРУЗКА МОДЕЛЕЙ
# ==========================

async def load_base_gifts() -> List[BaseGift]:
    log.info("Loading base star gifts...")
    result = await user_client(functions.payments.GetStarGiftsRequest(hash=0))
    raw_gifts = getattr(result, "gifts", []) or []
    gifts = []
    for raw in raw_gifts:
        gift_id = safe_int(get_field(raw, "id"))
        title = get_field(raw, "title")
        if not gift_id or not title:
            continue
        availability_resale = get_field(raw, "availability_resale")
        if not availability_resale:
            continue
        gifts.append(BaseGift(
            gift_id=gift_id,
            title=str(title),
            stars=get_field(raw, "stars"),
            availability_resale=safe_int(availability_resale),
            resell_min_stars=safe_int(get_field(raw, "resell_min_stars")),
            sold_out=get_field(raw, "sold_out"),
        ))
    gifts.sort(key=lambda g: (g.resell_min_stars or 999999, g.title.lower()))
    log.info("Loaded %s base gifts", len(gifts))
    return gifts


async def ensure_models_loaded():
    global BASE_GIFTS, BASE_GIFTS_BY_ID
    if not BASE_GIFTS:
        BASE_GIFTS = await load_base_gifts()
        BASE_GIFTS_BY_ID = {g.gift_id: g for g in BASE_GIFTS}


# ==========================
# ПОИСК
# ==========================

async def find_market_gifts(
    gift_id: int,
    min_stars: int,
    max_stars: int,
    need: int = SEARCH_RESULT_LIMIT,
    skip_slugs: set = None,
) -> List[MarketGift]:
    found = []
    skip_slugs = skip_slugs or set()
    offset = ""
    pages = 0

    while len(found) < need and pages < MAX_MARKET_PAGES:
        pages += 1
        try:
            result = await user_client(
                functions.payments.GetResaleStarGiftsRequest(
                    gift_id=gift_id,
                    offset=offset,
                    limit=REQUEST_PAGE_LIMIT,
                    sort_by_price=True,
                    sort_by_num=False,
                    stars_only=True,
                    for_craft=False,
                )
            )
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:
            log.error(f"Search error: {e}")
            break

        gifts = getattr(result, "gifts", [])
        if not gifts:
            break

        for raw in gifts:
            slug = get_field(raw, "slug")
            if not slug or slug in skip_slugs:
                continue
            price = extract_stars_amount(get_field(raw, "resell_amount"))
            if price < min_stars:
                continue
            if price > max_stars:
                return found
            owner = await resolve_owner_info(raw)
            if is_owner_blacklisted(owner.key):
                continue
            found.append(
                MarketGift(
                    title=str(get_field(raw, "title") or "Gift"),
                    num=safe_int(get_field(raw, "num")),
                    slug=slug,
                    price=price,
                    owner=owner,
                )
            )
            if len(found) >= need:
                return found
        offset = getattr(result, "next_offset", "")
        if not offset:
            break
    return found


# ==========================
# ФОРМАТИРОВАНИЕ ВЫВОДА
# ==========================

def format_gift_list(gifts: List[MarketGift]) -> str:
    lines = []
    for i, g in enumerate(gifts, 1):
        num = f" #{g.num}" if g.num else ""
        owner = f"@{g.owner.username}" if g.owner.username else g.owner.label
        lines.append(
            f"{i}. {g.title}{num}\n"
            f"💰 Цена: {g.price} ⭐\n"
            f"👤 Владелец: {owner}\n"
            f"🔗 {g.link}"
        )
    return "\n\n".join(lines)


async def send_search_results(
    message: Message, base: BaseGift, results: List[MarketGift], min_price: int, max_price: int
):
    if not results:
        await message.answer(
            f"❌ По модели {base.title} ничего не найдено в диапазоне {min_price}-{max_price} ⭐"
        )
        return

    text = (
        f"🎁 {base.title} | {min_price}—{max_price} ⭐\n"
        f"└ Найдено: {len(results)}\n\n"
        f"{format_gift_list(results[:LINKS_PER_MESSAGE])}"
    )

    if len(text) > 4000:
        text = text[:3950] + "\n\n..."

    await message.answer(text, disable_web_page_preview=True, reply_markup=search_results_keyboard(results))


# ==========================
# МОНИТОРИНГ
# ==========================

async def monitor_worker():
    global monitor_running, SENT_MONITOR_SLUGS
    while monitor_running:
        try:
            if not await is_user_client_authorized() or not BASE_GIFTS:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            new_gifts = []
            for base in BASE_GIFTS[:15]:
                results = await find_market_gifts(
                    base.gift_id,
                    base.resell_min_stars or 0,
                    base.resell_min_stars + 5000 if base.resell_min_stars else 10000,
                    need=5,
                    skip_slugs=SENT_MONITOR_SLUGS,
                )
                for g in results:
                    if g.slug not in SENT_MONITOR_SLUGS:
                        new_gifts.append(g)
                        SENT_MONITOR_SLUGS.add(g.slug)

            if new_gifts and MONITOR_CHAT_ID:
                for base in BASE_GIFTS:
                    bgifts = [g for g in new_gifts if g.title == base.title]
                    if bgifts:
                        text = f"🆕 *НОВЫЕ ПОДАРКИ* | {base.title}\n\n{format_gift_list(bgifts[:LINKS_PER_MESSAGE])}"
                        try:
                            await bot.send_message(MONITOR_CHAT_ID, text, disable_web_page_preview=True)
                        except Exception as e:
                            log.error(f"Monitor send error: {e}")
                        await asyncio.sleep(random.uniform(2, 5))
                        save_sent_monitor_slugs(SENT_MONITOR_SLUGS)
        except Exception as e:
            log.error(f"Monitor error: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)


async def safe_send_message(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text, disable_web_page_preview=True)
        await asyncio.sleep(random.uniform(1, 2))
    except Exception as e:
        log.error(f"Send error: {e}")


# ==========================
# ОСНОВНЫЕ КОМАНДЫ
# ==========================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not await ensure_access(message):
        return
    if not await is_user_client_authorized():
        await message.answer("⏳ Сессия не добавлена. Добавь через админ-панель")
        return
    await ensure_models_loaded()
    await message.answer(
        "🎁 *ПАРСЕР ПОДАРКОВ*\n\n"
        "📦 ВЫБРАТЬ МОДЕЛЬ — выбери подарок и укажи цену\n"
        "🚫 ЧЁРНЫЙ СПИСОК — управление забаненными владельцами\n\n"
        "📌 Пример цены: `500 800`\n"
        "💰 Цены в ⭐",
        reply_markup=main_menu_keyboard(message.from_user.id),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎁 *ГЛАВНОЕ МЕНЮ*",
        reply_markup=main_menu_keyboard(callback.from_user.id),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("models:"))
async def show_models(callback: CallbackQuery):
    await ensure_models_loaded()
    try:
        page = int(callback.data.split(":")[1])
    except:
        page = 0
    total = max(1, (len(BASE_GIFTS) + GIFTS_PER_PAGE - 1) // GIFTS_PER_PAGE)
    await callback.message.edit_text(
        f"📦 *ВЫБЕРИ МОДЕЛЬ*\nСтраница {page+1}/{total}",
        reply_markup=models_keyboard(page),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gift:"))
async def select_gift(callback: CallbackQuery):
    gift_id = int(callback.data.split(":")[1])
    gift = BASE_GIFTS_BY_ID.get(gift_id)
    if not gift:
        await callback.answer("Модель не найдена")
        return
    USER_SELECTED_GIFT[callback.from_user.id] = gift_id
    await callback.message.edit_text(
        f"✅ *{gift.title}*\n"
        f"💰 Мин.цена: {gift.resell_min_stars or 0}⭐\n"
        f"📦 Доступно: {gift.availability_resale} шт.\n\n"
        f"📝 Отправь диапазон цен: `500 800`",
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.message()
async def price_handler(message: Message):
    user_id = message.from_user.id
    if user_id not in USER_SELECTED_GIFT:
        return

    parts = message.text.strip().replace("-", " ").split()
    if len(parts) != 2:
        await message.answer(
            "❌ Отправь два числа: мин и макс цена\nПример: `500 800`", parse_mode="Markdown"
        )
        return

    try:
        min_p, max_p = int(parts[0]), int(parts[1])
        if min_p < 0 or max_p < 0 or min_p > max_p:
            raise ValueError
    except:
        await message.answer("❌ Введи корректные числа")
        return

    gift_id = USER_SELECTED_GIFT.pop(user_id)
    base = BASE_GIFTS_BY_ID.get(gift_id)
    if not base:
        await message.answer("❌ Модель не найдена")
        return

    LAST_SEARCH_BY_USER[user_id] = {"gift_id": gift_id, "min_stars": min_p, "max_stars": max_p}

    status = await message.answer(
        f"⏳ *Ищу {base.title} от {min_p} до {max_p} ⭐...*", parse_mode="Markdown"
    )

    seen = get_seen_slugs(gift_id, min_p, max_p)
    results = await find_market_gifts(gift_id, min_p, max_p, SEARCH_RESULT_LIMIT, seen)

    LAST_RESULTS_BY_USER[user_id] = results
    remember_seen_results(gift_id, min_p, max_p, results)

    await status.delete()
    await send_search_results(message, base, results, min_p, max_p)


@dp.callback_query(F.data == "repeat_search")
async def repeat_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)
    if not search:
        await callback.answer("Нет прошлого поиска")
        return

    gift_id = search["gift_id"]
    min_p = search["min_stars"]
    max_p = search["max_stars"]
    base = BASE_GIFTS_BY_ID.get(gift_id)
    if not base:
        await callback.answer("Модель не найдена")
        return

    await callback.message.edit_text(f"⏳ *Ищу ещё...*", parse_mode="Markdown")

    seen = get_seen_slugs(gift_id, min_p, max_p)
    results = await find_market_gifts(gift_id, min_p, max_p, SEARCH_RESULT_LIMIT, seen)

    LAST_RESULTS_BY_USER[user_id] = results
    remember_seen_results(gift_id, min_p, max_p, results)

    await callback.message.delete()
    await send_search_results(callback.message, base, results, min_p, max_p)
    await callback.answer()


@dp.callback_query(F.data == "clear_seen_current")
async def clear_seen(callback: CallbackQuery):
    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)
    if search:
        clear_seen_for_query(search["gift_id"], search["min_stars"], search["max_stars"])
    await callback.answer("История сброшена")
    await callback.message.edit_text("🧹 История сброшена", reply_markup=main_menu_keyboard(user_id))


# ==========================
# ЧЁРНЫЙ СПИСОК
# ==========================

@dp.callback_query(F.data == "owners_blacklist")
async def show_blacklist(callback: CallbackQuery):
    if not OWNERS_BLACKLIST:
        await callback.message.edit_text(
            "🚫 Чёрный список пуст", reply_markup=main_menu_keyboard(callback.from_user.id)
        )
        await callback.answer()
        return
    text = "🚫 *ЧЁРНЫЙ СПИСОК ВЛАДЕЛЬЦЕВ*\n\n"
    for i, (key, label) in enumerate(OWNERS_BLACKLIST.items(), 1):
        text += f"{i}. `{label}`\n"
    await callback.message.edit_text(
        text, reply_markup=blacklist_keyboard(), parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("ban_owner:"))
async def ban_owner(callback: CallbackQuery):
    idx = int(callback.data.split(":")[1])
    results = LAST_RESULTS_BY_USER.get(callback.from_user.id, [])
    if idx >= len(results):
        await callback.answer("Результат устарел")
        return
    gift = results[idx]
    if gift.owner.key:
        OWNERS_BLACKLIST[gift.owner.key] = gift.owner.display
        save_owners_blacklist()
        await callback.answer(f"✅ Забанен {gift.owner.display}")
    else:
        await callback.answer("Невозможно забанить")


@dp.callback_query(F.data.startswith("unban_owner:"))
async def unban_owner(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    label = OWNERS_BLACKLIST.pop(key, None)
    if label:
        save_owners_blacklist()
        await callback.answer(f"✅ Разбанен {label}")
    else:
        await callback.answer("Не найден")
    await show_blacklist(callback)


@dp.callback_query(F.data == "clear_owners_blacklist")
async def clear_blacklist(callback: CallbackQuery):
    OWNERS_BLACKLIST.clear()
    save_owners_blacklist()
    await callback.answer("Чёрный список очищен")
    await show_blacklist(callback)


# ==========================
# АДМИН-ПАНЕЛЬ (ДЛЯ ДОБАВЛЕНИЯ СЕССИИ)
# ==========================

@dp.callback_query(F.data == "auth_start")
async def auth_start_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return
    await ensure_user_client_connected()
    if await is_user_client_authorized():
        await callback.message.edit_text(
            "Сессия уже добавлена. Можно пользоваться ботом.",
            reply_markup=main_menu_keyboard(callback.from_user.id),
        )
        await callback.answer()
        return
    AUTH_STATES_BY_USER[callback.from_user.id] = {"step": "phone"}
    await callback.message.edit_text(
        "Отправь номер телефона аккаунта Telegram в международном формате.\n\nПример:\n<code>+79991234567</code>",
        parse_mode="HTML",
        reply_markup=auth_cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "auth_cancel")
async def auth_cancel_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer()
        return
    AUTH_STATES_BY_USER.pop(callback.from_user.id, None)
    if await is_user_client_authorized():
        await callback.message.edit_text(
            "Авторизация отменена. Текущая сессия активна.",
            reply_markup=main_menu_keyboard(callback.from_user.id),
        )
    else:
        await callback.message.edit_text(
            "Авторизация отменена. Без сессии парсер не сможет искать подарки.",
            reply_markup=session_required_keyboard(),
        )
    await callback.answer()


@dp.message(Command("reload"))
async def reload_models(message: Message):
    if not is_admin_user(message.from_user.id if message.from_user else None):
        return
    global BASE_GIFTS, BASE_GIFTS_BY_ID
    await message.answer("Обновляю список моделей...")
    try:
        BASE_GIFTS = await load_base_gifts()
        BASE_GIFTS_BY_ID = {gift.gift_id: gift for gift in BASE_GIFTS}
        await message.answer(
            f"Готово. Загружено моделей с ресейлом: <b>{len(BASE_GIFTS)}</b>",
            reply_markup=main_menu_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"Ошибка обновления: {e}")


# ==========================
# МОНИТОРИНГ (АДМИН)
# ==========================

@dp.callback_query(F.data == "monitor_admin_panel")
async def monitor_panel(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Только для админа")
        return
    await callback.message.edit_text(
        f"📡 *МОНИТОРИНГ*\n\nОтправлено: {len(SENT_MONITOR_SLUGS)}",
        reply_markup=monitor_admin_keyboard(),
        parse_mode="Markdown",
    )
    await callback.answer()


@dp.callback_query(F.data == "monitor_start")
async def monitor_start(callback: CallbackQuery):
    global monitor_running, monitor_task
    if monitor_running:
        await callback.answer("Уже работает")
        return
    monitor_running = True
    monitor_task = asyncio.create_task(monitor_worker())
    await callback.answer("✅ Мониторинг запущен")
    await monitor_panel(callback)


@dp.callback_query(F.data == "monitor_stop")
async def monitor_stop(callback: CallbackQuery):
    global monitor_running
    monitor_running = False
    await callback.answer("⏹️ Мониторинг остановлен")
    await monitor_panel(callback)


@dp.callback_query(F.data == "monitor_reset")
async def monitor_reset(callback: CallbackQuery):
    global SENT_MONITOR_SLUGS
    SENT_MONITOR_SLUGS = set()
    save_sent_monitor_slugs(SENT_MONITOR_SLUGS)
    await callback.answer("История сброшена")
    await monitor_panel(callback)


@dp.callback_query(F.data == "monitor_status")
async def monitor_status(callback: CallbackQuery):
    await callback.answer(f"Статус: {'Активен' if monitor_running else 'Остановлен'}")


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено")


# ==========================
# ЗАПУСК
# ==========================

async def main():
    global OWNERS_BLACKLIST, SEEN_GIFTS_BY_QUERY, SENT_MONITOR_SLUGS, bot_settings, LINKS_PER_MESSAGE, DELAY_BETWEEN_BATCHES

    OWNERS_BLACKLIST = load_owners_blacklist()
    SEEN_GIFTS_BY_QUERY = load_seen_gifts()
    SENT_MONITOR_SLUGS = load_sent_monitor_slugs()
    bot_settings = load_settings()
    LINKS_PER_MESSAGE = bot_settings.get("links_per_message", 10)
    DELAY_BETWEEN_BATCHES = bot_settings.get("delay_between_batches", 30)

    log.info(
        f"Loaded: blacklist={len(OWNERS_BLACKLIST)}, seen={len(SEEN_GIFTS_BY_QUERY)}, monitor={len(SENT_MONITOR_SLUGS)}"
    )

    await ensure_user_client_connected()

    if await is_user_client_authorized():
        me = await user_client.get_me()
        log.info(f"Telethon signed in as {me.first_name}")
        try:
            await ensure_models_loaded()
            log.info(f"Models loaded: {len(BASE_GIFTS)}")
        except Exception as e:
            log.error(f"Models load error: {e}")
    else:
        log.info("Telethon not authorized")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
