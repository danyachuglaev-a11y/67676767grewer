import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
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
from telethon.tl.types import PeerUser


# ============================================================
# НАСТРОЙКИ
# ============================================================

API_ID = 26259835
API_HASH = "3fa32264398920f001dd2428b42060f6"
BOT_TOKEN = "8206373294:AAEeZp8zrOquQeWrPHL7SJ4nG-mp3nfivrI"
ADMIN_ID = 8986358602

REQUIRED_CHANNEL = "@pupuhop"
REQUIRED_CHANNEL_URL = "https://t.me/pupuhop"
MONITOR_CHAT_ID = -1004306033280

SESSION_NAME = "telethon_market_userbot"

GIFTS_PER_PAGE = 8
SEARCH_RESULT_LIMIT = 10
REQUEST_PAGE_LIMIT = 50
MAX_MARKET_PAGES = 30

MONITOR_INTERVAL = 60
MONITOR_MODELS_LIMIT = 25
MONITOR_PER_MODEL_LIMIT = 8
MONITOR_SEND_LIMIT = 20
MONITOR_WARMUP_ON_START = True

SEEN_MANUAL_FILE = "seen_manual.json"
MONITOR_SEEN_FILE = "monitor_seen_slugs.json"
OWNER_BLACKLIST_FILE = "owner_blacklist.json"
OWNER_CACHE_FILE = "owner_cache.json"


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("gift-parser")


# ============================================================
# AIORGRAM / TELETHON
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


# ============================================================
# ДАННЫЕ В ПАМЯТИ
# ============================================================

@dataclass
class BaseGift:
    gift_id: int
    title: str
    stars: int
    availability_resale: int
    resell_min_stars: int
    sold_out: bool


@dataclass
class OwnerInfo:
    key: str
    label: str
    username: Optional[str] = None

    @property
    def display(self) -> str:
        return f"@{self.username}" if self.username else self.label


@dataclass
class MarketGift:
    title: str
    num: int
    slug: str
    price: int
    owner: OwnerInfo

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}"


BASE_GIFTS: List[BaseGift] = []
BASE_GIFTS_BY_ID: Dict[int, BaseGift] = {}

USER_SELECTED_GIFT: Dict[int, int] = {}
LAST_SEARCH_BY_USER: Dict[int, Dict[str, int]] = {}
LAST_RESULTS_BY_USER: Dict[int, List[MarketGift]] = {}

SEEN_MANUAL: Dict[str, List[str]] = {}
MONITOR_SEEN_SLUGS: Set[str] = set()
OWNER_BLACKLIST: Dict[str, str] = {}
OWNER_CACHE: Dict[str, Dict[str, str]] = {}

monitor_running = False
monitor_task: Optional[asyncio.Task] = None


# ============================================================
# JSON
# ============================================================

def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Save json error %s: %s", path, e)


def load_seen_manual() -> Dict[str, List[str]]:
    data = load_json(SEEN_MANUAL_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}


def save_seen_manual() -> None:
    save_json(SEEN_MANUAL_FILE, SEEN_MANUAL)


def load_monitor_seen() -> Set[str]:
    data = load_json(MONITOR_SEEN_FILE, [])
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def save_monitor_seen() -> None:
    save_json(MONITOR_SEEN_FILE, sorted(MONITOR_SEEN_SLUGS))


def load_owner_blacklist() -> Dict[str, str]:
    data = load_json(OWNER_BLACKLIST_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_owner_blacklist() -> None:
    save_json(OWNER_BLACKLIST_FILE, OWNER_BLACKLIST)


def load_owner_cache() -> Dict[str, Dict[str, str]]:
    data = load_json(OWNER_CACHE_FILE, {})
    if not isinstance(data, dict):
        return {}
    fixed: Dict[str, Dict[str, str]] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            fixed[str(k)] = {str(a): str(b) for a, b in v.items() if b is not None}
    return fixed


def save_owner_cache() -> None:
    save_json(OWNER_CACHE_FILE, OWNER_CACHE)


# ============================================================
# ОБЩИЕ ХЕЛПЕРЫ
# ============================================================

def is_admin(user_id: Optional[int]) -> bool:
    return user_id == ADMIN_ID


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except Exception:
        return default


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_owner_id(raw_id: Any) -> Optional[int]:
    if raw_id is None:
        return None
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, PeerUser):
        return raw_id.user_id
    if hasattr(raw_id, "user_id"):
        return safe_int(getattr(raw_id, "user_id"), 0) or None
    try:
        return int(raw_id)
    except Exception:
        return None


def is_owner_blacklisted(owner: OwnerInfo) -> bool:
    if owner.key and owner.key in OWNER_BLACKLIST:
        return True
    if owner.username and f"username:{owner.username.lower()}" in OWNER_BLACKLIST:
        return True
    return False


def blacklist_owner(owner: OwnerInfo) -> None:
    if owner.key:
        OWNER_BLACKLIST[owner.key] = owner.display
    if owner.username:
        OWNER_BLACKLIST[f"username:{owner.username.lower()}"] = owner.display
    save_owner_blacklist()


async def resolve_owner_info(raw_gift: Any) -> OwnerInfo:
    owner_id = extract_owner_id(get_field(raw_gift, "owner_id"))
    owner_name = get_field(raw_gift, "owner_name")

    if owner_id:
        cache_key = str(owner_id)
        cached = OWNER_CACHE.get(cache_key)
        if cached:
            username = cached.get("username") or None
            label = cached.get("label") or f"id:{owner_id}"
            key = f"username:{username.lower()}" if username else f"id:{owner_id}"
            return OwnerInfo(key=key, label=label, username=username)

        try:
            entity = await user_client.get_entity(owner_id)
            username = getattr(entity, "username", None)
            first_name = getattr(entity, "first_name", None)
            last_name = getattr(entity, "last_name", None)
            name = " ".join(x for x in [first_name, last_name] if x).strip()

            if username:
                label = f"@{username}"
                key = f"username:{username.lower()}"
            else:
                label = name or f"id:{owner_id}"
                key = f"id:{owner_id}"

            OWNER_CACHE[cache_key] = {"label": label, "username": username or ""}
            save_owner_cache()
            return OwnerInfo(key=key, label=label, username=username)
        except FloodWaitError as e:
            wait_time = int(e.seconds) + 1
            log.warning("FloodWait on owner resolve: %s sec", wait_time)
            await asyncio.sleep(wait_time)
            return OwnerInfo(key=f"id:{owner_id}", label=f"id:{owner_id}", username=None)
        except Exception as e:
            log.debug("Owner resolve failed for %s: %s", owner_id, e)
            return OwnerInfo(key=f"id:{owner_id}", label=f"id:{owner_id}", username=None)

    if owner_name:
        return OwnerInfo(key=f"name:{str(owner_name).lower()}", label=str(owner_name), username=None)

    return OwnerInfo(key="unknown", label="не указан", username=None)


def extract_stars_amount(value: Any) -> int:
    if value is None:
        return 0

    if isinstance(value, int):
        return value

    if isinstance(value, list):
        for item in value:
            amount = extract_stars_amount(item)
            if amount:
                return amount
        return 0

    amount = get_field(value, "amount")
    if amount is not None:
        return safe_int(amount)

    stars = get_field(value, "stars")
    if stars is not None:
        return safe_int(stars)

    return 0


def manual_seen_key(gift_id: int, min_price: int, max_price: int) -> str:
    return f"{gift_id}:{min_price}:{max_price}"


def get_manual_seen(gift_id: int, min_price: int, max_price: int) -> Set[str]:
    return set(SEEN_MANUAL.get(manual_seen_key(gift_id, min_price, max_price), []))


def remember_manual_results(gift_id: int, min_price: int, max_price: int, gifts: List[MarketGift]) -> None:
    key = manual_seen_key(gift_id, min_price, max_price)
    current = set(SEEN_MANUAL.get(key, []))
    for gift in gifts:
        if gift.slug not in current:
            SEEN_MANUAL.setdefault(key, []).append(gift.slug)
            current.add(gift.slug)
    save_seen_manual()


def clear_manual_seen(gift_id: int, min_price: int, max_price: int) -> None:
    key = manual_seen_key(gift_id, min_price, max_price)
    SEEN_MANUAL.pop(key, None)
    save_seen_manual()


# ============================================================
# ПОДПИСКА
# ============================================================

async def is_user_subscribed(user_id: Optional[int]) -> bool:
    if user_id is None or is_admin(user_id):
        return True

    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        status = str(getattr(member, "status", "")).lower()
        return status not in {"left", "kicked"}
    except Exception:
        return False


async def ensure_access(message: Message) -> bool:
    if await is_user_subscribed(message.from_user.id):
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
        await callback.message.edit_text("✅ Подписка подтверждена! Нажми /start")
    else:
        await callback.answer("❌ Подписка не найдена", show_alert=True)


# ============================================================
# АВТОРИЗАЦИЯ TELETHON
# ============================================================

class AuthState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


async def ensure_user_client_connected() -> None:
    if not user_client.is_connected():
        await user_client.connect()


async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    return await user_client.is_user_authorized()


@dp.message(Command("add_session"))
async def add_session_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора")
        return

    if await is_user_client_authorized():
        await message.answer("✅ Сессия уже добавлена. Бот работает.")
        return

    await state.set_state(AuthState.waiting_phone)
    await message.answer(
        "📱 *ДОБАВЛЕНИЕ СЕССИИ*\n\n"
        "Введите номер телефона в международном формате:\n"
        "Пример: `+79991234567`\n\n"
        "❌ Отмена — /cancel",
        parse_mode="Markdown"
    )


@dp.message(AuthState.waiting_phone)
async def auth_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    await ensure_user_client_connected()

    try:
        await user_client.send_code_request(phone)
        await state.update_data(phone=phone)
        await state.set_state(AuthState.waiting_code)
        await message.answer(
            "✅ Код отправлен!\n\n"
            "Введите код из Telegram. Можно через #, например: `1#2#3#4#5`",
            parse_mode="Markdown"
        )
    except PhoneNumberInvalidError:
        await message.answer("❌ Неверный номер. Попробуй ещё раз.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(AuthState.waiting_code)
async def auth_code(message: Message, state: FSMContext):
    code = "".join(ch for ch in message.text.strip() if ch.isdigit())

    if len(code) < 4:
        await message.answer("❌ Код слишком короткий.")
        return

    data = await state.get_data()
    phone = data.get("phone")

    try:
        await user_client.sign_in(phone=phone, code=code)
        await state.clear()
        await finish_auth(message)
    except SessionPasswordNeededError:
        await state.set_state(AuthState.waiting_password)
        await message.answer("🔐 Введите пароль от 2FA:")
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код.")
    except PhoneCodeExpiredError:
        await message.answer("❌ Код истёк. Начни заново: /add_session")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(AuthState.waiting_password)
async def auth_password(message: Message, state: FSMContext):
    try:
        await user_client.sign_in(password=message.text.strip())
        await state.clear()
        await finish_auth(message)
    except PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def finish_auth(message: Message):
    me = await user_client.get_me()
    await message.answer(
        f"✅ *Сессия добавлена!*\n\n"
        f"👤 Аккаунт: `{me.first_name or me.username or me.id}`\n\n"
        f"Загружаю модели...",
        parse_mode="Markdown"
    )

    await load_base_gifts()

    await message.answer(
        f"✅ Готово.\n\n"
        f"📦 Моделей с маркетом: {len(BASE_GIFTS)}\n\n"
        f"Нажми /start",
    )

    await start_monitor_if_needed()


# ============================================================
# ЗАГРУЗКА МОДЕЛЕЙ
# ============================================================

async def load_base_gifts() -> List[BaseGift]:
    global BASE_GIFTS, BASE_GIFTS_BY_ID

    log.info("Loading base star gifts...")

    result = await user_client(functions.payments.GetStarGiftsRequest(hash=0))
    raw_gifts = getattr(result, "gifts", []) or []

    log.info("Raw gifts received: %s", len(raw_gifts))

    gifts: List[BaseGift] = []
    skipped_no_title = 0
    skipped_no_resale = 0

    for raw in raw_gifts:
        gift_id = safe_int(get_field(raw, "id"))
        title = get_field(raw, "title")
        availability_resale = safe_int(get_field(raw, "availability_resale"))
        resell_min_stars = safe_int(get_field(raw, "resell_min_stars"))

        if not gift_id:
            continue

        if not title:
            skipped_no_title += 1
            continue

        if availability_resale <= 0:
            skipped_no_resale += 1
            continue

        gifts.append(BaseGift(
            gift_id=gift_id,
            title=str(title),
            stars=safe_int(get_field(raw, "stars")),
            availability_resale=availability_resale,
            resell_min_stars=resell_min_stars,
            sold_out=bool(get_field(raw, "sold_out")),
        ))

    gifts.sort(key=lambda g: (g.resell_min_stars or 999999999, g.title.lower()))

    BASE_GIFTS = gifts
    BASE_GIFTS_BY_ID = {g.gift_id: g for g in gifts}

    log.info(
        "Loaded %s base gifts | skipped_no_title=%s | skipped_no_resale=%s",
        len(gifts),
        skipped_no_title,
        skipped_no_resale,
    )

    return gifts


async def ensure_models_loaded() -> None:
    if not BASE_GIFTS:
        await load_base_gifts()


# ============================================================
# ПОИСК ПОДАРКОВ
# ============================================================

async def get_resale_page(
    gift_id: int,
    offset: str = "",
    limit: int = REQUEST_PAGE_LIMIT,
    sort_by_price: bool = True,
):
    while True:
        try:
            try:
                return await user_client(
                    functions.payments.GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset=offset,
                        limit=limit,
                        sort_by_price=sort_by_price,
                        sort_by_num=False,
                        stars_only=True,
                        for_craft=False,
                    )
                )
            except TypeError as e:
                if "stars_only" in str(e) or "for_craft" in str(e):
                    log.warning("Telethon has old GetResaleStarGiftsRequest signature, retrying without stars_only/for_craft")
                    return await user_client(
                        functions.payments.GetResaleStarGiftsRequest(
                            gift_id=gift_id,
                            offset=offset,
                            limit=limit,
                            sort_by_price=sort_by_price,
                            sort_by_num=False,
                        )
                    )
                raise
        except FloodWaitError as e:
            wait_time = int(e.seconds) + 1
            log.warning("FloodWait on resale request: %s sec", wait_time)
            await asyncio.sleep(wait_time)


async def find_market_gifts(
    gift_id: int,
    min_price: int,
    max_price: int,
    need: int = SEARCH_RESULT_LIMIT,
    skip_slugs: Optional[Set[str]] = None,
) -> List[MarketGift]:
    found: List[MarketGift] = []
    skip_slugs = skip_slugs or set()

    offset = ""
    pages = 0

    while len(found) < need and pages < MAX_MARKET_PAGES:
        pages += 1

        result = await get_resale_page(
            gift_id=gift_id,
            offset=offset,
            limit=REQUEST_PAGE_LIMIT,
            sort_by_price=True,
        )

        raw_gifts = getattr(result, "gifts", []) or []
        if not raw_gifts:
            break

        for raw in raw_gifts:
            slug = get_field(raw, "slug")
            if not slug or slug in skip_slugs:
                continue

            price = extract_stars_amount(get_field(raw, "resell_amount"))

            if price < min_price:
                continue

            if price > max_price:
                return found

            base_title = BASE_GIFTS_BY_ID[gift_id].title if gift_id in BASE_GIFTS_BY_ID else "Gift"

            owner = await resolve_owner_info(raw)
            if is_owner_blacklisted(owner):
                log.debug("Skip blacklisted owner %s for %s", owner.display, slug)
                continue

            found.append(MarketGift(
                title=str(get_field(raw, "title") or base_title),
                num=safe_int(get_field(raw, "num")),
                slug=str(slug),
                price=price,
                owner=owner,
            ))

            if len(found) >= need:
                return found

        offset = getattr(result, "next_offset", "") or ""
        if not offset:
            break

    return found


# ============================================================
# ФОРМАТИРОВАНИЕ
# ============================================================

def format_gifts(gifts: List[MarketGift]) -> str:
    lines = []

    for i, gift in enumerate(gifts, 1):
        num = f" #{gift.num}" if gift.num else ""
        lines.append(
            f"{i}. 🎁 *{gift.title}{num}*\n"
            f"💰 Цена: *{gift.price} ⭐*\n"
            f"👤 Владелец: {gift.owner.display}\n"
            f"🔗 {gift.link}"
        )

    return "\n\n".join(lines)


async def send_search_results(
    message: Message,
    base: BaseGift,
    results: List[MarketGift],
    min_price: int,
    max_price: int,
) -> None:
    if not results:
        await message.answer(
            f"❌ По модели *{base.title}* ничего не найдено в диапазоне {min_price}-{max_price} ⭐",
            parse_mode="Markdown",
        )
        return

    text = (
        f"🎁 *{base.title}*\n"
        f"💰 Диапазон: *{min_price}—{max_price} ⭐*\n"
        f"🔎 Найдено: *{len(results)}*\n\n"
        f"{format_gifts(results)}"
    )

    if len(text) > 4000:
        text = text[:3950] + "\n\n..."

    await message.answer(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=search_results_keyboard(results),
    )


# ============================================================
# КНОПКИ
# ============================================================

def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📦 ВЫБРАТЬ МОДЕЛЬ", callback_data="models:0")],
    ]

    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="📡 МОНИТОРИНГ", callback_data="monitor_panel")])
        rows.append([InlineKeyboardButton(text="⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_panel")])
        rows.append([InlineKeyboardButton(text="🔄 ОБНОВИТЬ МОДЕЛИ", callback_data="reload_models")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total = len(BASE_GIFTS)
    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE

    rows = []

    for gift in BASE_GIFTS[start:end]:
        text = f"{gift.title}"
        if gift.resell_min_stars:
            text += f" · от {gift.resell_min_stars}⭐"
        if gift.availability_resale:
            text += f" · {gift.availability_resale} шт."

        rows.append([
            InlineKeyboardButton(
                text=text[:64],
                callback_data=f"gift:{gift.gift_id}"
            )
        ])

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

    for i, gift in enumerate(results[:10]):
        if gift.owner.key and gift.owner.key != "unknown":
            rows.append([
                InlineKeyboardButton(
                    text=f"🚫 Забанить {gift.owner.display}"[:64],
                    callback_data=f"ban_owner:{i}"
                )
            ])

    rows.append([InlineKeyboardButton(text="🔁 ПОВТОРИТЬ ПОИСК", callback_data="repeat_search")])
    rows.append([InlineKeyboardButton(text="🧹 СБРОСИТЬ ИСТОРИЮ", callback_data="clear_seen")])
    rows.append([InlineKeyboardButton(text="🚫 ЧЁРНЫЙ СПИСОК", callback_data="blacklist")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def monitor_keyboard() -> InlineKeyboardMarkup:
    status = "🟢 РАБОТАЕТ" if monitor_running else "🔴 ОСТАНОВЛЕН"

    rows = [
        [InlineKeyboardButton(text=f"📊 СТАТУС: {status}", callback_data="monitor_status")],
    ]

    if monitor_running:
        rows.append([InlineKeyboardButton(text="⏹ ОСТАНОВИТЬ", callback_data="monitor_stop")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ ЗАПУСТИТЬ", callback_data="monitor_start")])

    rows.append([InlineKeyboardButton(text="🧹 СБРОСИТЬ ИСТОРИЮ МОНИТОРА", callback_data="monitor_reset")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 ПРОВЕРИТЬ СЕССИЮ", callback_data="check_session_admin")],
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")],
        [InlineKeyboardButton(text="📊 СТАТУС БОТА", callback_data="bot_status")],
        [InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")]
    ])


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return

    is_auth = await is_user_client_authorized()
    status = "✅ АКТИВНА" if is_auth else "❌ НЕ АКТИВНА"
    me = await user_client.get_me() if is_auth else None
    account = f"{me.first_name} (@{me.username})" if me else "—"

    await callback.message.edit_text(
        f"⚙️ *АДМИН-ПАНЕЛЬ*\n\n"
        f"🔐 Сессия: {status}\n"
        f"👤 Аккаунт: {account}\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"📡 Мониторинг: {'🟢 ВКЛ' if monitor_running else '🔴 ВЫКЛ'}\n"
        f"🚫 В черном списке: {len(OWNER_BLACKLIST)}\n"
        f"📤 Запомнено slug: {len(MONITOR_SEEN_SLUGS)}",
        parse_mode="Markdown",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "check_session_admin")
async def check_session_admin(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return

    if await is_user_client_authorized():
        me = await user_client.get_me()
        await callback.answer(
            f"✅ Сессия активна\n"
            f"👤 {me.first_name} (@{me.username})",
            show_alert=True
        )
    else:
        await callback.answer("❌ Сессия не активна!\nИспользуй /add_session", show_alert=True)

    await admin_panel(callback)


@dp.callback_query(F.data == "add_session_btn")
async def add_session_btn(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return

    await callback.message.answer(
        "📱 ДОБАВЛЕНИЕ СЕССИИ\n\n"
        "Введите номер телефона:\n"
        "Пример: +79991234567\n\n"
        "❌ Отмена — /cancel"
    )
    await state.set_state(AuthState.waiting_phone)
    await callback.answer()


@dp.callback_query(F.data == "bot_status")
async def bot_status_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return

    is_auth = await is_user_client_authorized()
    me = await user_client.get_me() if is_auth else None

    text = (
        f"📊 *СТАТУС БОТА*\n\n"
        f"🤖 Бот: @{bot.username}\n"
        f"🔐 Сессия: {'✅ АКТИВНА' if is_auth else '❌ НЕ АКТИВНА'}\n"
    )
    if me:
        text += f"👤 Аккаунт: {me.first_name} (@{me.username})\n"
    text += (
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"📡 Мониторинг: {'🟢 ВКЛ' if monitor_running else '🔴 ВЫКЛ'}\n"
        f"🚫 В черном списке: {len(OWNER_BLACKLIST)}\n"
        f"📤 Запомнено slug: {len(MONITOR_SEEN_SLUGS)}"
    )

    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()


# ============================================================
# МОНИТОРИНГ
# ============================================================

async def warmup_monitor_seen() -> None:
    global MONITOR_SEEN_SLUGS

    if MONITOR_SEEN_SLUGS:
        log.info("Monitor warmup skipped: already have %s slugs", len(MONITOR_SEEN_SLUGS))
        return

    log.info("Monitor warmup started...")

    count = 0

    for base in BASE_GIFTS[:MONITOR_MODELS_LIMIT]:
        result = await get_resale_page(
            gift_id=base.gift_id,
            offset="",
            limit=MONITOR_PER_MODEL_LIMIT,
            sort_by_price=True,
        )

        for raw in getattr(result, "gifts", []) or []:
            slug = get_field(raw, "slug")
            if slug:
                MONITOR_SEEN_SLUGS.add(str(slug))
                count += 1

        await asyncio.sleep(random.uniform(0.6, 1.5))

    save_monitor_seen()
    log.info("Monitor warmup complete. Saved %s slugs", count)


async def collect_new_monitor_gifts() -> List[MarketGift]:
    new_gifts: List[MarketGift] = []

    for base in BASE_GIFTS[:MONITOR_MODELS_LIMIT]:
        if len(new_gifts) >= MONITOR_SEND_LIMIT:
            break

        result = await get_resale_page(
            gift_id=base.gift_id,
            offset="",
            limit=MONITOR_PER_MODEL_LIMIT,
            sort_by_price=True,
        )

        for raw in getattr(result, "gifts", []) or []:
            slug = get_field(raw, "slug")
            if not slug:
                continue

            slug = str(slug)

            if slug in MONITOR_SEEN_SLUGS:
                continue

            owner = await resolve_owner_info(raw)
            if is_owner_blacklisted(owner):
                MONITOR_SEEN_SLUGS.add(slug)
                log.info("Monitor skip blacklisted owner %s for %s", owner.display, slug)
                continue

            gift = MarketGift(
                title=str(get_field(raw, "title") or base.title),
                num=safe_int(get_field(raw, "num")),
                slug=slug,
                price=extract_stars_amount(get_field(raw, "resell_amount")),
                owner=owner,
            )

            MONITOR_SEEN_SLUGS.add(slug)
            new_gifts.append(gift)

            if len(new_gifts) >= MONITOR_SEND_LIMIT:
                break

        await asyncio.sleep(random.uniform(0.7, 1.8))

    if new_gifts:
        save_monitor_seen()

    return new_gifts


async def send_monitor_gifts(gifts: List[MarketGift]) -> None:
    if not gifts or not MONITOR_CHAT_ID:
        return

    # Проверяем что чат существует
    try:
        await bot.get_chat(MONITOR_CHAT_ID)
    except Exception as e:
        log.error(f"Monitor chat {MONITOR_CHAT_ID} not found: {e}")
        return

    # ОТПРАВЛЯЕМ ПО ОДНОМУ
    for gift in gifts:
        num_text = f" #{gift.num}" if gift.num else ""
        owner_display = f"@{gift.owner.username}" if gift.owner.username else gift.owner.label

        msg = (
            f"🆕 *НОВЫЙ ПОДАРОК НА ПРОДАЖЕ*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🎁 *{gift.title}{num_text}*\n"
            f"💰 Цена: *{gift.price}* ⭐\n"
            f"👤 Владелец: {owner_display}\n"
            f"🔗 [Купить]({gift.link})\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"⏱ {datetime.now().strftime('%H:%M:%S')}"
        )

        try:
            await bot.send_message(
                MONITOR_CHAT_ID,
                msg,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            log.info(f"Monitor sent: {gift.slug}")
            await asyncio.sleep(1.5)
        except Exception as e:
            log.error(f"Monitor send error for {gift.slug}: {e}")


async def monitor_worker() -> None:
    global monitor_running

    log.info("Monitor worker started")

    while monitor_running:
        try:
            if not await is_user_client_authorized():
                log.warning("Monitor skipped: user client not authorized")
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            await ensure_models_loaded()

            new_gifts = await collect_new_monitor_gifts()

            if new_gifts:
                log.info("Monitor found %s new gifts", len(new_gifts))
                await send_monitor_gifts(new_gifts)
            else:
                log.info("Monitor: no new gifts")

        except FloodWaitError as e:
            wait_time = int(e.seconds) + 1
            log.warning("Monitor FloodWait: %s sec", wait_time)
            await asyncio.sleep(wait_time)
        except Exception as e:
            log.error("Monitor error: %s", e)

        await asyncio.sleep(MONITOR_INTERVAL)

    log.info("Monitor worker stopped")


async def start_monitor_if_needed() -> None:
    global monitor_running, monitor_task

    if not MONITOR_CHAT_ID:
        log.info("Monitor disabled: MONITOR_CHAT_ID is empty")
        return

    if monitor_running:
        return

    if not await is_user_client_authorized():
        log.info("Monitor not started: user client not authorized")
        return

    await ensure_models_loaded()

    if MONITOR_WARMUP_ON_START:
        await warmup_monitor_seen()

    monitor_running = True
    monitor_task = asyncio.create_task(monitor_worker())
    log.info("Monitor auto-started")


# ============================================================
# ХЕНДЛЕРЫ
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not await ensure_access(message):
        return

    if not await is_user_client_authorized():
        if is_admin(message.from_user.id):
            await message.answer(
                "⚠️ *Сессия не добавлена!*\n\n"
                "Используй `/add_session`, чтобы добавить Telegram-аккаунт.",
                parse_mode="Markdown",
            )
        else:
            await message.answer("⚠️ Бот ещё не настроен. Подождите администратора.")
        return

    await ensure_models_loaded()

    await message.answer(
        "🎁 *ПАРСЕР ПОДАРКОВ*\n\n"
        "📦 Выбери модель и введи диапазон цены.\n"
        "📡 Мониторинг сам отправляет новые подарки в группу.\n\n"
        "Пример цены: `500 800`",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎁 *ГЛАВНОЕ МЕНЮ*",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data == "reload_models")
async def reload_models_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа")
        return

    await callback.answer("Обновляю...")
    await load_base_gifts()
    await callback.message.edit_text(
        f"✅ Модели обновлены.\n\n📦 Загружено: {len(BASE_GIFTS)}",
        reply_markup=main_menu_keyboard(callback.from_user.id),
    )


@dp.message(Command("reload"))
async def reload_models_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer("🔄 Обновляю модели...")
    await load_base_gifts()
    await message.answer(f"✅ Загружено моделей: {len(BASE_GIFTS)}")


@dp.callback_query(F.data.startswith("models:"))
async def show_models(callback: CallbackQuery):
    await ensure_models_loaded()

    try:
        page = int(callback.data.split(":")[1])
    except Exception:
        page = 0

    total_pages = max(1, (len(BASE_GIFTS) + GIFTS_PER_PAGE - 1) // GIFTS_PER_PAGE)

    await callback.message.edit_text(
        f"📦 *ВЫБЕРИ МОДЕЛЬ*\n"
        f"Страница {page + 1}/{total_pages}\n\n"
        f"Моделей: {len(BASE_GIFTS)}",
        parse_mode="Markdown",
        reply_markup=models_keyboard(page),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gift:"))
async def select_gift(callback: CallbackQuery):
    gift_id = safe_int(callback.data.split(":")[1])
    gift = BASE_GIFTS_BY_ID.get(gift_id)

    if not gift:
        await callback.answer("Модель не найдена", show_alert=True)
        return

    USER_SELECTED_GIFT[callback.from_user.id] = gift_id

    await callback.message.edit_text(
        f"✅ *{gift.title}*\n\n"
        f"💰 Мин. цена: *{gift.resell_min_stars or 0}⭐*\n"
        f"📦 На маркете: *{gift.availability_resale} шт.*\n\n"
        f"Отправь диапазон цены:\n"
        f"`500 800`",
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
        await message.answer("❌ Отправь два числа: `500 800`", parse_mode="Markdown")
        return

    try:
        min_price = int(parts[0])
        max_price = int(parts[1])
        if min_price < 0 or max_price < 0 or min_price > max_price:
            raise ValueError
    except Exception:
        await message.answer("❌ Введи корректные числа.")
        return

    gift_id = USER_SELECTED_GIFT.pop(user_id)
    base = BASE_GIFTS_BY_ID.get(gift_id)

    if not base:
        await message.answer("❌ Модель не найдена.")
        return

    LAST_SEARCH_BY_USER[user_id] = {
        "gift_id": gift_id,
        "min_price": min_price,
        "max_price": max_price,
    }

    status = await message.answer(
        f"⏳ Ищу *{base.title}* от *{min_price}* до *{max_price}⭐*...",
        parse_mode="Markdown",
    )

    seen = get_manual_seen(gift_id, min_price, max_price)

    results = await find_market_gifts(
        gift_id=gift_id,
        min_price=min_price,
        max_price=max_price,
        need=SEARCH_RESULT_LIMIT,
        skip_slugs=seen,
    )

    LAST_RESULTS_BY_USER[user_id] = results
    remember_manual_results(gift_id, min_price, max_price, results)

    try:
        await status.delete()
    except Exception:
        pass

    await send_search_results(message, base, results, min_price, max_price)


@dp.callback_query(F.data == "repeat_search")
async def repeat_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)

    if not search:
        await callback.answer("Нет прошлого поиска", show_alert=True)
        return

    gift_id = search["gift_id"]
    min_price = search["min_price"]
    max_price = search["max_price"]

    base = BASE_GIFTS_BY_ID.get(gift_id)
    if not base:
        await callback.answer("Модель не найдена", show_alert=True)
        return

    await callback.answer("Ищу...")

    try:
        await callback.message.edit_text("⏳ Ищу ещё...", parse_mode="Markdown")
    except Exception:
        pass

    seen = get_manual_seen(gift_id, min_price, max_price)

    results = await find_market_gifts(
        gift_id=gift_id,
        min_price=min_price,
        max_price=max_price,
        need=SEARCH_RESULT_LIMIT,
        skip_slugs=seen,
    )

    LAST_RESULTS_BY_USER[user_id] = results
    remember_manual_results(gift_id, min_price, max_price, results)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await send_search_results(callback.message, base, results, min_price, max_price)


@dp.callback_query(F.data == "clear_seen")
async def clear_seen(callback: CallbackQuery):
    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)

    if search:
        clear_manual_seen(search["gift_id"], search["min_price"], search["max_price"])

    await callback.answer("История поиска сброшена")
    await callback.message.edit_text(
        "🧹 История текущего поиска сброшена.",
        reply_markup=main_menu_keyboard(user_id),
    )


@dp.callback_query(F.data.startswith("ban_owner:"))
async def ban_owner_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("Только для админа", show_alert=True)
        return

    try:
        idx = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Ошибка индекса", show_alert=True)
        return

    results = LAST_RESULTS_BY_USER.get(user_id, [])
    if idx < 0 or idx >= len(results):
        await callback.answer("Результат устарел", show_alert=True)
        return

    owner = results[idx].owner
    blacklist_owner(owner)
    await callback.answer(f"Забанен {owner.display}")


@dp.callback_query(F.data == "blacklist")
async def show_blacklist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return

    if not OWNER_BLACKLIST:
        text = "🚫 Чёрный список пуст"
    else:
        lines = ["🚫 *ЧЁРНЫЙ СПИСОК*\n"]
        for i, (key, label) in enumerate(list(OWNER_BLACKLIST.items())[:50], 1):
            lines.append(f"{i}. `{key}` — {label}")
        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧹 Очистить", callback_data="blacklist_clear")],
            [InlineKeyboardButton(text="🏠 Главное", callback_data="menu")],
        ])
    )
    await callback.answer()


@dp.callback_query(F.data == "blacklist_clear")
async def clear_blacklist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return

    OWNER_BLACKLIST.clear()
    save_owner_blacklist()
    await callback.answer("Чёрный список очищен")
    await show_blacklist(callback)


@dp.message(Command("ban"))
async def ban_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: `/ban @username` или `/ban 123456789`", parse_mode="Markdown")
        return

    value = parts[1].strip()
    if value.startswith("@"):
        username = value[1:].lower()
        OWNER_BLACKLIST[f"username:{username}"] = f"@{username}"
        save_owner_blacklist()
        await message.answer(f"✅ Добавлен в ЧС: @{username}")
        return

    if value.isdigit():
        OWNER_BLACKLIST[f"id:{value}"] = f"id:{value}"
        save_owner_blacklist()
        await message.answer(f"✅ Добавлен в ЧС: id:{value}")
        return

    await message.answer("❌ Нужно указать @username или числовой id")


@dp.message(Command("blacklist"))
async def blacklist_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    if not OWNER_BLACKLIST:
        await message.answer("🚫 Чёрный список пуст")
        return

    lines = ["🚫 Чёрный список:\n"]
    for i, (key, label) in enumerate(list(OWNER_BLACKLIST.items())[:50], 1):
        lines.append(f"{i}. {key} — {label}")
    await message.answer("\n".join(lines))


@dp.callback_query(F.data == "monitor_panel")
async def monitor_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа")
        return

    text = (
        f"📡 *МОНИТОРИНГ*\n\n"
        f"Статус: {'🟢 работает' if monitor_running else '🔴 остановлен'}\n"
        f"Группа: `{MONITOR_CHAT_ID}`\n"
        f"Интервал: `{MONITOR_INTERVAL}` сек\n"
        f"Моделей за цикл: `{MONITOR_MODELS_LIMIT}`\n"
        f"Запомнено slug: `{len(MONITOR_SEEN_SLUGS)}`"
    )

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=monitor_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "monitor_start")
async def monitor_start(callback: CallbackQuery):
    global monitor_running, monitor_task

    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа")
        return

    if monitor_running:
        await callback.answer("Уже работает")
        return

    if not await is_user_client_authorized():
        await callback.answer("Сессия не авторизована", show_alert=True)
        return

    await ensure_models_loaded()

    monitor_running = True
    monitor_task = asyncio.create_task(monitor_worker())

    await callback.answer("Мониторинг запущен")
    await monitor_panel(callback)


@dp.callback_query(F.data == "monitor_stop")
async def monitor_stop(callback: CallbackQuery):
    global monitor_running

    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа")
        return

    monitor_running = False

    await callback.answer("Мониторинг остановлен")
    await monitor_panel(callback)


@dp.callback_query(F.data == "monitor_status")
async def monitor_status(callback: CallbackQuery):
    await callback.answer("Работает" if monitor_running else "Остановлен")


@dp.callback_query(F.data == "monitor_reset")
async def monitor_reset(callback: CallbackQuery):
    global MONITOR_SEEN_SLUGS

    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа")
        return

    MONITOR_SEEN_SLUGS = set()
    save_monitor_seen()

    await callback.answer("История мониторинга очищена")
    await monitor_panel(callback)


@dp.message(Command("monitor_on"))
async def monitor_on_command(message: Message):
    global monitor_running, monitor_task

    if not is_admin(message.from_user.id):
        return

    if monitor_running:
        await message.answer("📡 Мониторинг уже работает.")
        return

    await ensure_models_loaded()
    monitor_running = True
    monitor_task = asyncio.create_task(monitor_worker())
    await message.answer("✅ Мониторинг запущен.")


@dp.message(Command("monitor_off"))
async def monitor_off_command(message: Message):
    global monitor_running

    if not is_admin(message.from_user.id):
        return

    monitor_running = False
    await message.answer("⏹ Мониторинг остановлен.")


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    USER_SELECTED_GIFT.pop(message.from_user.id, None)
    await message.answer("❌ Действие отменено.")


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    global SEEN_MANUAL, MONITOR_SEEN_SLUGS, OWNER_BLACKLIST, OWNER_CACHE

    SEEN_MANUAL = load_seen_manual()
    MONITOR_SEEN_SLUGS = load_monitor_seen()
    OWNER_BLACKLIST = load_owner_blacklist()
    OWNER_CACHE = load_owner_cache()

    log.info(
        "Loaded storage: manual_queries=%s | monitor_slugs=%s | blacklist=%s | owner_cache=%s",
        len(SEEN_MANUAL),
        len(MONITOR_SEEN_SLUGS),
        len(OWNER_BLACKLIST),
        len(OWNER_CACHE),
    )

    await ensure_user_client_connected()

    if await is_user_client_authorized():
        me = await user_client.get_me()
        log.info("Telethon signed in as %s | @%s | id=%s", me.first_name, me.username, me.id)

        try:
            await ensure_models_loaded()
            log.info("Models loaded: %s", len(BASE_GIFTS))
        except Exception as e:
            log.error("Models load error: %s", e)

        try:
            await start_monitor_if_needed()
        except Exception as e:
            log.error("Monitor autostart error: %s", e)
    else:
        log.info("Telethon not authorized. Admin must run /add_session")

    log.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
