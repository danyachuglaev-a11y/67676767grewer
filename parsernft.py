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
WHITELIST_FILE = "whitelist_users.json"
USER_MONITOR_SESSIONS_FILE = "user_monitor_sessions.json"

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
USER_SELECTED_GIFT: Dict[int, int] = {}
LAST_SEARCH_BY_USER: Dict[int, Dict[str, int]] = {}
LAST_RESULTS_BY_USER: Dict[int, List["MarketGift"]] = {}
OWNERS_BLACKLIST: Dict[str, str] = {}
SEEN_GIFTS_BY_QUERY: Dict[str, List[str]] = {}
OWNER_CACHE: Dict[int, "OwnerInfo"] = {}
WHITELIST_USERS: Set[int] = set()
USER_MONITOR_SESSIONS: Dict[int, "UserMonitorSession"] = {}

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


@dataclass
class UserMonitorSession:
    user_id: int
    active: bool = False
    gift_ids: List[int] = field(default_factory=list)
    min_price: int = 0
    max_price: int = 999999
    limit: int = 10
    seen_slugs: Set[str] = field(default_factory=set)
    last_results: List[MarketGift] = field(default_factory=list)
    last_message_id: Optional[int] = None


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


def load_whitelist() -> Set[int]:
    data = load_json(WHITELIST_FILE, [])
    if isinstance(data, list):
        return {int(x) for x in data if x}
    return set()


def save_whitelist() -> None:
    save_json(WHITELIST_FILE, sorted(WHITELIST_USERS))


def load_user_monitor_sessions() -> Dict[int, UserMonitorSession]:
    data = load_json(USER_MONITOR_SESSIONS_FILE, {})
    sessions = {}
    for uid_str, cfg in data.items():
        try:
            uid = int(uid_str)
            sess = UserMonitorSession(
                user_id=uid,
                active=cfg.get("active", False),
                gift_ids=cfg.get("gift_ids", []),
                min_price=cfg.get("min_price", 0),
                max_price=cfg.get("max_price", 999999),
                limit=cfg.get("limit", 10),
                seen_slugs=set(cfg.get("seen_slugs", [])),
                last_message_id=cfg.get("last_message_id")
            )
            sessions[uid] = sess
        except Exception as e:
            log.error(f"Error loading session {uid_str}: {e}")
    return sessions


def save_user_monitor_sessions() -> None:
    data = {}
    for uid, sess in USER_MONITOR_SESSIONS.items():
        data[str(uid)] = {
            "active": sess.active,
            "gift_ids": sess.gift_ids,
            "min_price": sess.min_price,
            "max_price": sess.max_price,
            "limit": sess.limit,
            "seen_slugs": list(sess.seen_slugs),
            "last_message_id": sess.last_message_id
        }
    save_json(USER_MONITOR_SESSIONS_FILE, data)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================

def is_admin(user_id: Optional[int]) -> bool:
    return user_id == ADMIN_ID


def is_user_allowed(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    if is_admin(user_id):
        return True
    return user_id in WHITELIST_USERS


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
# ПЕРСОНАЛЬНЫЙ МОНИТОРИНГ
# ============================================================

async def perform_monitor_search(user_id: int) -> None:
    """Поиск новых подарков для пользователя"""
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess or not sess.active:
        return
    
    await ensure_models_loaded()
    
    models_to_scan = sess.gift_ids if sess.gift_ids else [g.gift_id for g in BASE_GIFTS]
    new_gifts = []
    
    for gift_id in models_to_scan:
        if len(new_gifts) >= sess.limit:
            break
        
        try:
            result = await get_resale_page(
                gift_id=gift_id,
                offset="",
                limit=sess.limit * 2,
                sort_by_price=True
            )
            
            for raw in getattr(result, "gifts", []) or []:
                slug = get_field(raw, "slug")
                if not slug or slug in sess.seen_slugs:
                    continue
                
                price = extract_stars_amount(get_field(raw, "resell_amount"))
                if price < sess.min_price or price > sess.max_price:
                    sess.seen_slugs.add(slug)
                    continue
                
                owner = await resolve_owner_info(raw)
                if is_owner_blacklisted(owner):
                    sess.seen_slugs.add(slug)
                    continue
                
                gift = MarketGift(
                    title=str(get_field(raw, "title") or "Gift"),
                    num=safe_int(get_field(raw, "num")),
                    slug=slug,
                    price=price,
                    owner=owner
                )
                
                sess.seen_slugs.add(slug)
                new_gifts.append(gift)
                
                if len(new_gifts) >= sess.limit:
                    break
        
        except Exception as e:
            log.error(f"Monitor search error for user {user_id}, model {gift_id}: {e}")
        
        await asyncio.sleep(0.5)
    
    sess.last_results = new_gifts
    save_user_monitor_sessions()
    
    if new_gifts:
        await send_monitor_results(user_id, new_gifts)
    else:
        await send_no_results_message(user_id)


async def send_monitor_results(user_id: int, gifts: List[MarketGift]) -> None:
    """Отправка результатов мониторинга"""
    if not gifts:
        return
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        return
    
    text = f"🎁 НОВЫЕ ПОДАРКИ НА МАРКЕТЕ\n"
    text += f"💰 Диапазон: {sess.min_price}—{sess.max_price} ⭐\n"
    text += f"🔎 Найдено: {len(gifts)}\n\n"
    
    for i, gift in enumerate(gifts[:10], 1):
        num = f" #{gift.num}" if gift.num else ""
        text += (
            f"{i}. 🎁 {gift.title}{num}\n"
            f"💰 {gift.price} ⭐\n"
            f"👤 {gift.owner.display}\n"
            f"🔗 {gift.link}\n\n"
        )
    
    if len(text) > 4000:
        text = text[:3950] + "\n\n..."
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 ПОВТОРИТЬ ПОИСК", callback_data="monitor_repeat_search")],
        [InlineKeyboardButton(text="🧹 СБРОСИТЬ ИСТОРИЮ", callback_data="monitor_clear_seen")],
        [InlineKeyboardButton(text="📊 СТАТУС", callback_data="user_monitor_status")],
        [InlineKeyboardButton(text="⏹ ОСТАНОВИТЬ", callback_data="user_monitor_stop")]
    ])
    
    try:
        if sess.last_message_id:
            await bot.edit_message_text(
                text,
                chat_id=user_id,
                message_id=sess.last_message_id,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        else:
            msg = await bot.send_message(
                user_id,
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            sess.last_message_id = msg.message_id
    except Exception as e:
        log.warning(f"Failed to edit message for {user_id}: {e}")
        msg = await bot.send_message(
            user_id,
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        sess.last_message_id = msg.message_id
    
    save_user_monitor_sessions()


async def send_no_results_message(user_id: int) -> None:
    """Отправка сообщения, что ничего не найдено"""
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        return
    
    text = (
        f"🔍 НОВЫХ ПОДАРКОВ НЕ НАЙДЕНО\n\n"
        f"📦 Модели: {'Все' if not sess.gift_ids else f'{len(sess.gift_ids)} моделей'}\n"
        f"💰 Цена: {sess.min_price}—{sess.max_price} ⭐\n\n"
        f"Нажмите 'ПОВТОРИТЬ ПОИСК' чтобы проверить снова."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 ПОВТОРИТЬ ПОИСК", callback_data="monitor_repeat_search")],
        [InlineKeyboardButton(text="📊 СТАТУС", callback_data="user_monitor_status")],
        [InlineKeyboardButton(text="⏹ ОСТАНОВИТЬ", callback_data="user_monitor_stop")]
    ])
    
    try:
        if sess.last_message_id:
            await bot.edit_message_text(
                text,
                chat_id=user_id,
                message_id=sess.last_message_id,
                reply_markup=keyboard
            )
        else:
            msg = await bot.send_message(user_id, text, reply_markup=keyboard)
            sess.last_message_id = msg.message_id
    except Exception:
        msg = await bot.send_message(user_id, text, reply_markup=keyboard)
        sess.last_message_id = msg.message_id
    
    save_user_monitor_sessions()


# ============================================================
# КНОПКИ МОНИТОРИНГА
# ============================================================

@dp.callback_query(F.data == "monitor_repeat_search")
async def monitor_repeat_search(callback: CallbackQuery):
    """ПОВТОРИТЬ ПОИСК"""
    user_id = callback.from_user.id
    sess = USER_MONITOR_SESSIONS.get(user_id)
    
    if not sess or not sess.active:
        await callback.answer("⚠️ Мониторинг не активен", show_alert=True)
        return
    
    await callback.answer("🔄 Ищу новые подарки...")
    await perform_monitor_search(user_id)


@dp.callback_query(F.data == "monitor_clear_seen")
async def monitor_clear_seen(callback: CallbackQuery):
    """Сброс истории"""
    user_id = callback.from_user.id
    sess = USER_MONITOR_SESSIONS.get(user_id)
    
    if not sess or not sess.active:
        await callback.answer("❌ Мониторинг не запущен", show_alert=True)
        return
    
    sess.seen_slugs = set()
    save_user_monitor_sessions()
    
    await callback.answer("🧹 История сброшена")
    await perform_monitor_search(user_id)


@dp.callback_query(F.data == "user_monitor_status")
async def user_monitor_status(callback: CallbackQuery):
    """Статус мониторинга"""
    user_id = callback.from_user.id
    sess = USER_MONITOR_SESSIONS.get(user_id)
    
    if not sess or not sess.active:
        await callback.answer("📊 Мониторинг НЕ АКТИВЕН")
        return
    
    model_names = []
    if sess.gift_ids:
        for gid in sess.gift_ids[:5]:
            gift = BASE_GIFTS_BY_ID.get(gid)
            if gift:
                model_names.append(gift.title)
        if len(sess.gift_ids) > 5:
            model_names.append(f"... и ещё {len(sess.gift_ids) - 5}")
    else:
        model_names.append("Все модели")
    
    text = (
        f"📊 СТАТУС МОНИТОРИНГА\n\n"
        f"🟢 Активен: ДА\n"
        f"📦 Модели: {', '.join(model_names)}\n"
        f"💰 Диапазон: {sess.min_price}—{sess.max_price} ⭐\n"
        f"🔢 Лимит: {sess.limit}\n"
        f"📤 Найдено за сессию: {len(sess.seen_slugs)} шт.\n"
        f"📋 Последних результатов: {len(sess.last_results)}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 ПОВТОРИТЬ ПОИСК", callback_data="monitor_repeat_search")],
            [InlineKeyboardButton(text="🔙 НАЗАД", callback_data="monitor_repeat_search")]
        ])
    )
    await callback.answer()


@dp.callback_query(F.data == "user_monitor_stop")
async def user_monitor_stop(callback: CallbackQuery):
    """Остановка мониторинга"""
    user_id = callback.from_user.id
    sess = USER_MONITOR_SESSIONS.get(user_id)
    
    if not sess or not sess.active:
        await callback.answer("⚠️ Мониторинг уже остановлен")
        return
    
    sess.active = False
    sess.last_message_id = None
    save_user_monitor_sessions()
    
    await callback.answer("✅ Мониторинг остановлен")
    await callback.message.edit_text(
        "⏹ МОНИТОРИНГ ОСТАНОВЛЕН\n\n"
        "Чтобы запустить снова - используй /monitor",
        reply_markup=main_menu_keyboard(user_id)
    )


# ============================================================
# ЗАПУСК МОНИТОРИНГА
# ============================================================

@dp.message(Command("monitor"))
async def cmd_monitor(message: Message):
    """Запуск мониторинга"""
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        await message.answer("⛔ Доступ запрещён. Обратитесь к @rozuvu")
        return
    
    if not await is_user_client_authorized():
        await message.answer("⚠️ Сессия не активна. Обратитесь к админу")
        return
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if sess and sess.active:
        await message.answer("⚠️ Мониторинг уже запущен. Используй /status")
        return
    
    if not sess:
        sess = UserMonitorSession(user_id=user_id)
        USER_MONITOR_SESSIONS[user_id] = sess
    
    await show_monitor_setup(message)


async def show_monitor_setup(message: Message):
    """Показывает настройки мониторинга"""
    user_id = message.from_user.id
    sess = USER_MONITOR_SESSIONS.get(user_id)
    
    if not sess:
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 ВЫБРАТЬ МОДЕЛЬ", callback_data="monitor_select_model")],
        [InlineKeyboardButton(text="💰 УСТАНОВИТЬ ЦЕНУ", callback_data="monitor_set_price")],
        [InlineKeyboardButton(text="🔢 УСТАНОВИТЬ ЛИМИТ", callback_data="monitor_set_limit")],
        [InlineKeyboardButton(text="▶️ ЗАПУСТИТЬ МОНИТОРИНГ", callback_data="monitor_start_now")],
        [InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="menu")]
    ])
    
    text = (
        f"🎯 НАСТРОЙКА МОНИТОРИНГА\n\n"
        f"Текущие настройки:\n"
        f"📦 Модель: {'Все' if not sess.gift_ids else f'{len(sess.gift_ids)} моделей'}\n"
        f"💰 Цена: {sess.min_price}—{sess.max_price} ⭐\n"
        f"🔢 Лимит: {sess.limit} подарков\n\n"
        f"После запуска нажмите 'ПОВТОРИТЬ ПОИСК' для обновления"
    )
    
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data == "monitor_start_now")
async def monitor_start_now(callback: CallbackQuery):
    """Запуск мониторинга из настроек"""
    user_id = callback.from_user.id
    if not is_user_allowed(user_id):
        await callback.answer("⛔ Доступ запрещён")
        return
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        sess = UserMonitorSession(user_id=user_id)
        USER_MONITOR_SESSIONS[user_id] = sess
    
    if sess.active:
        await callback.answer("⚠️ Уже запущен")
        return
    
    sess.active = True
    sess.seen_slugs = set()
    save_user_monitor_sessions()
    
    await callback.answer("✅ Мониторинг запущен!")
    await perform_monitor_search(user_id)
    
    await callback.message.edit_text(
        "🟢 МОНИТОРИНГ ЗАПУЩЕН\n\n"
        f"📦 Модели: {'Все' if not sess.gift_ids else f'{len(sess.gift_ids)} моделей'}\n"
        f"💰 Цена: {sess.min_price}—{sess.max_price} ⭐\n"
        f"🔢 Лимит: {sess.limit} подарков\n\n"
        f"Результаты будут показаны выше.\n"
        f"Для обновления нажмите 'ПОВТОРИТЬ ПОИСК'",
        reply_markup=main_menu_keyboard(user_id)
    )


@dp.callback_query(F.data == "monitor_select_model")
async def monitor_select_model(callback: CallbackQuery):
    """Выбор модели для мониторинга"""
    user_id = callback.from_user.id
    if not is_user_allowed(user_id):
        await callback.answer("⛔ Доступ запрещён")
        return
    
    await ensure_models_loaded()
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        sess = UserMonitorSession(user_id=user_id)
        USER_MONITOR_SESSIONS[user_id] = sess
    
    selected = set(sess.gift_ids)
    keyboard = []
    
    for gift in BASE_GIFTS[:20]:
        check = "✅ " if gift.gift_id in selected else ""
        keyboard.append([
            InlineKeyboardButton(
                text=f"{check}{gift.title[:30]}",
                callback_data=f"monitor_toggle_model:{gift.gift_id}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(text="✅ ВСЕ МОДЕЛИ", callback_data="monitor_select_all")
    ])
    keyboard.append([
        InlineKeyboardButton(text="❌ ОЧИСТИТЬ ВСЕ", callback_data="monitor_clear_models")
    ])
    keyboard.append([
        InlineKeyboardButton(text="🔙 НАЗАД", callback_data="monitor_back_setup")
    ])
    
    await callback.message.edit_text(
        "🎯 ВЫБЕРИ МОДЕЛИ ДЛЯ МОНИТОРИНГА\n\n"
        "✅ - выбрана\n"
        "Пусто - не выбрана\n\n"
        "Если ничего не выбрано - будут все модели",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("monitor_toggle_model:"))
async def monitor_toggle_model(callback: CallbackQuery):
    """Переключение модели"""
    user_id = callback.from_user.id
    gift_id = int(callback.data.split(":")[1])
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        sess = UserMonitorSession(user_id=user_id)
        USER_MONITOR_SESSIONS[user_id] = sess
    
    if gift_id in sess.gift_ids:
        sess.gift_ids.remove(gift_id)
    else:
        sess.gift_ids.append(gift_id)
    
    save_user_monitor_sessions()
    await monitor_select_model(callback)


@dp.callback_query(F.data == "monitor_select_all")
async def monitor_select_all(callback: CallbackQuery):
    user_id = callback.from_user.id
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        sess = UserMonitorSession(user_id=user_id)
        USER_MONITOR_SESSIONS[user_id] = sess
    
    await ensure_models_loaded()
    sess.gift_ids = [g.gift_id for g in BASE_GIFTS]
    save_user_monitor_sessions()
    await callback.answer("✅ Выбраны все модели")
    await monitor_select_model(callback)


@dp.callback_query(F.data == "monitor_clear_models")
async def monitor_clear_models(callback: CallbackQuery):
    user_id = callback.from_user.id
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if sess:
        sess.gift_ids = []
        save_user_monitor_sessions()
    await callback.answer("✅ Список моделей очищен")
    await monitor_select_model(callback)


@dp.callback_query(F.data == "monitor_set_price")
async def monitor_set_price(callback: CallbackQuery):
    await callback.message.answer(
        "💰 ВВЕДИ ДИАПАЗОН ЦЕН\n\n"
        "Отправь два числа через пробел:\n"
        "Пример: 700 900\n\n"
        "Для отмены отправь /cancel"
    )
    await callback.answer()


@dp.callback_query(F.data == "monitor_set_limit")
async def monitor_set_limit(callback: CallbackQuery):
    await callback.message.answer(
        "🔢 УСТАНОВИ ЛИМИТ ПОДАРКОВ ЗА ЦИКЛ\n\n"
        "Отправь число от 1 до 50:\n"
        "Пример: 10\n\n"
        "Для отмены отправь /cancel"
    )
    await callback.answer()


@dp.callback_query(F.data == "monitor_back_setup")
async def monitor_back_setup(callback: CallbackQuery):
    await show_monitor_setup(callback.message)
    await callback.answer()


@dp.message(lambda msg: msg.text and " " in msg.text and msg.text.split()[0].isdigit())
async def handle_price_input(message: Message):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        return
    
    parts = message.text.strip().split()
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
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        sess = UserMonitorSession(user_id=user_id)
        USER_MONITOR_SESSIONS[user_id] = sess
    
    sess.min_price = min_price
    sess.max_price = max_price
    save_user_monitor_sessions()
    
    await message.answer(f"✅ Ценовой диапазон установлен: {min_price}—{max_price} ⭐")
    await show_monitor_setup(message)


@dp.message(lambda msg: msg.text and msg.text.isdigit() and 1 <= int(msg.text) <= 50)
async def handle_limit_input(message: Message):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        return
    
    limit = int(message.text)
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess:
        sess = UserMonitorSession(user_id=user_id)
        USER_MONITOR_SESSIONS[user_id] = sess
    
    sess.limit = limit
    save_user_monitor_sessions()
    
    await message.answer(f"✅ Лимит установлен: {limit} подарков")
    await show_monitor_setup(message)


# ============================================================
# ОСНОВНЫЕ ХЕНДЛЕРЫ
# ============================================================

def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📦 ВЫБРАТЬ МОДЕЛЬ", callback_data="models:0")]]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_panel")])
        rows.append([InlineKeyboardButton(text="🔄 ОБНОВИТЬ МОДЕЛИ", callback_data="reload_models")])
        rows.append([InlineKeyboardButton(text="👥 ВЫДАТЬ ДОСТУП", callback_data="grant_access_panel")])
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
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=f"gift:{gift.gift_id}")])
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
            rows.append([InlineKeyboardButton(text=f"🚫 Забанить {gift.owner.display}"[:64], callback_data=f"ban_owner:{i}")])
    rows.append([InlineKeyboardButton(text="🔁 ПОВТОРИТЬ ПОИСК", callback_data="repeat_search")])
    rows.append([InlineKeyboardButton(text="🧹 СБРОСИТЬ ИСТОРИЮ", callback_data="clear_seen")])
    rows.append([InlineKeyboardButton(text="🚫 ЧЁРНЫЙ СПИСОК", callback_data="blacklist")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 ПРОВЕРИТЬ СЕССИЮ", callback_data="check_session_admin")],
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")],
        [InlineKeyboardButton(text="📊 СТАТУС БОТА", callback_data="bot_status")],
        [InlineKeyboardButton(text="👥 ВЫДАТЬ ДОСТУП", callback_data="grant_access_panel")],
        [InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")]
    ])


def grant_access_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ВЫДАТЬ ПО ID", callback_data="grant_access")],
        [InlineKeyboardButton(text="📋 СПИСОК РАЗРЕШЁННЫХ", callback_data="list_allowed")],
        [InlineKeyboardButton(text="🔙 НАЗАД", callback_data="admin_panel")]
    ])


# ============================================================
# ОСНОВНЫЕ ХЕНДЛЕРЫ
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    
    if not is_user_allowed(user_id):
        await message.answer(
            "⛔ У вас нет доступа к этому боту.\n\n"
            "Для получения доступа обратитесь к администратору: @rozuvu"
        )
        return
    
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
        "📦 Выбери модель и введи диапазон цены.\n"
        "📡 Мониторинг работает по кнопке 'ПОВТОРИТЬ ПОИСК'.\n\n"
        "Пример цены: 500 800",
        reply_markup=main_menu_keyboard(user_id)
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        await message.answer("⛔ Доступ запрещён")
        return
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess or not sess.active:
        await message.answer("📊 Мониторинг НЕ АКТИВЕН")
        return
    
    model_names = []
    if sess.gift_ids:
        for gid in sess.gift_ids[:5]:
            gift = BASE_GIFTS_BY_ID.get(gid)
            if gift:
                model_names.append(gift.title)
        if len(sess.gift_ids) > 5:
            model_names.append(f"... и ещё {len(sess.gift_ids) - 5}")
    else:
        model_names.append("Все модели")
    
    text = (
        f"📊 СТАТУС МОНИТОРИНГА\n\n"
        f"🟢 Активен: ДА\n"
        f"📦 Модели: {', '.join(model_names)}\n"
        f"💰 Диапазон: {sess.min_price}—{sess.max_price} ⭐\n"
        f"🔢 Лимит: {sess.limit}\n"
        f"📤 Найдено за сессию: {len(sess.seen_slugs)} шт.\n"
        f"📋 Последних результатов: {len(sess.last_results)}"
    )
    
    await message.answer(text)


@dp.message(Command("stop_monitor"))
async def cmd_stop_monitor(message: Message):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        await message.answer("⛔ Доступ запрещён")
        return
    
    sess = USER_MONITOR_SESSIONS.get(user_id)
    if not sess or not sess.active:
        await message.answer("⚠️ Мониторинг не запущен")
        return
    
    sess.active = False
    sess.last_message_id = None
    save_user_monitor_sessions()
    await message.answer("✅ Мониторинг остановлен")


@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_user_allowed(user_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("🎁 ГЛАВНОЕ МЕНЮ", reply_markup=main_menu_keyboard(user_id))
    await callback.answer()


@dp.callback_query(F.data == "reload_models")
async def reload_models_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа")
        return
    await callback.answer("Обновляю...")
    await load_base_gifts()
    await callback.message.edit_text(f"✅ Модели обновлены.\n\n📦 Загружено: {len(BASE_GIFTS)}", reply_markup=main_menu_keyboard(callback.from_user.id))


@dp.callback_query(F.data.startswith("models:"))
async def show_models(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_user_allowed(user_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await ensure_models_loaded()
    try:
        page = int(callback.data.split(":")[1])
    except Exception:
        page = 0
    total_pages = max(1, (len(BASE_GIFTS) + GIFTS_PER_PAGE - 1) // GIFTS_PER_PAGE)
    await callback.message.edit_text(
        f"📦 ВЫБЕРИ МОДЕЛЬ\nСтраница {page + 1}/{total_pages}\n\nМоделей: {len(BASE_GIFTS)}",
        reply_markup=models_keyboard(page)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("gift:"))
async def select_gift(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_user_allowed(user_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    gift_id = safe_int(callback.data.split(":")[1])
    gift = BASE_GIFTS_BY_ID.get(gift_id)
    if not gift:
        await callback.answer("Модель не найдена", show_alert=True)
        return
    USER_SELECTED_GIFT[user_id] = gift_id
    await callback.message.edit_text(
        f"✅ {gift.title}\n\n"
        f"💰 Мин. цена: {gift.resell_min_stars or 0}⭐\n"
        f"📦 На маркете: {gift.availability_resale} шт.\n\n"
        f"Отправь диапазон цены:\n500 800"
    )
    await callback.answer()


@dp.message()
async def price_handler(message: Message):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        await message.answer("⛔ Доступ запрещён. Обратитесь к @rozuvu")
        return
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
    base = BASE_GIFTS_BY_ID.get(gift_id)
    if not base:
        await message.answer("❌ Модель не найдена.")
        return
    LAST_SEARCH_BY_USER[user_id] = {"gift_id": gift_id, "min_price": min_price, "max_price": max_price}
    status = await message.answer(f"⏳ Ищу {base.title} от {min_price} до {max_price}⭐...")
    seen = get_manual_seen(gift_id, min_price, max_price)
    results = await find_market_gifts(gift_id=gift_id, min_price=min_price, max_price=max_price, need=SEARCH_RESULT_LIMIT, skip_slugs=seen)
    LAST_RESULTS_BY_USER[user_id] = results
    remember_manual_results(gift_id, min_price, max_price, results)
    try:
        await status.delete()
    except Exception:
        pass
    await send_search_results(message, base, results, min_price, max_price)


async def send_search_results(message: Message, base: BaseGift, results: List[MarketGift], min_price: int, max_price: int) -> None:
    if not results:
        await message.answer(f"❌ По модели {base.title} ничего не найдено в диапазоне {min_price}-{max_price} ⭐")
        return
    
    text = f"🎁 {base.title}\n💰 Диапазон: {min_price}—{max_price} ⭐\n🔎 Найдено: {len(results)}\n\n"
    
    for i, gift in enumerate(results[:10], 1):
        num = f" #{gift.num}" if gift.num else ""
        text += f"{i}. {gift.title}{num}\n💰 {gift.price} ⭐ | 👤 {gift.owner.display}\n🔗 {gift.link}\n\n"
    
    if len(text) > 4000:
        text = text[:3950] + "\n\n..."
    
    await message.answer(
        text,
        disable_web_page_preview=True,
        reply_markup=search_results_keyboard(results)
    )


@dp.callback_query(F.data == "repeat_search")
async def repeat_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_user_allowed(user_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
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
        await callback.message.edit_text("⏳ Ищу ещё...")
    except Exception:
        pass
    seen = get_manual_seen(gift_id, min_price, max_price)
    results = await find_market_gifts(gift_id=gift_id, min_price=min_price, max_price=max_price, need=SEARCH_RESULT_LIMIT, skip_slugs=seen)
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
    if not is_user_allowed(user_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    search = LAST_SEARCH_BY_USER.get(user_id)
    if search:
        clear_manual_seen(search["gift_id"], search["min_price"], search["max_price"])
    await callback.answer("История поиска сброшена")
    await callback.message.edit_text("🧹 История текущего поиска сброшена.", reply_markup=main_menu_keyboard(user_id))


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
    if not OWNERS_BLACKLIST:
        text = "🚫 Чёрный список пуст"
    else:
        lines = ["🚫 ЧЁРНЫЙ СПИСОК\n"]
        for i, (key, label) in enumerate(list(OWNERS_BLACKLIST.items())[:50], 1):
            lines.append(f"{i}. {key} — {label}")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Очистить", callback_data="blacklist_clear")],
        [InlineKeyboardButton(text="🏠 Главное", callback_data="menu")]
    ]))
    await callback.answer()


@dp.callback_query(F.data == "blacklist_clear")
async def clear_blacklist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админа", show_alert=True)
        return
    OWNERS_BLACKLIST.clear()
    save_owner_blacklist()
    await callback.answer("Чёрный список очищен")
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
    active_monitors = sum(1 for s in USER_MONITOR_SESSIONS.values() if s.active)
    await callback.message.edit_text(
        f"⚙️ АДМИН-ПАНЕЛЬ\n\n"
        f"🔐 Сессия: {status}\n"
        f"👤 Аккаунт: {account}\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"👥 Активных мониторингов: {active_monitors}\n"
        f"🚫 В черном списке: {len(OWNERS_BLACKLIST)}\n"
        f"👥 Разрешённых юзеров: {len(WHITELIST_USERS)}",
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
            if me:
                name = me.first_name or "Пользователь"
                username = f" (@{me.username})" if me.username else ""
                await callback.answer(f"✅ Сессия активна\n👤 {name}{username}", show_alert=True)
            else:
                await callback.answer("✅ Сессия активна", show_alert=True)
        except Exception as e:
            await callback.answer(f"⚠️ Сессия активна, но ошибка: {e}", show_alert=True)
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
    active_monitors = sum(1 for s in USER_MONITOR_SESSIONS.values() if s.active)
    text = (
        f"📊 СТАТУС БОТА\n\n"
        f"🤖 Бот: @{bot.username}\n"
        f"🔐 Сессия: {'✅ АКТИВНА' if is_auth else '❌ НЕ АКТИВНА'}\n"
    )
    if me:
        text += f"👤 Аккаунт: {me.first_name} (@{me.username})\n"
    text += (
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"👥 Активных мониторингов: {active_monitors}\n"
        f"🚫 В черном списке: {len(OWNERS_BLACKLIST)}\n"
        f"👥 Разрешённых юзеров: {len(WHITELIST_USERS)}"
    )
    await callback.message.edit_text(text)
    await callback.answer()


# ============================================================
# ВЫДАЧА ПРАВ
# ============================================================

@dp.callback_query(F.data == "grant_access_panel")
async def grant_access_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    await callback.message.edit_text(
        "👥 УПРАВЛЕНИЕ ДОСТУПОМ\n\n"
        "Здесь вы можете выдать доступ к функциям бота обычным пользователям.\n"
        "Для выдачи отправьте команду:\n"
        "/grant ID_пользователя\n\n"
        "Например: /grant 123456789\n\n"
        "Пользователь сможет:\n"
        "- Искать подарки по моделям\n"
        "- Использовать мониторинг\n\n"
        "⚠️ Только для администратора.",
        reply_markup=grant_access_keyboard()
    )
    await callback.answer()


@dp.message(Command("grant"))
async def grant_access_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Использование: /grant ID_пользователя")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    WHITELIST_USERS.add(user_id)
    save_whitelist()
    await message.answer(f"✅ Пользователь {user_id} добавлен в список разрешённых.")


@dp.message(Command("revoke"))
async def revoke_access_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Использование: /revoke ID_пользователя")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    if user_id in WHITELIST_USERS:
        WHITELIST_USERS.remove(user_id)
        save_whitelist()
        await message.answer(f"✅ Доступ отозван у {user_id}")
    else:
        await message.answer(f"❌ Пользователь {user_id} не в списке разрешённых.")


@dp.callback_query(F.data == "grant_access")
async def grant_access_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    await callback.message.answer(
        "Введите ID пользователя для выдачи доступа:\n"
        "Пример: 123456789"
    )
    await callback.answer()


@dp.callback_query(F.data == "list_allowed")
async def list_allowed_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    if not WHITELIST_USERS:
        text = "👥 Список разрешённых пуст."
    else:
        text = "👥 РАЗРЕШЁННЫЕ ПОЛЬЗОВАТЕЛИ:\n\n"
        for uid in sorted(WHITELIST_USERS):
            try:
                user = await bot.get_chat(uid)
                name = user.full_name or user.username or str(uid)
                text += f"• {name} (ID: {uid})\n"
            except Exception:
                text += f"• ID: {uid}\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 НАЗАД", callback_data="grant_access_panel")]
    ]))
    await callback.answer()


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    USER_SELECTED_GIFT.pop(message.from_user.id, None)
    await message.answer("❌ Действие отменено.")


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    global SEEN_GIFTS_BY_QUERY, OWNERS_BLACKLIST, OWNER_CACHE
    global WHITELIST_USERS, USER_MONITOR_SESSIONS

    SEEN_GIFTS_BY_QUERY = load_seen_manual()
    OWNERS_BLACKLIST = load_owner_blacklist()
    OWNER_CACHE = load_owner_cache()
    WHITELIST_USERS = load_whitelist()
    USER_MONITOR_SESSIONS = load_user_monitor_sessions()

    log.info(
        "Loaded: manual_queries=%s | blacklist=%s | whitelist=%s | monitor_sessions=%s",
        len(SEEN_GIFTS_BY_QUERY), len(OWNERS_BLACKLIST),
        len(WHITELIST_USERS), len(USER_MONITOR_SESSIONS)
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
                "🚨 **СЕССИЯ НЕ АКТИВНА!**\n\nИспользуйте /add_session",
                parse_mode="Markdown"
            )
        except Exception as e:
            log.error("Failed to send initial session alert: %s", e)

    log.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
