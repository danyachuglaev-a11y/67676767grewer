import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

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
    AuthKeyUnregisteredError,
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

SESSION_NAME = "telethon_market_userbot"

GIFTS_PER_PAGE = 8
SEARCH_RESULT_LIMIT = 10
REQUEST_PAGE_LIMIT = 50
MAX_MARKET_PAGES = 30

SEEN_MANUAL_FILE = "seen_manual.json"
OWNER_BLACKLIST_FILE = "owner_blacklist.json"
OWNER_CACHE_FILE = "owner_cache.json"
USER_NEW_SEEN_FILE = "user_new_seen.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("gift-parser")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

BASE_GIFTS: List["BaseGift"] = []
BASE_GIFTS_BY_ID: Dict[int, "BaseGift"] = {}
USER_MODE: Dict[int, str] = {}  # "parse" или "new"
USER_SELECTED_GIFT: Dict[int, int] = {}
OWNERS_BLACKLIST: Dict[str, str] = {}
SEEN_GIFTS_BY_QUERY: Dict[str, List[str]] = {}
OWNER_CACHE: Dict[int, "OwnerInfo"] = {}
USER_NEW_SEEN: Dict[int, Set[str]] = {}  # Для режима "Новые подарки"
USER_SEARCH_HISTORY: Dict[int, Dict] = {}

session_alert_sent = False


# ============================================================
# ДАТАКЛАССЫ
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


class AuthState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


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
    save_json(SEEN_MANUAL_FILE, SEEN_GIFTS_BY_QUERY)


def load_owner_blacklist() -> Dict[str, str]:
    data = load_json(OWNER_BLACKLIST_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_owner_blacklist() -> None:
    save_json(OWNER_BLACKLIST_FILE, OWNERS_BLACKLIST)


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


def load_user_new_seen() -> Dict[int, Set[str]]:
    data = load_json(USER_NEW_SEEN_FILE, {})
    result = {}
    for k, v in data.items():
        try:
            result[int(k)] = set(v)
        except Exception:
            pass
    return result


def save_user_new_seen() -> None:
    data = {str(k): list(v) for k, v in USER_NEW_SEEN.items()}
    save_json(USER_NEW_SEEN_FILE, data)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
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
    if owner.key and owner.key in OWNERS_BLACKLIST:
        return True
    if owner.username and f"username:{owner.username.lower()}" in OWNERS_BLACKLIST:
        return True
    return False


def blacklist_owner(owner: OwnerInfo) -> None:
    if owner.key:
        OWNERS_BLACKLIST[owner.key] = owner.display
    if owner.username:
        OWNERS_BLACKLIST[f"username:{owner.username.lower()}"] = owner.display
    save_owner_blacklist()


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
    return set(SEEN_GIFTS_BY_QUERY.get(manual_seen_key(gift_id, min_price, max_price), []))


def remember_manual_results(gift_id: int, min_price: int, max_price: int, gifts: List[MarketGift]) -> None:
    key = manual_seen_key(gift_id, min_price, max_price)
    current = set(SEEN_GIFTS_BY_QUERY.get(key, []))
    for gift in gifts:
        if gift.slug not in current:
            SEEN_GIFTS_BY_QUERY.setdefault(key, []).append(gift.slug)
            current.add(gift.slug)
    save_seen_manual()


def clear_manual_seen(gift_id: int, min_price: int, max_price: int) -> None:
    key = manual_seen_key(gift_id, min_price, max_price)
    SEEN_GIFTS_BY_QUERY.pop(key, None)
    save_seen_manual()


async def ensure_user_client_connected() -> None:
    if not user_client.is_connected():
        await user_client.connect()


async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    try:
        return await user_client.is_user_authorized()
    except (AuthKeyUnregisteredError, ValueError, ConnectionError) as e:
        log.warning("Session invalid or kicked: %s", e)
        return False


# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

@dp.message(Command("add_session"))
async def add_session_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора")
        return
    if await is_user_client_authorized():
        await message.answer("✅ Сессия уже добавлена")
        return
    await state.set_state(AuthState.waiting_phone)
    await message.answer(
        "📱 ДОБАВЛЕНИЕ СЕССИИ\n\n"
        "Введите номер телефона:\n"
        "Пример: +79991234567\n\n"
        "❌ Отмена — /cancel"
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
        await message.answer("✅ Код отправлен!\n\nВведите код из Telegram:")
    except PhoneNumberInvalidError:
        await message.answer("❌ Неверный номер")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(AuthState.waiting_code)
async def auth_code(message: Message, state: FSMContext):
    code = "".join(ch for ch in message.text.strip() if ch.isdigit())
    if len(code) < 4:
        await message.answer("❌ Код слишком короткий")
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
        await message.answer("❌ Неверный код")
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
        await message.answer("❌ Неверный пароль")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def finish_auth(message: Message):
    global session_alert_sent
    session_alert_sent = False
    me = await user_client.get_me()
    name = me.first_name or me.username or str(me.id)
    await message.answer(f"✅ Сессия добавлена!\n👤 Аккаунт: {name}\n\nЗагружаю модели...")
    await load_base_gifts()
    await message.answer(f"✅ Готово!\n📦 Моделей: {len(BASE_GIFTS)}\n\nНажми /start")


# ============================================================
# ЗАГРУЗКА МОДЕЛЕЙ
# ============================================================

async def load_base_gifts() -> List[BaseGift]:
    global BASE_GIFTS, BASE_GIFTS_BY_ID
    log.info("Loading base star gifts...")
    try:
        await ensure_user_client_connected()
        if not await user_client.is_user_authorized():
            log.warning("User client not authorized")
            return []
        result = await user_client(functions.payments.GetStarGiftsRequest(hash=0))
        raw_gifts = getattr(result, "gifts", []) or []
        log.info("Raw gifts received: %s", len(raw_gifts))
        gifts: List[BaseGift] = []
        for raw in raw_gifts:
            gift_id = safe_int(get_field(raw, "id"))
            title = get_field(raw, "title")
            availability_resale = safe_int(get_field(raw, "availability_resale"))
            resell_min_stars = safe_int(get_field(raw, "resell_min_stars"))
            if not gift_id or not title or availability_resale <= 0:
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
        log.info("Loaded %s base gifts", len(gifts))
        return gifts
    except Exception as e:
        log.error(f"Error loading gifts: {e}")
        return []


async def ensure_models_loaded() -> None:
    if not BASE_GIFTS:
        await load_base_gifts()


# ============================================================
# ПОИСК ПОДАРКОВ
# ============================================================

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
            await ensure_user_client_connected()
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


async def get_resale_page(gift_id: int, offset: str = "", limit: int = REQUEST_PAGE_LIMIT, sort_by_price: bool = True):
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
        result = await get_resale_page(gift_id=gift_id, offset=offset, limit=REQUEST_PAGE_LIMIT, sort_by_price=True)
        raw_gifts = getattr(result, "gifts", []) or []
        if not raw_gifts:
            break
        for raw in raw_gifts:
            slug = get_field(raw, "slug")
            if not slug or slug in skip_slugs:
                continue
            price = extract_stars_amount(get_field(raw, "resell_amount"))
            if price < min_price or price > max_price:
                continue
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
# КЛАВИАТУРЫ
# ============================================================

def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📦 ОБЫЧНЫЙ ПАРСИНГ", callback_data="mode_parse")],
        [InlineKeyboardButton(text="🆕 НОВЫЕ ПОДАРКИ", callback_data="mode_new")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_panel")])
    rows.append([InlineKeyboardButton(text="🔄 ОБНОВИТЬ МОДЕЛИ", callback_data="reload_models")])
    rows.append([InlineKeyboardButton(text="🚫 ЧЁРНЫЙ СПИСОК", callback_data="blacklist")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_keyboard(page: int = 0, mode: str = "parse") -> InlineKeyboardMarkup:
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
        rows.append([InlineKeyboardButton(
            text=text[:64], 
            callback_data=f"gift_{mode}:{gift.gift_id}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"models_{mode}:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"models_{mode}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_results_keyboard(results: List[MarketGift], mode: str = "parse") -> InlineKeyboardMarkup:
    rows = []
    for i, gift in enumerate(results[:10]):
        if gift.owner.key and gift.owner.key != "unknown":
            rows.append([InlineKeyboardButton(
                text=f"🚫 Забанить {gift.owner.display}"[:64], 
                callback_data=f"ban_owner:{i}:{mode}"
            )])
    rows.append([InlineKeyboardButton(
        text="🔁 ПОВТОРИТЬ ПОИСК", 
        callback_data=f"repeat_search:{mode}"
    )])
    rows.append([InlineKeyboardButton(
        text="🧹 СБРОСИТЬ ИСТОРИЮ", 
        callback_data=f"clear_seen:{mode}"
    )])
    rows.append([InlineKeyboardButton(text="🚫 ЧЁРНЫЙ СПИСОК", callback_data="blacklist")])
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
# ОБРАБОТЧИКИ РЕЖИМОВ
# ============================================================

@dp.callback_query(F.data == "mode_parse")
async def mode_parse(callback: CallbackQuery):
    user_id = callback.from_user.id
    USER_MODE[user_id] = "parse"
    await callback.message.edit_text(
        "📦 ОБЫЧНЫЙ ПАРСИНГ\n\n"
        "Выбери модель подарка:",
        reply_markup=models_keyboard(0, "parse")
    )
    await callback.answer()


@dp.callback_query(F.data == "mode_new")
async def mode_new(callback: CallbackQuery):
    user_id = callback.from_user.id
    USER_MODE[user_id] = "new"
    await callback.message.edit_text(
        "🆕 НОВЫЕ ПОДАРКИ\n\n"
        "Выбери модель подарка:\n"
        "(будут показаны только новые подарки)",
        reply_markup=models_keyboard(0, "new")
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("models_parse:"))
async def models_parse_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "📦 ОБЫЧНЫЙ ПАРСИНГ\n\nВыбери модель:",
        reply_markup=models_keyboard(page, "parse")
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("models_new:"))
async def models_new_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "🆕 НОВЫЕ ПОДАРКИ\n\nВыбери модель:",
        reply_markup=models_keyboard(page, "new")
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gift_parse:"))
async def gift_parse(callback: CallbackQuery):
    user_id = callback.from_user.id
    gift_id = int(callback.data.split(":")[1])
    USER_SELECTED_GIFT[user_id] = gift_id
    USER_MODE[user_id] = "parse"
    gift = BASE_GIFTS_BY_ID.get(gift_id)
    if not gift:
        await callback.answer("Модель не найдена", show_alert=True)
        return
    await callback.message.edit_text(
        f"✅ {gift.title}\n\n"
        f"💰 Мин. цена: {gift.resell_min_stars or 0}⭐\n"
        f"📦 На маркете: {gift.availability_resale} шт.\n\n"
        f"Отправь диапазон цены:\n500 800"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gift_new:"))
async def gift_new(callback: CallbackQuery):
    user_id = callback.from_user.id
    gift_id = int(callback.data.split(":")[1])
    USER_SELECTED_GIFT[user_id] = gift_id
    USER_MODE[user_id] = "new"
    gift = BASE_GIFTS_BY_ID.get(gift_id)
    if not gift:
        await callback.answer("Модель не найдена", show_alert=True)
        return
    await callback.message.edit_text(
        f"🆕 {gift.title} (НОВЫЕ)\n\n"
        f"💰 Мин. цена: {gift.resell_min_stars or 0}⭐\n"
        f"📦 На маркете: {gift.availability_resale} шт.\n\n"
        f"Отправь диапазон цены:\n500 800\n\n"
        f"Будут показаны только новые подарки"
    )
    await callback.answer()


# ============================================================
# ОБРАБОТКА ЦЕН
# ============================================================

@dp.message()
async def price_handler(message: Message):
    user_id = message.from_user.id
    
    if user_id not in USER_SELECTED_GIFT:
        return
    
    parts = message.text.strip().replace("-", " ").split()
    if len(parts) != 2:
        await message.answer("❌ Отправь два числа: 500 800")
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
    mode = USER_MODE.get(user_id, "parse")
    base = BASE_GIFTS_BY_ID.get(gift_id)
    if not base:
        await message.answer("❌ Модель не найдена.")
        return
    
    USER_SEARCH_HISTORY[user_id] = {
        "gift_id": gift_id,
        "min_price": min_price,
        "max_price": max_price,
        "mode": mode
    }
    
    status = await message.answer(f"⏳ Ищу {base.title} от {min_price} до {max_price}⭐...")
    
    if mode == "parse":
        seen = get_manual_seen(gift_id, min_price, max_price)
        results = await find_market_gifts(
            gift_id=gift_id,
            min_price=min_price,
            max_price=max_price,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen
        )
        remember_manual_results(gift_id, min_price, max_price, results)
        title = f"🎁 {base.title}"
    else:
        if user_id not in USER_NEW_SEEN:
            USER_NEW_SEEN[user_id] = set()
        seen = USER_NEW_SEEN[user_id]
        results = await find_market_gifts(
            gift_id=gift_id,
            min_price=min_price,
            max_price=max_price,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen
        )
        for gift in results:
            USER_NEW_SEEN[user_id].add(gift.slug)
        save_user_new_seen()
        title = f"🆕 {base.title} (НОВЫЕ)"
    
    try:
        await status.delete()
    except Exception:
        pass
    
    await send_search_results(message, title, results, min_price, max_price, mode)


async def send_search_results(message: Message, title: str, results: List[MarketGift], min_price: int, max_price: int, mode: str) -> None:
    if not results:
        await message.answer(f"❌ {title}\nНичего не найдено в диапазоне {min_price}-{max_price} ⭐")
        return
    
    text = f"{title}\n💰 Диапазон: {min_price}—{max_price} ⭐\n🔎 Найдено: {len(results)}\n\n"
    
    for i, gift in enumerate(results[:10], 1):
        num = f" #{gift.num}" if gift.num else ""
        text += f"{i}. {gift.title}{num}\n💰 {gift.price} ⭐ | 👤 {gift.owner.display}\n🔗 {gift.link}\n\n"
    
    if len(text) > 4000:
        text = text[:3950] + "\n\n..."
    
    await message.answer(
        text,
        disable_web_page_preview=True,
        reply_markup=search_results_keyboard(results, mode)
    )


# ============================================================
# КНОПКИ РЕЗУЛЬТАТОВ
# ============================================================

@dp.callback_query(F.data.startswith("repeat_search:"))
async def repeat_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    mode = callback.data.split(":")[1]
    search = USER_SEARCH_HISTORY.get(user_id)
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
        await callback.message.edit_text("⏳ Ищу ещё...")
    except Exception:
        pass
    
    if mode == "parse":
        seen = get_manual_seen(gift_id, min_price, max_price)
        results = await find_market_gifts(
            gift_id=gift_id,
            min_price=min_price,
            max_price=max_price,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen
        )
        remember_manual_results(gift_id, min_price, max_price, results)
        title = f"🎁 {base.title}"
    else:
        if user_id not in USER_NEW_SEEN:
            USER_NEW_SEEN[user_id] = set()
        seen = USER_NEW_SEEN[user_id]
        results = await find_market_gifts(
            gift_id=gift_id,
            min_price=min_price,
            max_price=max_price,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen
        )
        for gift in results:
            USER_NEW_SEEN[user_id].add(gift.slug)
        save_user_new_seen()
        title = f"🆕 {base.title} (НОВЫЕ)"
    
    try:
        await callback.message.delete()
    except Exception:
        pass
    
    await send_search_results(callback.message, title, results, min_price, max_price, mode)


@dp.callback_query(F.data.startswith("clear_seen:"))
async def clear_seen(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    mode = callback.data.split(":")[1]
    search = USER_SEARCH_HISTORY.get(user_id)
    
    if mode == "parse":
        if search:
            clear_manual_seen(search["gift_id"], search["min_price"], search["max_price"])
        await callback.answer("🧹 История обычного поиска сброшена")
    else:
        if user_id in USER_NEW_SEEN:
            USER_NEW_SEEN[user_id] = set()
            save_user_new_seen()
        await callback.answer("🧹 История новых подарков сброшена")
    
    await callback.message.edit_text(
        "🧹 История поиска сброшена.",
        reply_markup=main_menu_keyboard(user_id)
    )


@dp.callback_query(F.data.startswith("ban_owner:"))
async def ban_owner_callback(callback: CallbackQuery):
    """Бан владельца - МОГУТ ВСЕ"""
    user_id = callback.from_user.id
    
    try:
        _, idx_str, mode = callback.data.split(":")
        idx = int(idx_str)
    except Exception:
        await callback.answer("Ошибка", show_alert=True)
        return
    
    search = USER_SEARCH_HISTORY.get(user_id)
    if not search:
        await callback.answer("Нет результатов", show_alert=True)
        return
    
    gift_id = search["gift_id"]
    min_price = search["min_price"]
    max_price = search["max_price"]
    
    if mode == "parse":
        seen = get_manual_seen(gift_id, min_price, max_price)
        results = await find_market_gifts(
            gift_id=gift_id,
            min_price=min_price,
            max_price=max_price,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen
        )
    else:
        seen = USER_NEW_SEEN.get(user_id, set())
        results = await find_market_gifts(
            gift_id=gift_id,
            min_price=min_price,
            max_price=max_price,
            need=SEARCH_RESULT_LIMIT,
            skip_slugs=seen
        )
    
    if idx < 0 or idx >= len(results):
        await callback.answer("Результат устарел", show_alert=True)
        return
    
    owner = results[idx].owner
    blacklist_owner(owner)
    await callback.answer(f"✅ Забанен {owner.display}")


# ============================================================
# ЧЁРНЫЙ СПИСОК
# ============================================================

@dp.callback_query(F.data == "blacklist")
async def show_blacklist(callback: CallbackQuery):
    if not OWNERS_BLACKLIST:
        text = "🚫 Чёрный список пуст"
    else:
        lines = ["🚫 ЧЁРНЫЙ СПИСОК\n"]
        for i, (key, label) in enumerate(list(OWNERS_BLACKLIST.items())[:50], 1):
            lines.append(f"{i}. {key} — {label}")
        text = "\n".join(lines)
    
    keyboard = []
    if is_admin(callback.from_user.id):
        keyboard.append([InlineKeyboardButton(text="🧹 Очистить", callback_data="blacklist_clear")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное", callback_data="menu")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@dp.callback_query(F.data == "blacklist_clear")
async def clear_blacklist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа", show_alert=True)
        return
    OWNERS_BLACKLIST.clear()
    save_owner_blacklist()
    await callback.answer("🧹 Чёрный список очищен")
    await show_blacklist(callback)


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
        f"⚙️ АДМИН-ПАНЕЛЬ\n\n"
        f"🔐 Сессия: {status}\n"
        f"👤 Аккаунт: {account}\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"🚫 В черном списке: {len(OWNERS_BLACKLIST)}",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "check_session_admin")
async def check_session_admin(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    is_auth = await is_user_client_authorized()
    if is_auth:
        try:
            me = await user_client.get_me()
            name = me.first_name or "Пользователь"
            username = f" (@{me.username})" if me.username else ""
            await callback.answer(f"✅ Сессия активна\n👤 {name}{username}", show_alert=True)
        except Exception:
            await callback.answer("✅ Сессия активна", show_alert=True)
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
        f"📊 СТАТУС БОТА\n\n"
        f"🤖 Бот: @{bot.username}\n"
        f"🔐 Сессия: {'✅ АКТИВНА' if is_auth else '❌ НЕ АКТИВНА'}\n"
    )
    if me:
        text += f"👤 Аккаунт: {me.first_name} (@{me.username})\n"
    text += (
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"🚫 В черном списке: {len(OWNERS_BLACKLIST)}"
    )
    await callback.message.edit_text(text)
    await callback.answer()


# ============================================================
# ОБЩИЕ КОМАНДЫ
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    
    if not await is_user_client_authorized():
        if is_admin(user_id):
            await message.answer(
                "⚠️ Сессия не добавлена!\n\nИспользуй /add_session",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")]
                ])
            )
        else:
            await message.answer("⚠️ Бот настраивается. Подождите.")
        return
    
    await ensure_models_loaded()
    await message.answer(
        "🎁 ПАРСЕР ПОДАРКОВ\n\n"
        "Выбери режим:\n"
        "📦 ОБЫЧНЫЙ ПАРСИНГ - все подарки на маркете\n"
        "🆕 НОВЫЕ ПОДАРКИ - только свежие поступления\n\n"
        "Банить владельцев могут ВСЕ пользователи!",
        reply_markup=main_menu_keyboard(user_id)
    )


@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(
        "🎁 ГЛАВНОЕ МЕНЮ\n\n"
        "Выбери режим работы:",
        reply_markup=main_menu_keyboard(user_id)
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
        reply_markup=main_menu_keyboard(callback.from_user.id)
    )


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    USER_SELECTED_GIFT.pop(message.from_user.id, None)
    await message.answer("❌ Действие отменено.")


# ============================================================
# МОНИТОРИНГ СЕССИИ
# ============================================================

async def check_session_and_alert():
    global session_alert_sent
    is_auth = await is_user_client_authorized()
    if not is_auth and not session_alert_sent:
        session_alert_sent = True
        try:
            await bot.send_message(
                ADMIN_ID,
                "🚨 СЕССИЯ ТЕЛЕГРАМ ПОТЕРЯНА!\n\nАккаунт разлогинился.\nВосстановите сессию командой /add_session"
            )
            log.warning("Session loss alert sent to admin")
        except Exception as e:
            log.error("Failed to send session alert: %s", e)
        return False
    elif is_auth and session_alert_sent:
        session_alert_sent = False
        log.info("Session restored, alert flag cleared")
        try:
            await bot.send_message(
                ADMIN_ID,
                "✅ СЕССИЯ ВОССТАНОВЛЕНА"
            )
        except Exception:
            pass
    return is_auth


async def session_monitor():
    while True:
        await asyncio.sleep(60)
        await check_session_and_alert()


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    global SEEN_GIFTS_BY_QUERY, OWNERS_BLACKLIST, OWNER_CACHE
    global USER_NEW_SEEN

    SEEN_GIFTS_BY_QUERY = load_seen_manual()
    OWNERS_BLACKLIST = load_owner_blacklist()
    OWNER_CACHE = load_owner_cache()
    USER_NEW_SEEN = load_user_new_seen()

    log.info(
        "Loaded: manual_queries=%s | blacklist=%s | user_new_seen=%s",
        len(SEEN_GIFTS_BY_QUERY), len(OWNERS_BLACKLIST), len(USER_NEW_SEEN)
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
    else:
        log.info("Telethon not authorized. Admin must run /add_session")
        try:
            await bot.send_message(
                ADMIN_ID,
                "🚨 СЕССИЯ НЕ АКТИВНА!\n\nИспользуйте /add_session"
            )
        except Exception as e:
            log.error("Failed to send session alert: %s", e)

    asyncio.create_task(session_monitor())

    log.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
