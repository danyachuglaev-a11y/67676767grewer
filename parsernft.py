import asyncio
import html
import inspect
import json
import logging
import random
import re
import sqlite3
import time
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
from telethon.tl.types import PeerUser

# ==========================
# НАСТРОЙКИ
# ==========================

API_ID = 26259835
API_HASH = "3fa32264398920f001dd2428b42060f6"
BOT_TOKEN = "8206373294:AAEeZp8zrOquQeWrPHL7SJ4nG-mp3nfivrI"
ADMIN_ID = 8855434638

# Канал для подписки (если нужен)
REQUIRED_CHANNEL = "@pupuhop"
REQUIRED_CHANNEL_URL = "https://t.me/pupuhop"

# ID группы для мониторинга
MONITOR_CHAT_ID = -1005566054184
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
DB_FILE = "gift_parser.db"

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

bot_settings = {
    "links_per_message": 10,
    "delay_between_batches": 30,
    "skip_arabic_profiles": True,
    "max_owner_gifts": 5,
    "girls_only": False,
    "monitor_new_only": True,
}
LINKS_PER_MESSAGE = bot_settings["links_per_message"]
DELAY_BETWEEN_BATCHES = bot_settings["delay_between_batches"]
last_send_time_by_model: Dict[str, float] = {}

PAID_MESSAGES_CACHE: Dict[int, bool] = {}
OWNER_GIFTS_COUNT_CACHE: Dict[int, int] = {}
PROFILE_FILTER_CACHE: Dict[int, bool] = {}

ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
GIRL_NAME_RE = re.compile(
    r"\b(девочка|девушка|она|girl|female|woman|lady|princess|queen|baby|miss|her)\b",
    re.IGNORECASE,
)



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
        data = db_load_owners_blacklist()
        if data:
            return data
    except Exception as e:
        log.debug(f"DB blacklist load failed: {e}")
    try:
        with open(OWNERS_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = {str(k): str(v) for k, v in data.items()}
            db_save_owners_blacklist(data)
            return data
    except:
        return {}


def save_owners_blacklist():
    try:
        db_save_owners_blacklist(OWNERS_BLACKLIST)
    except Exception as e:
        log.debug(f"DB blacklist save failed: {e}")
    try:
        with open(OWNERS_BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(OWNERS_BLACKLIST, f, ensure_ascii=False, indent=2)
    except:
        pass


def load_seen_gifts() -> Dict[str, List[str]]:
    try:
        data = db_load_seen_gifts()
        if data:
            return data
    except Exception as e:
        log.debug(f"DB seen load failed: {e}")
    try:
        with open(SEEN_GIFTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
            for q, slugs in data.items():
                db_save_seen_query(q, slugs)
            return data
    except:
        return {}


def save_seen_gifts():
    try:
        for q, slugs in SEEN_GIFTS_BY_QUERY.items():
            db_save_seen_query(q, slugs)
    except Exception as e:
        log.debug(f"DB seen save failed: {e}")
    try:
        with open(SEEN_GIFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(SEEN_GIFTS_BY_QUERY, f, ensure_ascii=False, indent=2)
    except:
        pass


def load_sent_monitor_slugs() -> set:
    try:
        data = db_load_monitor_slugs()
        if data:
            return data
    except Exception as e:
        log.debug(f"DB monitor load failed: {e}")
    try:
        with open(SENT_MONITOR_SLUGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = set(data) if isinstance(data, list) else set()
            db_save_monitor_slugs(data)
            return data
    except:
        return set()


def save_sent_monitor_slugs(slugs: set):
    try:
        db_save_monitor_slugs(slugs)
    except Exception as e:
        log.debug(f"DB monitor save failed: {e}")
    try:
        with open(SENT_MONITOR_SLUGS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(slugs), f, ensure_ascii=False, indent=2)
    except:
        pass


def init_db():
    """SQLite-хранилище: устойчивее JSON и не теряет историю при рестартах."""
    with sqlite3.connect(DB_FILE) as con:
        con.execute("CREATE TABLE IF NOT EXISTS owners_blacklist (key TEXT PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS seen_gifts (query_key TEXT NOT NULL, slug TEXT NOT NULL, seen_at INTEGER NOT NULL, PRIMARY KEY(query_key, slug))")
        con.execute("CREATE TABLE IF NOT EXISTS monitor_slugs (slug TEXT PRIMARY KEY, seen_at INTEGER NOT NULL)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_seen_query ON seen_gifts(query_key)")
        con.commit()


def db_load_owners_blacklist() -> Dict[str, str]:
    init_db()
    with sqlite3.connect(DB_FILE) as con:
        return {str(k): str(v) for k, v in con.execute("SELECT key, label FROM owners_blacklist")}


def db_save_owners_blacklist(data: Dict[str, str]):
    init_db()
    with sqlite3.connect(DB_FILE) as con:
        con.execute("DELETE FROM owners_blacklist")
        con.executemany("INSERT OR REPLACE INTO owners_blacklist(key, label) VALUES(?, ?)", data.items())
        con.commit()


def db_load_seen_gifts() -> Dict[str, List[str]]:
    init_db()
    result: Dict[str, List[str]] = {}
    with sqlite3.connect(DB_FILE) as con:
        for query_key, slug in con.execute("SELECT query_key, slug FROM seen_gifts ORDER BY seen_at ASC"):
            result.setdefault(str(query_key), []).append(str(slug))
    return result


def db_save_seen_query(query_key: str, slugs: List[str]):
    init_db()
    now = int(time.time())
    with sqlite3.connect(DB_FILE) as con:
        con.executemany(
            "INSERT OR IGNORE INTO seen_gifts(query_key, slug, seen_at) VALUES(?, ?, ?)",
            [(query_key, slug, now) for slug in slugs],
        )
        con.commit()


def db_clear_seen_query(query_key: str):
    init_db()
    with sqlite3.connect(DB_FILE) as con:
        con.execute("DELETE FROM seen_gifts WHERE query_key = ?", (query_key,))
        con.commit()


def db_load_monitor_slugs() -> set:
    init_db()
    with sqlite3.connect(DB_FILE) as con:
        return {str(row[0]) for row in con.execute("SELECT slug FROM monitor_slugs")}


def db_save_monitor_slugs(slugs: set):
    init_db()
    now = int(time.time())
    with sqlite3.connect(DB_FILE) as con:
        con.executemany("INSERT OR IGNORE INTO monitor_slugs(slug, seen_at) VALUES(?, ?)", [(slug, now) for slug in slugs])
        con.commit()


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


# ==========================
# АВТОРИЗАЦИЯ TELETHON (ТОЛЬКО ДЛЯ АДМИНА)
# ==========================

class AuthState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


async def recreate_user_client():
    """Пересоздаёт Telethon-клиент без рестарта бота/хостинга."""
    global user_client, BASE_GIFTS, BASE_GIFTS_BY_ID
    try:
        if user_client.is_connected():
            await user_client.disconnect()
    except Exception:
        pass
    user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    BASE_GIFTS = []
    BASE_GIFTS_BY_ID = {}
    await user_client.connect()


async def ensure_user_client_connected():
    if not user_client.is_connected():
        await user_client.connect()


async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    return await user_client.is_user_authorized()


@dp.message(Command("relogin"))
async def relogin_command(message: Message, state: FSMContext):
    """Принудительно добавить/обновить сессию без перезапуска бота."""
    if not is_admin_user(message.from_user.id):
        await message.answer("⛔ Только для администратора")
        return
    await state.clear()
    await recreate_user_client()
    await state.set_state(AuthState.waiting_phone)
    await message.answer(
        "🔁 *ПЕРЕПОДКЛЮЧЕНИЕ СЕССИИ*\n\n"
        "Бот не перезапускается. Введите номер телефона в международном формате:\n"
        "Пример: `+79991234567`\n\n"
        "❌ Отмена — /cancel",
        parse_mode="Markdown"
    )


@dp.message(Command("add_session"))
async def add_session_command(message: Message, state: FSMContext):
    """Только админ может добавить сессию"""
    if not is_admin_user(message.from_user.id):
        await message.answer("⛔ Только для администратора")
        return
    
    if await is_user_client_authorized():
        await message.answer("✅ Сессия уже добавлена. Если хочешь заменить её без рестарта, используй /relogin")
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
            "Введите код из Telegram (можно через #, например: `1#2#3#4#5`):",
            parse_mode="Markdown"
        )
    except PhoneNumberInvalidError:
        await message.answer("❌ Неверный номер. Попробуйте ещё раз.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(AuthState.waiting_code)
async def auth_code(message: Message, state: FSMContext):
    code = message.text.strip()
    # Убираем всё кроме цифр
    code = "".join(ch for ch in code if ch.isdigit())
    
    if len(code) < 4:
        await message.answer("❌ Код слишком короткий. Попробуйте ещё раз.")
        return
    
    data = await state.get_data()
    phone = data.get("phone")
    
    try:
        await user_client.sign_in(phone=phone, code=code)
        await state.clear()
        await finish_auth_success(message)
    except SessionPasswordNeededError:
        await state.set_state(AuthState.waiting_password)
        await message.answer("🔐 Введите пароль от двухфакторной авторизации:")
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуйте ещё раз.")
    except PhoneCodeExpiredError:
        await message.answer("❌ Код истёк. Начните заново: /add_session")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(AuthState.waiting_password)
async def auth_password(message: Message, state: FSMContext):
    password = message.text.strip()
    
    try:
        await user_client.sign_in(password=password)
        await state.clear()
        await finish_auth_success(message)
    except PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль. Попробуйте ещё раз.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def finish_auth_success(message: Message):
    me = await user_client.get_me()
    name = me.first_name or me.username or str(me.id)
    
    await message.answer(
        f"✅ *Сессия успешно добавлена!*\n\n"
        f"👤 Вход выполнен как: `{name}`\n\n"
        f"Загружаю модели подарков...",
        parse_mode="Markdown"
    )
    
    await load_base_gifts()
    await start_monitor_if_needed()
    
    await message.answer(
        f"✅ *Готово!*\n\n"
        f"📦 Загружено моделей: {len(BASE_GIFTS)}\n\n"
        f"Теперь бот полностью работает.\n"
        f"Нажми /start для начала.",
        parse_mode="Markdown"
    )


# ==========================
# ЗАГРУЗКА МОДЕЛЕЙ
# ==========================

async def load_base_gifts() -> List[BaseGift]:
    """
    Загружает базовые модели подарков.

    Важно: нельзя отбрасывать модель только потому, что availability_resale пустой/0/None.
    У части подарков Telegram может не вернуть это поле, из-за чего старый код грузил 0 моделей.
    """
    global BASE_GIFTS, BASE_GIFTS_BY_ID
    log.info("Loading base star gifts...")

    result = await user_client(functions.payments.GetStarGiftsRequest(hash=0))
    raw_gifts = getattr(result, "gifts", []) or []
    log.info("Raw gifts received: %s", len(raw_gifts))

    gifts: List[BaseGift] = []
    skipped = 0

    for raw in raw_gifts:
        gift_id = safe_int(get_field(raw, "id"))
        title = get_field(raw, "title")

        if not gift_id or not title:
            skipped += 1
            continue

        availability_resale = safe_int(get_field(raw, "availability_resale"), 0)
        resell_min_stars = safe_int(get_field(raw, "resell_min_stars"), 0)
        stars = safe_int(get_field(raw, "stars"), 0)

        gifts.append(BaseGift(
            gift_id=gift_id,
            title=str(title),
            stars=stars,
            availability_resale=availability_resale,
            resell_min_stars=resell_min_stars,
            sold_out=get_field(raw, "sold_out"),
        ))

    gifts.sort(key=lambda g: (g.resell_min_stars or 999999, g.title.lower()))

    BASE_GIFTS = gifts
    BASE_GIFTS_BY_ID = {g.gift_id: g for g in gifts}

    log.info("Loaded %s base gifts, skipped %s", len(gifts), skipped)
    return gifts


async def ensure_models_loaded():
    if not BASE_GIFTS:
        await load_base_gifts()


# ==========================
# ПОИСК
# ==========================

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


def is_owner_blacklisted(key: Optional[str]) -> bool:
    return key in OWNERS_BLACKLIST if key else False


def get_seen_slugs(gift_id: int, min_stars: int, max_stars: int) -> set:
    key = f"{gift_id}:{min_stars}:{max_stars}"
    return set(SEEN_GIFTS_BY_QUERY.get(key, []))


def remember_seen_results(gift_id: int, min_stars: int, max_stars: int, results: List[MarketGift]):
    key = f"{gift_id}:{min_stars}:{max_stars}"
    old = set(SEEN_GIFTS_BY_QUERY.get(key, []))
    fresh = []
    for gift in results:
        if gift.slug not in old:
            SEEN_GIFTS_BY_QUERY.setdefault(key, []).append(gift.slug)
            fresh.append(gift.slug)
    if fresh:
        db_save_seen_query(key, fresh)
        save_seen_gifts()


def clear_seen_for_query(gift_id: int, min_stars: int, max_stars: int):
    key = f"{gift_id}:{min_stars}:{max_stars}"
    SEEN_GIFTS_BY_QUERY.pop(key, None)
    db_clear_seen_query(key)
    save_seen_gifts()


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


def extract_user_id(raw_id: Any) -> Optional[int]:
    """Извлекает числовой ID из PeerUser или прямого числа"""
    if raw_id is None:
        return None
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, PeerUser):
        return raw_id.user_id
    if hasattr(raw_id, 'user_id'):
        return raw_id.user_id
    try:
        return int(raw_id)
    except:
        return None


async def has_paid_messages_enabled(user_id: int) -> bool:
    """Проверяет, включена ли у пользователя опция 'писать за звезды'"""
    if user_id in PAID_MESSAGES_CACHE:
        return PAID_MESSAGES_CACHE[user_id]
    
    try:
        full_user = await user_client(functions.users.GetFullUserRequest(id=user_id))
        result = False
        if hasattr(full_user, 'user') and hasattr(full_user.user, 'require_stars_to_message'):
            result = getattr(full_user.user, 'require_stars_to_message', False)
        PAID_MESSAGES_CACHE[user_id] = result
        if result:
            log.debug(f"User {user_id} has paid messages enabled")
        return result
    except Exception as e:
        log.debug(f"Check paid messages for user {user_id} failed: {e}")
        PAID_MESSAGES_CACHE[user_id] = False
        return False



def text_has_arabic(text: Optional[str]) -> bool:
    return bool(text and ARABIC_RE.search(str(text)))


def looks_like_girl_profile(entity: Any, full_user: Any = None) -> bool:
    """Telegram не отдаёт пол пользователя, поэтому это только эвристика по имени/био."""
    parts = [
        getattr(entity, "first_name", "") or "",
        getattr(entity, "last_name", "") or "",
        getattr(entity, "username", "") or "",
    ]
    about = getattr(getattr(full_user, "full_user", None), "about", "") if full_user else ""
    if about:
        parts.append(about)
    text = " ".join(parts).lower()
    if GIRL_NAME_RE.search(text):
        return True
    first_name = (getattr(entity, "first_name", "") or "").strip().lower()
    # Очень мягкая эвристика для рус/укр/англ имён: часто женские имена заканчиваются на -а/-я/-ia/-na.
    return bool(first_name and (first_name.endswith(("а", "я", "ia", "na", "ie", "elle"))))


async def get_owner_profile_gifts_count(user_id: int, max_allowed: int) -> int:
    """Возвращает количество подарков в профиле, считая только до max_allowed + 1."""
    if user_id in OWNER_GIFTS_COUNT_CACHE:
        return OWNER_GIFTS_COUNT_CACHE[user_id]
    try:
        peer = await user_client.get_input_entity(user_id)
        req_cls = getattr(functions.payments, "GetSavedStarGiftsRequest", None)
        if not req_cls:
            # На старой версии Telethon метода может не быть — не режем владельца вслепую.
            OWNER_GIFTS_COUNT_CACHE[user_id] = 0
            return 0
        kwargs = dict(peer=peer, offset="", limit=max_allowed + 1)
        sig = inspect.signature(req_cls)
        for optional_flag in ("exclude_unsaved", "exclude_saved", "exclude_unlimited", "exclude_limited", "exclude_unique", "sort_by_value"):
            if optional_flag in sig.parameters:
                kwargs[optional_flag] = False
        result = await user_client(req_cls(**kwargs))
        gifts = getattr(result, "gifts", []) or []
        count = len(gifts)
        OWNER_GIFTS_COUNT_CACHE[user_id] = count
        return count
    except Exception as e:
        log.debug(f"Owner gifts count check failed for {user_id}: {e}")
        OWNER_GIFTS_COUNT_CACHE[user_id] = 0
        return 0


async def owner_passes_profile_filters(owner_id: Optional[int], owner: OwnerInfo) -> bool:
    if not owner_id:
        return True
    if owner_id in PROFILE_FILTER_CACHE:
        return PROFILE_FILTER_CACHE[owner_id]

    skip_arabic = bool(bot_settings.get("skip_arabic_profiles", True))
    max_owner_gifts = safe_int(bot_settings.get("max_owner_gifts", 5), 5)
    girls_only = bool(bot_settings.get("girls_only", False))

    try:
        entity = await user_client.get_entity(owner_id)
        full_user = None
        try:
            full_user = await user_client(functions.users.GetFullUserRequest(id=owner_id))
        except Exception:
            full_user = None

        profile_text = " ".join(filter(None, [
            getattr(entity, "first_name", "") or "",
            getattr(entity, "last_name", "") or "",
            getattr(entity, "username", "") or "",
            owner.label or "",
            getattr(getattr(full_user, "full_user", None), "about", "") if full_user else "",
        ]))

        if skip_arabic and text_has_arabic(profile_text):
            PROFILE_FILTER_CACHE[owner_id] = False
            return False

        if max_owner_gifts >= 0:
            gifts_count = await get_owner_profile_gifts_count(owner_id, max_owner_gifts)
            if gifts_count > max_owner_gifts:
                PROFILE_FILTER_CACHE[owner_id] = False
                return False

        if girls_only and not looks_like_girl_profile(entity, full_user):
            PROFILE_FILTER_CACHE[owner_id] = False
            return False

        PROFILE_FILTER_CACHE[owner_id] = True
        return True
    except Exception as e:
        log.debug(f"Profile filter failed for {owner_id}: {e}")
        PROFILE_FILTER_CACHE[owner_id] = True
        return True


async def throttle_model_request(model_key: str):
    delay = safe_int(bot_settings.get("delay_between_batches", DELAY_BETWEEN_BATCHES), DELAY_BETWEEN_BATCHES)
    if delay <= 0:
        return
    now = time.time()
    last = last_send_time_by_model.get(model_key, 0)
    wait_for = delay - (now - last)
    if wait_for > 0:
        await asyncio.sleep(wait_for)
    last_send_time_by_model[model_key] = time.time()


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

    model_key = str(gift_id)
    while len(found) < need and pages < MAX_MARKET_PAGES:
        pages += 1
        await throttle_model_request(model_key)
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
            
            owner_id_raw = get_field(raw, "owner_id")
            owner_id = extract_user_id(owner_id_raw)
            if owner_id and await has_paid_messages_enabled(owner_id):
                log.debug(f"Skipping {slug} - owner requires stars to message")
                continue

            if not await owner_passes_profile_filters(owner_id, owner):
                log.debug(f"Skipping {slug} - owner did not pass profile filters")
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
# КЛАВИАТУРЫ
# ==========================

def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎁 Искать подарки", callback_data="models:0")],
        [InlineKeyboardButton(text="⚙️ Фильтры", callback_data="filters_panel")],
        [InlineKeyboardButton(text="🚫 Чёрный список", callback_data="owners_blacklist")],
    ]
    if MONITOR_CHAT_ID and is_admin_user(user_id):
        rows.insert(2, [InlineKeyboardButton(text="📡 Новые листинги", callback_data="monitor_admin_panel")])
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



def bool_badge(value: bool) -> str:
    return "✅" if value else "❌"


def filters_keyboard() -> InlineKeyboardMarkup:
    max_gifts = bot_settings.get("max_owner_gifts", 5)
    rows = [
        [InlineKeyboardButton(text=f"{bool_badge(bot_settings.get('skip_arabic_profiles', True))} Пропускать арабские символы", callback_data="toggle_filter:skip_arabic_profiles")],
        [InlineKeyboardButton(text=f"🎁 Подарков в профиле: до {max_gifts}", callback_data="noop")],
        [
            InlineKeyboardButton(text="➖", callback_data="max_gifts:dec"),
            InlineKeyboardButton(text="➕", callback_data="max_gifts:inc"),
        ],
        [InlineKeyboardButton(text=f"{bool_badge(bot_settings.get('girls_only', False))} Искать только девочек", callback_data="toggle_filter:girls_only")],
        [InlineKeyboardButton(text=f"{bool_badge(bot_settings.get('monitor_new_only', True))} Мониторить только новые", callback_data="toggle_filter:monitor_new_only")],
        [InlineKeyboardButton(text="🏠 Главное", callback_data="menu")],
    ]
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



@dp.callback_query(F.data == "filters_panel")
async def filters_panel(callback: CallbackQuery):
    text = (
        "⚙️ *ФИЛЬТРЫ ПОИСКА*\n\n"
        "• арабские символы — пропускает владельцев, если в имени/юзернейме/био есть арабская письменность;\n"
        "• подарков в профиле — пропускает владельцев, у которых больше лимита;\n"
        "• только девочки — примерная эвристика по имени/био, Telegram не отдаёт пол напрямую;\n"
        "• только новые — мониторинг отправляет только ещё не отправленные листинги."
    )
    await callback.message.edit_text(text, reply_markup=filters_keyboard(), parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_filter:"))
async def toggle_filter(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    if key not in {"skip_arabic_profiles", "girls_only", "monitor_new_only"}:
        await callback.answer("Неизвестный фильтр")
        return
    bot_settings[key] = not bool(bot_settings.get(key, False))
    save_settings(bot_settings)
    PROFILE_FILTER_CACHE.clear()
    await filters_panel(callback)


@dp.callback_query(F.data.startswith("max_gifts:"))
async def change_max_gifts(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    current = safe_int(bot_settings.get("max_owner_gifts", 5), 5)
    if action == "inc":
        current = min(50, current + 1)
    elif action == "dec":
        current = max(0, current - 1)
    bot_settings["max_owner_gifts"] = current
    save_settings(bot_settings)
    OWNER_GIFTS_COUNT_CACHE.clear()
    PROFILE_FILTER_CACHE.clear()
    await filters_panel(callback)


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


# ==========================
# МОНИТОРИНГ
# ==========================

async def start_monitor_if_needed():
    global monitor_running, monitor_task
    if MONITOR_CHAT_ID and not monitor_running and await is_user_client_authorized():
        monitor_running = True
        monitor_task = asyncio.create_task(monitor_worker())


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
                    skip_slugs=SENT_MONITOR_SLUGS if bot_settings.get("monitor_new_only", True) else set(),
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


# ==========================
# ОСНОВНЫЕ КОМАНДЫ
# ==========================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not await ensure_access(message):
        return
    
    # Проверяем сессию
    if not await is_user_client_authorized():
        if is_admin_user(message.from_user.id):
            await message.answer(
                "⚠️ *Сессия не добавлена!*\n\n"
                "Используй команду `/add_session` чтобы добавить сессию Telegram.\n\n"
                "После добавления сессии бот начнёт работать.",
                parse_mode="Markdown"
            )
        else:
            await message.answer(
                "⚠️ *Бот ещё не настроен*\n\n"
                "Пожалуйста, подождите. Администратор настраивает бота.\n"
                "Скоро он начнёт работать.",
                parse_mode="Markdown"
            )
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
        await callback.answer("Нет прошлого поиска", show_alert=True)
        return

    gift_id = search["gift_id"]
    min_p = search["min_stars"]
    max_p = search["max_stars"]
    base = BASE_GIFTS_BY_ID.get(gift_id)
    if not base:
        await callback.answer("Модель не найдена", show_alert=True)
        return

    # Отвечаем на callback сразу, чтобы он не истёк
    await callback.answer("🔍 Ищу...")
    
    await callback.message.edit_text(f"⏳ *Ищу ещё...*", parse_mode="Markdown")

    seen = get_seen_slugs(gift_id, min_p, max_p)
    results = await find_market_gifts(gift_id, min_p, max_p, SEARCH_RESULT_LIMIT, seen)

    LAST_RESULTS_BY_USER[user_id] = results
    remember_seen_results(gift_id, min_p, max_p, results)

    await callback.message.delete()
    await send_search_results(callback.message, base, results, min_p, max_p)


@dp.callback_query(F.data == "clear_seen_current")
async def clear_seen(callback: CallbackQuery):
    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)
    if search:
        clear_seen_for_query(search["gift_id"], search["min_stars"], search["max_stars"])
    
    await callback.answer("🧹 История сброшена")
    await callback.message.edit_text("🧹 История сброшена", reply_markup=main_menu_keyboard(user_id))


@dp.message(Command("reload"))
async def reload_models(message: Message):
    if not is_admin_user(message.from_user.id):
        return
    await message.answer("Обновляю список моделей...")
    await load_base_gifts()
    await message.answer(f"✅ Загружено моделей: {len(BASE_GIFTS)}")


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено")


# ==========================
# ЗАПУСК
# ==========================

async def main():
    global OWNERS_BLACKLIST, SEEN_GIFTS_BY_QUERY, SENT_MONITOR_SLUGS, bot_settings, LINKS_PER_MESSAGE, DELAY_BETWEEN_BATCHES, monitor_running, monitor_task

    OWNERS_BLACKLIST = load_owners_blacklist()
    SEEN_GIFTS_BY_QUERY = load_seen_gifts()
    SENT_MONITOR_SLUGS = load_sent_monitor_slugs()
    bot_settings = load_settings()
    LINKS_PER_MESSAGE = bot_settings.get("links_per_message", 10)
    DELAY_BETWEEN_BATCHES = bot_settings.get("delay_between_batches", 30)

    log.info(f"Loaded: blacklist={len(OWNERS_BLACKLIST)}, seen={len(SEEN_GIFTS_BY_QUERY)}, monitor={len(SENT_MONITOR_SLUGS)}")

    await ensure_user_client_connected()

    if await is_user_client_authorized():
        me = await user_client.get_me()
        log.info(f"Telethon signed in as {me.first_name}")
        try:
            await ensure_models_loaded()
            log.info(f"Models loaded: {len(BASE_GIFTS)}")
        except Exception as e:
            log.error(f"Models load error: {e}")
        await start_monitor_if_needed()
    else:
        log.info("Telethon not authorized. Admin must run /add_session")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
