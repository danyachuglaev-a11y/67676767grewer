import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
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
BOT_TOKEN = "8206373294:AAFEHHDDZOdX9i00voiMI55g68gjKjXFZeE"
ADMIN_ID = 8855434638

REQUIRED_CHANNEL = "@pupuhop"
REQUIRED_CHANNEL_URL = "https://t.me/pupuhop"
MONITOR_CHAT_ID = -1005340426610
MONITOR_INTERVAL = 60

SESSION_NAME = "telethon_market_userbot"
SESSIONS_FILE = "sessions.json"
ACTIVE_SESSION_FILE = "active_session.json"
GIFTS_PER_PAGE = 8
SEARCH_RESULT_LIMIT = 10
REQUEST_PAGE_LIMIT = 50
MAX_MARKET_PAGES = 30

OWNERS_BLACKLIST_FILE = "owners_blacklist.json"
SEEN_GIFTS_FILE = "seen_gifts.json"
SENT_MONITOR_SLUGS_FILE = "sent_monitor_slugs.json"
SETTINGS_FILE = "bot_settings.json"
MARKET_STATE_FILE = "market_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("nft-gift-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальные переменные
BASE_GIFTS: List["BaseGift"] = []
BASE_GIFTS_BY_ID: Dict[int, "BaseGift"] = {}
USER_SELECTED_GIFT: Dict[int, int] = {}
LAST_RESULTS_BY_USER: Dict[int, List["MarketGift"]] = {}
LAST_SEARCH_BY_USER: Dict[int, Dict[str, int]] = {}
OWNERS_BLACKLIST: Dict[str, str] = {}
SEEN_GIFTS_BY_QUERY: Dict[str, List[str]] = {}
SENT_MONITOR_SLUGS: set = set()
market_snapshots: Dict[int, Dict[str, Any]] = {}
PAID_MESSAGES_CACHE: Dict[int, bool] = {}
OWNER_CACHE: Dict[int, "OwnerInfo"] = {}
PHONE_CODE_HASH: Dict[str, str] = {}  # session_name -> phone_code_hash

monitor_running = False
monitor_task = None

# Текущий клиент Telethon
user_client: Optional[TelegramClient] = None
active_session_name: Optional[str] = None
sessions: Dict[str, Dict[str, Any]] = {}

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

class AuthState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_session_name = State()

# ==========================
# УПРАВЛЕНИЕ СЕССИЯМИ
# ==========================

def load_sessions() -> Dict[str, Dict[str, Any]]:
    """Загружает список сессий из файла"""
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_sessions():
    """Сохраняет список сессий в файл"""
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
    except:
        pass

def load_active_session() -> Optional[str]:
    """Загружает имя активной сессии"""
    try:
        with open(ACTIVE_SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("active_session")
    except:
        return None

def save_active_session(name: Optional[str]):
    """Сохраняет имя активной сессии"""
    try:
        with open(ACTIVE_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"active_session": name}, f, ensure_ascii=False, indent=2)
    except:
        pass

def get_session_file(session_name: str) -> str:
    """Возвращает имя файла сессии"""
    return f"{session_name}.session"

async def create_telethon_client(session_name: str) -> TelegramClient:
    """Создает клиента Telethon для указанной сессии"""
    session_file = get_session_file(session_name)
    return TelegramClient(session_file, API_ID, API_HASH)

async def switch_session(session_name: str) -> bool:
    """Переключает на указанную сессию"""
    global user_client, active_session_name
    
    if session_name not in sessions:
        log.error(f"Session {session_name} not found")
        return False
    
    # Отключаем текущий клиент
    if user_client and user_client.is_connected():
        await user_client.disconnect()
    
    # Создаем новый клиент
    user_client = await create_telethon_client(session_name)
    await user_client.connect()
    
    if await user_client.is_user_authorized():
        active_session_name = session_name
        save_active_session(session_name)
        
        # Обновляем статус в сессиях
        for name in sessions:
            sessions[name]["active"] = (name == session_name)
        save_sessions()
        
        log.info(f"Switched to session: {session_name}")
        return True
    else:
        log.warning(f"Session {session_name} is not authorized")
        return False

async def init_session_manager():
    """Инициализирует менеджер сессий"""
    global user_client, active_session_name, sessions
    
    sessions = load_sessions()
    active_session_name = load_active_session()
    
    # Если есть активная сессия и она существует
    if active_session_name and active_session_name in sessions:
        user_client = await create_telethon_client(active_session_name)
        await user_client.connect()
        if await user_client.is_user_authorized():
            log.info(f"Session {active_session_name} loaded")
            sessions[active_session_name]["active"] = True
            save_sessions()
            return True
        else:
            log.warning(f"Session {active_session_name} is not authorized")
            active_session_name = None
            save_active_session(None)
    
    # Пытаемся найти первую авторизованную сессию
    for name in sessions:
        if sessions[name].get("authorized", False):
            client = await create_telethon_client(name)
            await client.connect()
            if await client.is_user_authorized():
                user_client = client
                active_session_name = name
                save_active_session(name)
                sessions[name]["active"] = True
                save_sessions()
                log.info(f"Session {name} loaded")
                return True
            await client.disconnect()
    
    # Если ничего не найдено - создаем дефолтную сессию
    user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await user_client.connect()
    log.info("Using default session")
    return False

async def add_new_session(session_name: str, phone: str) -> bool:
    """Добавляет новую сессию в список"""
    if session_name in sessions:
        return False
    
    sessions[session_name] = {
        "phone": phone,
        "created": datetime.now().isoformat(),
        "authorized": False,
        "active": False
    }
    save_sessions()
    return True

async def delete_session(session_name: str) -> bool:
    """Удаляет сессию"""
    if session_name not in sessions:
        return False
    
    # Удаляем файл сессии
    session_file = get_session_file(session_name)
    if os.path.exists(session_file):
        try:
            os.remove(session_file)
        except:
            pass
    
    # Удаляем из списка
    del sessions[session_name]
    save_sessions()
    
    # Если удалили активную - сбрасываем
    if active_session_name == session_name:
        save_active_session(None)
    
    return True

# ==========================
# ЗАГРУЗКА/СОХРАНЕНИЕ (остальное)
# ==========================

def load_owners_blacklist() -> Dict[str, str]:
    try:
        with open(OWNERS_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            return {str(k): str(v) for k, v in json.load(f).items()}
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

def load_market_state() -> Dict[int, Dict[str, Any]]:
    try:
        with open(MARKET_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            result = {}
            for key, value in data.items():
                result[int(key)] = {
                    "slugs": value.get("slugs", []),
                    "num_map": value.get("num_map", {}),
                    "timestamp": value.get("timestamp", 0)
                }
            return result
    except:
        return {}

def save_market_state():
    try:
        data = {}
        for gift_id, snap in market_snapshots.items():
            data[str(gift_id)] = {
                "slugs": snap["slugs"],
                "num_map": snap["num_map"],
                "timestamp": snap["timestamp"]
            }
        with open(MARKET_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

# ==========================
# ВСПОМОГАТЕЛЬНЫЕ
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

def is_admin_user(user_id: Optional[int]) -> bool:
    return user_id == ADMIN_ID

def extract_user_id(raw_id: Any) -> Optional[int]:
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

async def ensure_user_client_connected():
    if not user_client or not user_client.is_connected():
        if user_client:
            await user_client.connect()
        else:
            await init_session_manager()

async def is_user_client_authorized() -> bool:
    await ensure_user_client_connected()
    if user_client:
        return await user_client.is_user_authorized()
    return False

# ==========================
# ПРОВЕРКА ПОДПИСКИ
# ==========================

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
# АВТОРИЗАЦИЯ СЕССИИ (ИСПРАВЛЕННАЯ)
# ==========================

@dp.callback_query(F.data == "add_session_btn")
async def add_session_btn(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    await callback.message.answer(
        "📱 ДОБАВЛЕНИЕ СЕССИИ\n\n"
        "Введите название для сессии (например: main, work, bot):"
    )
    await state.set_state(AuthState.waiting_session_name)
    await callback.answer()

@dp.message(AuthState.waiting_session_name)
async def session_name_input(message: Message, state: FSMContext):
    session_name = message.text.strip().replace(" ", "_")
    if not session_name:
        await message.answer("❌ Название не может быть пустым")
        return
    
    if session_name in sessions:
        await message.answer(f"❌ Сессия с именем '{session_name}' уже существует\n\nВведите другое имя:")
        return
    
    await state.update_data(session_name=session_name)
    await state.set_state(AuthState.waiting_phone)
    await message.answer(
        f"📱 Сессия: {session_name}\n\n"
        "Введите номер телефона:\n"
        "Пример: +79991234567\n\n"
        "❌ Отмена — /cancel"
    )

@dp.message(AuthState.waiting_phone)
async def auth_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith("+"):
        phone = "+" + phone
    
    data = await state.get_data()
    session_name = data.get("session_name")
    
    # Создаем временный клиент для авторизации
    session_file = get_session_file(session_name)
    temp_client = TelegramClient(session_file, API_ID, API_HASH)
    await temp_client.connect()
    
    try:
        # Сохраняем phone_code_hash
        result = await temp_client.send_code_request(phone)
        phone_code_hash = result.phone_code_hash
        PHONE_CODE_HASH[session_name] = phone_code_hash
        
        await state.update_data(phone=phone, session_name=session_name, phone_code_hash=phone_code_hash)
        await state.set_state(AuthState.waiting_code)
        await message.answer(
            f"✅ Код отправлен для сессии {session_name}!\n\n"
            "Введите код из Telegram:\n"
            "Если не приходит — напишите 'resend'"
        )
    except PhoneNumberInvalidError:
        await message.answer("❌ Неверный номер")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        await temp_client.disconnect()

@dp.message(AuthState.waiting_code)
async def auth_code(message: Message, state: FSMContext):
    data = await state.get_data()
    session_name = data.get("session_name")
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash") or PHONE_CODE_HASH.get(session_name)
    
    # Если пользователь хочет повторно отправить код
    if message.text.strip().lower() in ["resend", "повторно", "ещё"]:
        temp_client = TelegramClient(get_session_file(session_name), API_ID, API_HASH)
        await temp_client.connect()
        try:
            # Обновляем phone_code_hash при повторной отправке
            result = await temp_client.send_code_request(phone)
            new_hash = result.phone_code_hash
            PHONE_CODE_HASH[session_name] = new_hash
            await state.update_data(phone_code_hash=new_hash)
            await message.answer(f"✅ Код повторно отправлен для {session_name}!")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
        finally:
            await temp_client.disconnect()
        return
    
    code = "".join(ch for ch in message.text.strip() if ch.isdigit())
    if len(code) < 4:
        await message.answer("❌ Код слишком короткий\n\nЕсли не приходит, напишите 'resend'")
        return
    
    temp_client = TelegramClient(get_session_file(session_name), API_ID, API_HASH)
    await temp_client.connect()
    
    try:
        # Передаем phone_code_hash
        await temp_client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        
        # Сессия успешно создана
        me = await temp_client.get_me()
        name = me.first_name or me.username or str(me.id)
        
        # Добавляем в список сессий
        await add_new_session(session_name, phone)
        sessions[session_name]["authorized"] = True
        sessions[session_name]["account_name"] = name
        save_sessions()
        
        await state.clear()
        await message.answer(
            f"✅ Сессия '{session_name}' создана!\n"
            f"👤 Аккаунт: {name}\n\n"
            f"Теперь активируйте её в админ-панели."
        )
        
    except SessionPasswordNeededError:
        await state.set_state(AuthState.waiting_password)
        await state.update_data(phone_code_hash=phone_code_hash)
        await message.answer("🔐 Введите пароль от двухфакторной авторизации:")
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код\n\nЕсли не приходит, напишите 'resend'")
    except PhoneCodeExpiredError:
        await message.answer("❌ Код истёк. Напишите 'resend' для повторной отправки")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        await temp_client.disconnect()

@dp.message(AuthState.waiting_password)
async def auth_password(message: Message, state: FSMContext):
    data = await state.get_data()
    session_name = data.get("session_name")
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash") or PHONE_CODE_HASH.get(session_name)
    password = message.text.strip()
    
    temp_client = TelegramClient(get_session_file(session_name), API_ID, API_HASH)
    await temp_client.connect()
    
    try:
        # Для 2FA используем sign_in с паролем
        await temp_client.sign_in(password=password)
        me = await temp_client.get_me()
        name = me.first_name or me.username or str(me.id)
        
        await add_new_session(session_name, phone)
        sessions[session_name]["authorized"] = True
        sessions[session_name]["account_name"] = name
        save_sessions()
        
        await state.clear()
        await message.answer(
            f"✅ Сессия '{session_name}' создана!\n"
            f"👤 Аккаунт: {name}\n\n"
            f"Теперь активируйте её в админ-панели."
        )
    except PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        await temp_client.disconnect()

# ==========================
# УПРАВЛЕНИЕ СЕССИЯМИ В АДМИН-ПАНЕЛИ
# ==========================

def sessions_list_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура со списком сессий"""
    rows = []
    for name, data in sessions.items():
        status = "✅" if data.get("active", False) else "⏸️"
        auth = "🔐" if data.get("authorized", False) else "❌"
        label = data.get("account_name", name)
        rows.append([
            InlineKeyboardButton(
                text=f"{status} {auth} {label}",
                callback_data=f"session_info:{name}"
            )
        ])
    
    rows.append([InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def session_actions_keyboard(session_name: str) -> InlineKeyboardMarkup:
    """Клавиатура действий для сессии"""
    is_active = sessions.get(session_name, {}).get("active", False)
    is_authorized = sessions.get(session_name, {}).get("authorized", False)
    
    rows = []
    if is_authorized and not is_active:
        rows.append([InlineKeyboardButton(text="✅ АКТИВИРОВАТЬ", callback_data=f"session_activate:{session_name}")])
    if is_active:
        rows.append([InlineKeyboardButton(text="⏹️ ДЕАКТИВИРОВАТЬ", callback_data=f"session_deactivate:{session_name}")])
    if not is_active:
        rows.append([InlineKeyboardButton(text="🗑️ УДАЛИТЬ", callback_data=f"session_delete:{session_name}")])
    rows.append([InlineKeyboardButton(text="🔙 НАЗАД К СПИСКУ", callback_data="sessions_list")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "sessions_list")
async def sessions_list(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    if not sessions:
        await callback.message.edit_text(
            "📂 УПРАВЛЕНИЕ СЕССИЯМИ\n\n"
            "❌ Нет сохранённых сессий\n\n"
            "Нажми ➕ ДОБАВИТЬ СЕССИЮ для создания",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")],
                [InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")]
            ])
        )
        await callback.answer()
        return
    
    text = "📂 *УПРАВЛЕНИЕ СЕССИЯМИ*\n\n"
    for name, data in sessions.items():
        status = "🟢 АКТИВНА" if data.get("active", False) else "⏸️ НЕ АКТИВНА"
        auth = "🔐 Авторизована" if data.get("authorized", False) else "❌ Не авторизована"
        account = data.get("account_name", "—")
        phone = data.get("phone", "—")
        text += f"┌ {name}\n├ {status}\n├ {auth}\n├ 👤 {account}\n└ 📱 {phone}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=sessions_list_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("session_info:"))
async def session_info(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    session_name = callback.data.split(":", 1)[1]
    data = sessions.get(session_name)
    if not data:
        await callback.answer("Сессия не найдена")
        return
    
    status = "🟢 АКТИВНА" if data.get("active", False) else "⏸️ НЕ АКТИВНА"
    auth = "🔐 Авторизована" if data.get("authorized", False) else "❌ Не авторизована"
    account = data.get("account_name", "—")
    phone = data.get("phone", "—")
    created = data.get("created", "—")
    
    text = (
        f"📂 СЕССИЯ: {session_name}\n\n"
        f"📊 Статус: {status}\n"
        f"🔐 Авторизация: {auth}\n"
        f"👤 Аккаунт: {account}\n"
        f"📱 Телефон: {phone}\n"
        f"📅 Создана: {created[:19] if created != '—' else '—'}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=session_actions_keyboard(session_name)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("session_activate:"))
async def session_activate(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    session_name = callback.data.split(":", 1)[1]
    
    if session_name not in sessions:
        await callback.answer("Сессия не найдена")
        return
    
    if not sessions[session_name].get("authorized", False):
        await callback.answer("❌ Сессия не авторизована!", show_alert=True)
        return
    
    await callback.answer(f"🔄 Активирую {session_name}...")
    
    success = await switch_session(session_name)
    if success:
        await callback.answer(f"✅ Сессия {session_name} активирована!", show_alert=True)
        await load_base_gifts()
    else:
        await callback.answer(f"❌ Не удалось активировать {session_name}", show_alert=True)
    
    await session_info(callback)

@dp.callback_query(F.data.startswith("session_deactivate:"))
async def session_deactivate(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    session_name = callback.data.split(":", 1)[1]
    
    if session_name not in sessions:
        await callback.answer("Сессия не найдена")
        return
    
    sessions[session_name]["active"] = False
    save_sessions()
    
    global active_session_name
    if active_session_name == session_name:
        active_session_name = None
        save_active_session(None)
    
    await callback.answer(f"⏸️ Сессия {session_name} деактивирована")
    await session_info(callback)

@dp.callback_query(F.data.startswith("session_delete:"))
async def session_delete(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    session_name = callback.data.split(":", 1)[1]
    
    # Подтверждение удаления
    await callback.message.edit_text(
        f"⚠️ УДАЛИТЬ СЕССИЮ?\n\n"
        f"Сессия: {session_name}\n"
        f"Аккаунт: {sessions.get(session_name, {}).get('account_name', '—')}\n\n"
        f"Файл сессии будет удалён без возможности восстановления.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑️ ДА, УДАЛИТЬ", callback_data=f"session_confirm_delete:{session_name}")],
            [InlineKeyboardButton(text="❌ ОТМЕНА", callback_data=f"session_info:{session_name}")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("session_confirm_delete:"))
async def session_confirm_delete(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    session_name = callback.data.split(":", 1)[1]
    
    await delete_session(session_name)
    await callback.answer(f"🗑️ Сессия {session_name} удалена")
    await sessions_list(callback)

# ==========================
# АДМИН-ПАНЕЛЬ (ОБНОВЛЕННАЯ)
# ==========================

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    status = "✅ АКТИВНА" if await is_user_client_authorized() else "❌ НЕ АКТИВНА"
    session_name = active_session_name or "Нет"
    sessions_count = len(sessions)
    
    await callback.message.edit_text(
        f"⚙️ АДМИН-ПАНЕЛЬ\n\n"
        f"🔐 Текущая сессия: {session_name}\n"
        f"📊 Статус: {status}\n"
        f"📂 Всего сессий: {sessions_count}\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"📡 Мониторинг: {'🟢 ВКЛ' if monitor_running else '🔴 ВЫКЛ'}\n"
        f"🚫 В черном списке: {len(OWNERS_BLACKLIST)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📂 УПРАВЛЕНИЕ СЕССИЯМИ", callback_data="sessions_list")],
            [InlineKeyboardButton(text="🔄 ОБНОВИТЬ МОДЕЛИ", callback_data="reload_models")],
            [InlineKeyboardButton(text="📊 СТАТУС БОТА", callback_data="bot_status")],
            [InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "reload_models")
async def reload_models_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    await callback.answer("🔄 Обновляю модели...")
    await load_base_gifts()
    await callback.message.answer(f"✅ Загружено моделей: {len(BASE_GIFTS)}")
    await admin_panel(callback)

@dp.callback_query(F.data == "bot_status")
async def bot_status_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔ Только для админа")
        return
    
    session_name = active_session_name or "Нет"
    is_auth = await is_user_client_authorized()
    
    status_text = (
        f"📊 СТАТУС БОТА\n\n"
        f"🤖 Бот: @{bot.username}\n"
        f"📂 Активная сессия: {session_name}\n"
        f"🔐 Сессия: {'✅ АКТИВНА' if is_auth else '❌ НЕ АКТИВНА'}\n"
        f"📦 Моделей: {len(BASE_GIFTS)}\n"
        f"📡 Мониторинг: {'🟢 ВКЛ' if monitor_running else '🔴 ВЫКЛ'}\n"
        f"🚫 В черном списке: {len(OWNERS_BLACKLIST)}\n"
        f"📤 Отправлено монитором: {len(SENT_MONITOR_SLUGS)}\n"
        f"📸 Снимков рынка: {len(market_snapshots)}"
    )
    await callback.message.edit_text(status_text)
    await callback.answer()

# ==========================
# ЗАГРУЗКА МОДЕЛЕЙ
# ==========================

async def load_base_gifts() -> List[BaseGift]:
    global BASE_GIFTS, BASE_GIFTS_BY_ID
    log.info("Loading base star gifts...")
    try:
        if not user_client or not user_client.is_connected():
            await ensure_user_client_connected()
        
        if not await user_client.is_user_authorized():
            log.warning("User client not authorized")
            return []
        
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
        BASE_GIFTS = gifts
        BASE_GIFTS_BY_ID = {g.gift_id: g for g in gifts}
        log.info("Loaded %s base gifts", len(gifts))
        return gifts
    except Exception as e:
        log.error(f"Error loading gifts: {e}")
        return []

async def ensure_models_loaded():
    if not BASE_GIFTS:
        await load_base_gifts()

# ==========================
# РАБОТА С ВЛАДЕЛЬЦАМИ
# ==========================

async def resolve_owner_info(raw_gift: Any) -> OwnerInfo:
    # Проверяем кеш
    owner_id = get_field(raw_gift, "owner_id")
    user_id = extract_user_id(owner_id) if owner_id else None
    
    if user_id and user_id in OWNER_CACHE:
        return OWNER_CACHE[user_id]
    
    # Сначала проверяем, есть ли username напрямую в ответе
    direct_username = get_field(raw_gift, "owner_username") or get_field(raw_gift, "username")
    if direct_username:
        username = str(direct_username).lstrip("@")
        if username:
            info = OwnerInfo(
                key=f"username:{username.lower()}",
                label=f"@{username}",
                username=username,
                link=f"https://t.me/{username}"
            )
            if user_id:
                OWNER_CACHE[user_id] = info
            return info
    
    # Если нет - пытаемся получить через owner_id
    if user_id and user_client:
        try:
            await ensure_user_client_connected()
            entity = await user_client.get_entity(user_id)
            username = getattr(entity, "username", None)
            if username:
                username = str(username).lstrip("@")
                info = OwnerInfo(
                    key=f"username:{username.lower()}",
                    label=f"@{username}",
                    username=username,
                    link=f"https://t.me/{username}"
                )
                OWNER_CACHE[user_id] = info
                return info
            name = getattr(entity, "first_name", "") or getattr(entity, "title", "") or str(user_id)
            info = OwnerInfo(
                key=f"id:{user_id}",
                label=name[:30],
                username=None,
                link=None
            )
            OWNER_CACHE[user_id] = info
            return info
        except FloodWaitError as e:
            log.warning(f"FloodWait {e.seconds}s for user {user_id}, waiting...")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            log.debug(f"Could not get entity for {user_id}: {e}")
    
    owner_name = get_field(raw_gift, "owner_name")
    if owner_name:
        info = OwnerInfo(
            key=f"name:{owner_name}",
            label=str(owner_name)[:30],
            username=None,
            link=None
        )
        if user_id:
            OWNER_CACHE[user_id] = info
        return info
    
    info = OwnerInfo(key=None, label="не указан", username=None, link=None)
    if user_id:
        OWNER_CACHE[user_id] = info
    return info

async def has_paid_messages_enabled(user_id: int) -> bool:
    if user_id in PAID_MESSAGES_CACHE:
        return PAID_MESSAGES_CACHE[user_id]
    try:
        await ensure_user_client_connected()
        full_user = await user_client(functions.users.GetFullUserRequest(id=user_id))
        result = getattr(full_user.user, 'require_stars_to_message', False)
        PAID_MESSAGES_CACHE[user_id] = result
        return result
    except Exception:
        PAID_MESSAGES_CACHE[user_id] = False
        return False

# ==========================
# ПОИСК НА РЫНКЕ
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
            await ensure_user_client_connected()
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
            if price < min_stars or price > max_stars:
                continue
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

async def get_full_market_state(
    gift_id: int,
    min_stars: int,
    max_stars: int,
    max_pages: int = 8
) -> Tuple[List[MarketGift], Dict[str, int]]:
    results = []
    num_map = {}
    offset = ""
    pages = 0
    
    while pages < max_pages:
        pages += 1
        try:
            await ensure_user_client_connected()
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
            log.info(f"FloodWait {e.seconds}s for gift {gift_id}")
            await asyncio.sleep(e.seconds + 1)
            continue
        except Exception as e:
            log.error(f"Scan error: {e}")
            break
        
        gifts = getattr(result, "gifts", [])
        if not gifts:
            break
        
        for raw in gifts:
            slug = get_field(raw, "slug")
            if not slug:
                continue
            price = extract_stars_amount(get_field(raw, "resell_amount"))
            if price < min_stars or price > max_stars:
                continue
            owner = await resolve_owner_info(raw)
            if is_owner_blacklisted(owner.key):
                continue
            num = safe_int(get_field(raw, "num"))
            num_map[slug] = num
            results.append(
                MarketGift(
                    title=str(get_field(raw, "title") or "Gift"),
                    num=num,
                    slug=slug,
                    price=price,
                    owner=owner,
                )
            )
        offset = getattr(result, "next_offset", "")
        if not offset:
            break
    
    return results, num_map

# ==========================
# КЛАВИАТУРЫ
# ==========================

def main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎁 ВЫБРАТЬ МОДЕЛЬ", callback_data="models:0")],
        [InlineKeyboardButton(text="🚫 ЧЁРНЫЙ СПИСОК", callback_data="owners_blacklist")],
    ]
    if is_admin_user(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_panel")])
    if MONITOR_CHAT_ID and is_admin_user(user_id):
        rows.insert(2, [InlineKeyboardButton(text="📡 МОНИТОРИНГ", callback_data="monitor_admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def models_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total = len(BASE_GIFTS)
    start = page * GIFTS_PER_PAGE
    end = start + GIFTS_PER_PAGE
    page_gifts = BASE_GIFTS[start:end]
    buttons = []
    for gift in page_gifts:
        text = f"🎁 {gift.title}"
        if gift.resell_min_stars:
            text += f" · от {gift.resell_min_stars}⭐"
        if gift.availability_resale:
            text += f" · {gift.availability_resale} шт."
        buttons.append(InlineKeyboardButton(text=text[:60], callback_data=f"gift:{gift.gift_id}"))
    rows = [[b] for b in buttons]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ НАЗАД", callback_data=f"models:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="ВПЕРЁД ➡️", callback_data=f"models:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def search_results_keyboard(results: List[MarketGift]) -> InlineKeyboardMarkup:
    rows = []
    for i, gift in enumerate(results):
        if gift.owner.key and is_admin_user(ADMIN_ID):
            rows.append([InlineKeyboardButton(text=f"🚫 ЗАБАНИТЬ {gift.owner.display}", callback_data=f"ban_owner:{i}")])
    rows.append([InlineKeyboardButton(text="🔄 ПОВТОРИТЬ ПОИСК", callback_data="repeat_search")])
    rows.append([InlineKeyboardButton(text="🗑️ СБРОСИТЬ ИСТОРИЮ", callback_data="clear_seen_current")])
    rows.append([InlineKeyboardButton(text="🏠 ГЛАВНОЕ", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def blacklist_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(OWNERS_BLACKLIST.items())
    for i, (key, label) in enumerate(items[:20]):
        rows.append([InlineKeyboardButton(text=f"❌ {label}", callback_data=f"unban_owner:{key}")])
    if rows:
        rows.append([InlineKeyboardButton(text="🗑️ ОЧИСТИТЬ ВСЁ", callback_data="clear_owners_blacklist")])
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
# МОНИТОРИНГ
# ==========================

MONITOR_SCAN_PAGES = 8
MONITOR_SEND_DELAY = 3

async def monitor_worker():
    global monitor_running, SENT_MONITOR_SLUGS, market_snapshots
    
    SENT_MONITOR_SLUGS = load_sent_monitor_slugs()
    market_snapshots = load_market_state()
    
    log.info("Monitor started")
    
    while monitor_running:
        try:
            if not await is_user_client_authorized():
                await asyncio.sleep(30)
                continue
            if not BASE_GIFTS:
                await asyncio.sleep(30)
                continue
            
            models_to_scan = [g for g in BASE_GIFTS if g.availability_resale and g.availability_resale > 0]
            if not models_to_scan:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
            
            for base in models_to_scan:
                if not monitor_running:
                    break
                
                min_price = base.resell_min_stars or 0
                max_price = min_price + 10000
                
                try:
                    current_gifts, current_num_map = await get_full_market_state(
                        base.gift_id, min_price, max_price, MONITOR_SCAN_PAGES
                    )
                except Exception as e:
                    log.error(f"Scan error {base.title}: {e}")
                    continue
                
                if not current_gifts:
                    continue
                
                current_slugs = [g.slug for g in current_gifts]
                old_snapshot = market_snapshots.get(base.gift_id)
                new_listings = []
                
                if old_snapshot:
                    old_slugs = set(old_snapshot.get("slugs", []))
                    for slug in current_slugs:
                        if slug not in old_slugs and slug not in SENT_MONITOR_SLUGS:
                            gift_obj = next((g for g in current_gifts if g.slug == slug), None)
                            if gift_obj:
                                new_listings.append(gift_obj)
                else:
                    log.info(f"First snapshot for {base.title}: {len(current_slugs)} items")
                
                if new_listings:
                    log.info(f"New listings for {base.title}: {len(new_listings)}")
                    for gift in new_listings:
                        if gift.slug in SENT_MONITOR_SLUGS:
                            continue
                        
                        owner_display = f"@{gift.owner.username}" if gift.owner.username else gift.owner.label
                        owner_link = gift.owner.link or f"https://t.me/{gift.owner.username}" if gift.owner.username else None
                        
                        if owner_link:
                            owner_text = f"[{owner_display}]({owner_link})"
                        else:
                            owner_text = owner_display
                        
                        num_text = f" #{gift.num}" if gift.num else ""
                        
                        msg = (
                            f"🆕 *НОВЫЙ ПОДАРОК НА ПРОДАЖЕ*\n"
                            f"━━━━━━━━━━━━━━━━━\n"
                            f"🎁 *{gift.title}{num_text}*\n"
                            f"💰 Цена: *{gift.price}* ⭐\n"
                            f"👤 Владелец: {owner_text}\n"
                            f"🔗 [Купить]({gift.link})\n"
                            f"━━━━━━━━━━━━━━━━━\n"
                            f"⏱ {datetime.now().strftime('%H:%M:%S')}"
                        )
                        
                        try:
                            await bot.send_message(MONITOR_CHAT_ID, msg, disable_web_page_preview=True, parse_mode="Markdown")
                            SENT_MONITOR_SLUGS.add(gift.slug)
                            save_sent_monitor_slugs(SENT_MONITOR_SLUGS)
                            log.info(f"Sent: {gift.slug}")
                            await asyncio.sleep(MONITOR_SEND_DELAY)
                        except Exception as e:
                            log.error(f"Send error: {e}")
                
                market_snapshots[base.gift_id] = {
                    "slugs": current_slugs,
                    "num_map": current_num_map,
                    "timestamp": time.time()
                }
                save_market_state()
                await asyncio.sleep(0.5)
            
            await asyncio.sleep(MONITOR_INTERVAL)
            
        except Exception as e:
            log.error(f"Monitor error: {e}")
            await asyncio.sleep(30)

# ==========================
# КОМАНДЫ МОНИТОРИНГА
# ==========================

@dp.callback_query(F.data == "monitor_admin_panel")
async def monitor_panel(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Только для админа")
        return
    await callback.message.edit_text(
        f"📡 МОНИТОРИНГ\n\n"
        f"📊 Статус: {'🟢 РАБОТАЕТ' if monitor_running else '🔴 ОСТАНОВЛЕН'}\n"
        f"📤 Отправлено: {len(SENT_MONITOR_SLUGS)}\n"
        f"📸 Снимков: {len(market_snapshots)}\n"
        f"⏱ Интервал: {MONITOR_INTERVAL} сек.",
        reply_markup=monitor_admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "monitor_start")
async def monitor_start(callback: CallbackQuery):
    global monitor_running, monitor_task
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Только для админа")
        return
    if monitor_running:
        await callback.answer("Уже работает")
        return
    monitor_running = True
    monitor_task = asyncio.create_task(monitor_worker())
    await callback.answer("✅ Запущен")
    await monitor_panel(callback)

@dp.callback_query(F.data == "monitor_stop")
async def monitor_stop(callback: CallbackQuery):
    global monitor_running
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Только для админа")
        return
    monitor_running = False
    await callback.answer("⏹️ Остановлен")
    await monitor_panel(callback)

@dp.callback_query(F.data == "monitor_reset")
async def monitor_reset(callback: CallbackQuery):
    global SENT_MONITOR_SLUGS, market_snapshots
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Только для админа")
        return
    SENT_MONITOR_SLUGS = set()
    market_snapshots = {}
    save_sent_monitor_slugs(SENT_MONITOR_SLUGS)
    save_market_state()
    await callback.answer("Сброшено")
    await monitor_panel(callback)

@dp.callback_query(F.data == "monitor_status")
async def monitor_status(callback: CallbackQuery):
    await callback.answer(f"Статус: {'Активен' if monitor_running else 'Остановлен'}")

# ==========================
# ЧЁРНЫЙ СПИСОК
# ==========================

@dp.callback_query(F.data == "owners_blacklist")
async def show_blacklist(callback: CallbackQuery):
    if not OWNERS_BLACKLIST:
        await callback.message.edit_text("🚫 Чёрный список пуст", reply_markup=main_menu_keyboard(callback.from_user.id))
        await callback.answer()
        return
    text = "🚫 ЧЁРНЫЙ СПИСОК\n\n"
    for i, (key, label) in enumerate(OWNERS_BLACKLIST.items(), 1):
        text += f"{i}. {label}\n"
    await callback.message.edit_text(text, reply_markup=blacklist_keyboard())
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
        await callback.answer("Невозможно")

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
    await callback.answer("Очищено")
    await show_blacklist(callback)

# ==========================
# ОСНОВНЫЕ КОМАНДЫ
# ==========================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not await ensure_access(message):
        return
    
    is_auth = await is_user_client_authorized()
    
    if not is_auth:
        if is_admin_user(message.from_user.id):
            await message.answer(
                "⚠️ Нет активной сессии!\n\n"
                "Зайдите в АДМИН-ПАНЕЛЬ → УПРАВЛЕНИЕ СЕССИЯМИ\n"
                "И активируйте или добавьте сессию.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚙️ АДМИН-ПАНЕЛЬ", callback_data="admin_panel")],
                    [InlineKeyboardButton(text="➕ ДОБАВИТЬ СЕССИЮ", callback_data="add_session_btn")]
                ])
            )
        else:
            await message.answer("⚠️ Бот настраивается. Подождите.")
        return
    
    await ensure_models_loaded()
    await message.answer(
        "🎁 ПАРСЕР ПОДАРКОВ\n\n"
        "📦 ВЫБРАТЬ МОДЕЛЬ — выбери подарок и укажи цену\n"
        "🚫 ЧЁРНЫЙ СПИСОК — управление забаненными\n"
        "⚙️ АДМИН-ПАНЕЛЬ — управление ботом\n"
        "📡 МОНИТОРИНГ — автоотслеживание новых подарков\n\n"
        "📌 Пример цены: 500 800\n"
        "💰 Цены в звездах",
        reply_markup=main_menu_keyboard(message.from_user.id)
    )

@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎁 ГЛАВНОЕ МЕНЮ",
        reply_markup=main_menu_keyboard(callback.from_user.id)
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
        f"📦 ВЫБЕРИ МОДЕЛЬ\nСтраница {page+1}/{total}",
        reply_markup=models_keyboard(page)
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
        f"✅ {gift.title}\n"
        f"💰 Мин.цена: {gift.resell_min_stars or 0}⭐\n"
        f"📦 Доступно: {gift.availability_resale} шт.\n\n"
        f"📝 Отправь диапазон цен: 500 800"
    )
    await callback.answer()

@dp.message()
async def price_handler(message: Message):
    user_id = message.from_user.id
    if user_id not in USER_SELECTED_GIFT:
        return

    parts = message.text.strip().replace("-", " ").split()
    if len(parts) != 2:
        await message.answer("❌ Отправь два числа: мин и макс\nПример: 500 800")
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

    status = await message.answer(f"⏳ Ищу {base.title} от {min_p} до {max_p} ⭐...")

    seen = set()
    key = f"{gift_id}:{min_p}:{max_p}"
    if key in SEEN_GIFTS_BY_QUERY:
        seen = set(SEEN_GIFTS_BY_QUERY[key])
    
    results = await find_market_gifts(gift_id, min_p, max_p, SEARCH_RESULT_LIMIT, seen)

    LAST_RESULTS_BY_USER[user_id] = results
    
    for gift in results:
        if gift.slug not in seen:
            SEEN_GIFTS_BY_QUERY.setdefault(key, []).append(gift.slug)
    save_seen_gifts()

    await status.delete()

    if not results:
        await message.answer(f"❌ По модели {base.title} ничего не найдено в диапазоне {min_p}-{max_p} ⭐")
        return

    text = f"🎁 {base.title} | {min_p}-{max_p} ⭐\n└ Найдено: {len(results)}\n\n"
    for i, g in enumerate(results[:10], 1):
        num = f" #{g.num}" if g.num else ""
        owner_display = f"@{g.owner.username}" if g.owner.username else g.owner.label
        text += f"{i}. {g.title}{num}\n💰 {g.price} ⭐ | 👤 {owner_display}\n🔗 {g.link}\n\n"

    await message.answer(text, disable_web_page_preview=True, reply_markup=search_results_keyboard(results))

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

    await callback.answer("🔍 Ищу...")
    await callback.message.edit_text("⏳ Ищу ещё...")

    seen = set()
    key = f"{gift_id}:{min_p}:{max_p}"
    if key in SEEN_GIFTS_BY_QUERY:
        seen = set(SEEN_GIFTS_BY_QUERY[key])
    
    results = await find_market_gifts(gift_id, min_p, max_p, SEARCH_RESULT_LIMIT, seen)

    LAST_RESULTS_BY_USER[user_id] = results
    
    for gift in results:
        if gift.slug not in seen:
            SEEN_GIFTS_BY_QUERY.setdefault(key, []).append(gift.slug)
    save_seen_gifts()

    await callback.message.delete()

    if not results:
        await callback.message.answer(f"❌ По модели {base.title} ничего не найдено в диапазоне {min_p}-{max_p} ⭐")
        return

    text = f"🎁 {base.title} | {min_p}-{max_p} ⭐\n└ Найдено: {len(results)}\n\n"
    for i, g in enumerate(results[:10], 1):
        num = f" #{g.num}" if g.num else ""
        owner_display = f"@{g.owner.username}" if g.owner.username else g.owner.label
        text += f"{i}. {g.title}{num}\n💰 {g.price} ⭐ | 👤 {owner_display}\n🔗 {g.link}\n\n"

    await callback.message.answer(text, disable_web_page_preview=True, reply_markup=search_results_keyboard(results))

@dp.callback_query(F.data == "clear_seen_current")
async def clear_seen(callback: CallbackQuery):
    user_id = callback.from_user.id
    search = LAST_SEARCH_BY_USER.get(user_id)
    if search:
        key = f"{search['gift_id']}:{search['min_stars']}:{search['max_stars']}"
        SEEN_GIFTS_BY_QUERY.pop(key, None)
        save_seen_gifts()
    await callback.answer("🧹 Сброшено")
    await callback.message.edit_text("🧹 История сброшена", reply_markup=main_menu_keyboard(user_id))

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено")

# ==========================
# ЗАПУСК
# ==========================

async def main():
    global OWNERS_BLACKLIST, SEEN_GIFTS_BY_QUERY, SENT_MONITOR_SLUGS, market_snapshots, monitor_running, monitor_task

    OWNERS_BLACKLIST = load_owners_blacklist()
    SEEN_GIFTS_BY_QUERY = load_seen_gifts()
    SENT_MONITOR_SLUGS = load_sent_monitor_slugs()
    market_snapshots = load_market_state()

    log.info(f"Loaded: blacklist={len(OWNERS_BLACKLIST)}, seen={len(SEEN_GIFTS_BY_QUERY)}, monitor={len(SENT_MONITOR_SLUGS)}, snapshots={len(market_snapshots)}")

    # Инициализируем менеджер сессий
    await init_session_manager()

    if await is_user_client_authorized():
        me = await user_client.get_me()
        log.info(f"Telethon signed in as {me.first_name}")
        try:
            await load_base_gifts()
        except Exception as e:
            log.error(f"Models load error: {e}")
        if MONITOR_CHAT_ID:
            monitor_running = True
            monitor_task = asyncio.create_task(monitor_worker())
            log.info("Monitor started")
    else:
        log.info("No active session. Admin must add or activate session.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
