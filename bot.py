# -*- coding: utf-8 -*-
import re
import time
import json
import os
import random
import logging
from collections import deque
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    ChatMemberUpdated, Message, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery, ChatPermissions
)
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.filters import Command
from aiogram.enums import ContentType
import asyncio
import config

logging.basicConfig(level=logging.INFO)
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# --- Symbol detectors ---
CHINESE_RE = re.compile(u'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')
ARABIC_RE  = re.compile(u'[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]')

def has_cn_or_ar(text):
    return bool(CHINESE_RE.search(text) or ARABIC_RE.search(text))

# ─────────────────────────────────────────────
# SWEAR FILTER
# ─────────────────────────────────────────────
# Базовый список мата (корни слов, регистронезависимо)
DEFAULT_SWEAR_WORDS = [
    "блять", "бля", "блядь", "блин",
    "пизд", "пиздец", "пиздит", "пиздёж",
    "хуй", "хуйня", "хуйло", "хуёво",
    "ёбан", "ёб", "еб", "ебать", "ебал", "ебись",
    "пиздануть", "распиздяй",
    "мудак", "мудило",
    "сука", "сучара", "суч",
    "залупа", "залупен",
    "шлюха", "шлюшка",
    "ёбнутый", "ёбнут",
    "пиздануть", "выёбываться",
    "нахуй", "нахер",
    "охуеть", "охуен", "охуит",
    "ахуеть", "ахуен",
    "пиздануть", "пиздатый",
    "ёблан", "долбоёб", "долбоёба",
    "пиздабол", "пиздёж",
    "выблядок", "байстрюк",
    "хуесос", "хуесоска",
    "пиздострадатель",
]
SWEAR_REPLY = "Мы культурный чат, мы не материмся! 🤐"

# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.json")

DEFAULT_DB = {
    "verified_users": [],
    "stats": {"cn_ar": 0, "flood": 0, "total": 0, "captcha_fail": 0, "msg_deleted": 0, "copypaste": 0, "swear": 0},
    "kick_log": [],
    "settings": {
        "flood_threshold": None,
        "flood_window": None,
        "captcha_timeout": 120,
        "copypaste_limit": 3
    },
    "whitelist": [],
    "connected_channels": {},
    "swear_words": None
}

def db_read() -> dict:
    if not os.path.exists(DB_PATH):
        return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in DEFAULT_DB.items()}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_DB.items():
            if k not in data:
                data[k] = v
        s = data.get("stats", {})
        for key in DEFAULT_DB["stats"]:
            if key not in s:
                s[key] = 0
        data["stats"] = s
        return data
    except Exception as e:
        logging.warning("db_read error: %s", e)
        return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                for k, v in DEFAULT_DB.items()}

def db_write(data: dict):
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("db_write error: %s", e)

def db_add_verified(chat_id: int, user_id: int):
    verified_users.add((chat_id, user_id))
    d = db_read()
    d["verified_users"] = [list(p) for p in verified_users]
    db_write(d)

def db_save_stats():
    d = db_read()
    d["stats"] = stats
    db_write(d)

def db_save_settings():
    d = db_read()
    d["settings"] = {
        "flood_threshold": flood_threshold,
        "flood_window": flood_window,
        "captcha_timeout": CAPTCHA_TIMEOUT,
        "copypaste_limit": COPYPASTE_LIMIT
    }
    db_write(d)

def db_save_whitelist():
    d = db_read()
    d["whitelist"] = list(whitelist)
    db_write(d)

def db_save_channels():
    d = db_read()
    d["connected_channels"] = {str(k): v for k, v in connected_channels.items()}
    db_write(d)

def db_save_swear_words():
    d = db_read()
    d["swear_words"] = list(swear_words)
    db_write(d)

def db_add_kick_log(entry: dict):
    d = db_read()
    log = d.get("kick_log", [])
    log.append(entry)
    if len(log) > 1000:
        log = log[-1000:]
    d["kick_log"] = log
    db_write(d)

# ─────────────────────────────────────────────
# INIT RUNTIME STATE FROM DB
# ─────────────────────────────────────────────
_db_init = db_read()
verified_users = set(tuple(pair) for pair in _db_init.get("verified_users", []))
stats = _db_init.get("stats", dict(DEFAULT_DB["stats"]))
whitelist = set(_db_init.get("whitelist", []))
connected_channels = {int(k): v for k, v in _db_init.get("connected_channels", {}).items()}

_settings = _db_init.get("settings", {})
flood_threshold = _settings.get("flood_threshold") or config.FLOOD_THRESHOLD
flood_window = _settings.get("flood_window") or config.FLOOD_WINDOW
CAPTCHA_TIMEOUT = _settings.get("captcha_timeout", 120)
COPYPASTE_LIMIT = _settings.get("copypaste_limit", 3)

_sw = _db_init.get("swear_words")
swear_words = set(_sw) if _sw is not None else set(DEFAULT_SWEAR_WORDS)

logging.info("Loaded %d verified users, %d whitelist, %d channels from db.json",
             len(verified_users), len(whitelist), len(connected_channels))

# join_flood: {chat_id: deque of (timestamp, user_id, full_name)}
join_flood = {}
captcha_pending = {}  # (chat_id, user_id) -> {msg_id, expire, answer}
copypaste_tracker = {}  # (chat_id, user_id): deque of (ts, hash)

CAPTCHA_SUCCESS_DELETE_AFTER = 20

SERVICE_CONTENT_TYPES = {
    ContentType.NEW_CHAT_MEMBERS,
    ContentType.LEFT_CHAT_MEMBER,
    ContentType.NEW_CHAT_TITLE,
    ContentType.NEW_CHAT_PHOTO,
    ContentType.DELETE_CHAT_PHOTO,
    ContentType.GROUP_CHAT_CREATED,
    ContentType.SUPERGROUP_CHAT_CREATED,
    ContentType.MIGRATE_TO_CHAT_ID,
    ContentType.MIGRATE_FROM_CHAT_ID,
    ContentType.PINNED_MESSAGE,
}

def is_admin(user_id):
    return user_id in config.ADMIN_IDS

# ─────────────────────────────────────────────
# SWEAR DETECTION
# ─────────────────────────────────────────────
def _build_swear_re(words):
    if not words:
        return None
    pattern = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(pattern, re.IGNORECASE)

swear_re = _build_swear_re(swear_words)

def has_swear(text: str) -> bool:
    if not swear_re or not text:
        return False
    return bool(swear_re.search(text))

# ─────────────────────────────────────────────
# MATH CAPTCHA
# ─────────────────────────────────────────────
def gen_math_captcha():
    a = random.randint(1, 15)
    b = random.randint(1, 15)
    op = random.choice(["+", "-", "*"])
    if op == "+":
        answer = a + b
        question = "{} + {}".format(a, b)
    elif op == "-":
        if a < b:
            a, b = b, a
        answer = a - b
        question = "{} - {}".format(a, b)
    else:
        a = random.randint(2, 9)
        b = random.randint(2, 9)
        answer = a * b
        question = "{} × {}".format(a, b)
    wrong = set()
    while len(wrong) < 3:
        delta = random.choice([-3, -2, -1, 1, 2, 3])
        w = answer + delta
        if w != answer and w >= 0:
            wrong.add(w)
    return question, answer, list(wrong)

def make_captcha_keyboard(chat_id, user_id, correct, wrongs):
    options = wrongs + [correct]
    random.shuffle(options)
    buttons = [
        InlineKeyboardButton(
            text=str(opt),
            callback_data="mathcap_{}_{}_{}".format(chat_id, user_id, opt)
        )
        for opt in options
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])

# ─────────────────────────────────────────────
# COPY-PASTE FLOOD
# ─────────────────────────────────────────────
def check_copypaste(chat_id: int, user_id: int, text: str) -> bool:
    if not text or len(text.strip()) < 5:
        return False
    key = (chat_id, user_id)
    text_hash = hash(text.strip().lower())
    now = time.time()
    if key not in copypaste_tracker:
        copypaste_tracker[key] = deque()
    q = copypaste_tracker[key]
    while q and now - q[0][0] > 60:
        q.popleft()
    if q and q[-1][1] != text_hash:
        q.clear()
    q.append((now, text_hash))
    return len(q) >= COPYPASTE_LIMIT

# ─────────────────────────────────────────────
# FLOOD JOIN
# ─────────────────────────────────────────────
def track_flood_join(chat_id: int, user_id: int, full_name: str):
    now = time.time()
    if chat_id not in join_flood:
        join_flood[chat_id] = deque()
    q = join_flood[chat_id]
    while q and now - q[0][0] > flood_window:
        q.popleft()
    was_flood = len(q) >= flood_threshold
    q.append((now, user_id, full_name))
    is_flood = len(q) >= flood_threshold
    if is_flood and not was_flood:
        to_kick = [(uid, name) for (_, uid, name) in q]
        q.clear()
        return True, to_kick
    elif is_flood:
        return True, [(user_id, full_name)]
    return False, []

async def _delete_message_after(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def _kick_log(user_id, name, chat_id, chat_title, reason):
    db_add_kick_log({
        "user_id": user_id,
        "name": name,
        "chat_id": chat_id,
        "chat_title": chat_title or str(chat_id),
        "reason": reason,
        "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    })

# ─────────────────────────────────────────────
# NEW MEMBER HANDLER
# ─────────────────────────────────────────────
@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    user = event.new_chat_member.user
    chat_id = event.chat.id
    user_id = user.id
    chat_title = event.chat.title or str(chat_id)

    if user_id == (await bot.get_me()).id:
        connected_channels[chat_id] = chat_title
        db_save_channels()
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "✅ <b>Бот подключён к чату:</b> {}\n<code>{}</code>".format(chat_title, chat_id),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return

    if user_id in whitelist:
        db_add_verified(chat_id, user_id)
        return

    full_name = user.full_name or str(user_id)

    if has_cn_or_ar(full_name + (user.username or "")):
        stats["cn_ar"] += 1
        stats["total"] += 1
        db_save_stats()
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            _kick_log(user_id, full_name, chat_id, chat_title, "cn/ar ник")
            await _notify_admins("🚫 <b>Кикнут:</b> {} (<code>{}</code>)\n<b>Причина:</b> cn/ar ник\n<b>Чат:</b> {}".format(
                full_name, user_id, chat_title))
        except Exception as e:
            logging.warning("kick cn/ar failed %s: %s", user_id, e)
        return

    is_flood, to_kick = track_flood_join(chat_id, user_id, full_name)
    if is_flood:
        kicked_names = []
        for (uid, name) in to_kick:
            try:
                await bot.ban_chat_member(chat_id, uid)
                await bot.unban_chat_member(chat_id, uid)
                pending = captcha_pending.pop((chat_id, uid), None)
                if pending:
                    try:
                        await bot.delete_message(chat_id, pending["msg_id"])
                    except Exception:
                        pass
                stats["flood"] += 1
                stats["total"] += 1
                _kick_log(uid, name, chat_id, chat_title, "flood join")
                kicked_names.append("{} (<code>{}</code>)".format(name, uid))
            except Exception as e:
                logging.warning("flood kick failed %s: %s", uid, e)
        db_save_stats()
        if kicked_names:
            await _notify_admins(
                "🌊 <b>Флуд-кик:</b> {} чел.\n{}\n<b>Чат:</b> {}".format(
                    len(kicked_names), "\n".join(kicked_names), chat_title))
        return

    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + CAPTCHA_TIMEOUT + 5
        )
    except Exception as e:
        logging.warning("restrict failed %s: %s", user_id, e)

    await _send_math_captcha(chat_id, user_id, full_name, reply_to=None)


async def _send_math_captcha(chat_id, user_id, full_name, reply_to=None):
    question, answer, wrongs = gen_math_captcha()
    keyboard = make_captcha_keyboard(chat_id, user_id, answer, wrongs)
    mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, full_name)
    text = (
        "🧠 {}, добро пожаловать!\n\n"
        "Реши при��ер за <b>{} сек</b>, чтобы подтвердить, что вы не бот:\n\n"
        "<b>{} = ?</b>\n\n"
        "Выберите правильный ответ:"
    ).format(mention, CAPTCHA_TIMEOUT, question)
    try:
        kwargs = dict(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        captcha_msg = await bot.send_message(**kwargs)
        captcha_pending[(chat_id, user_id)] = {
            "msg_id": captcha_msg.message_id,
            "expire": time.time() + CAPTCHA_TIMEOUT,
            "answer": answer
        }
        asyncio.create_task(_captcha_timeout(chat_id, user_id, full_name, captcha_msg.message_id))
    except Exception as e:
        logging.warning("send captcha failed %s: %s", user_id, e)


async def _captcha_timeout(chat_id, user_id, full_name, captcha_msg_id):
    await asyncio.sleep(CAPTCHA_TIMEOUT)
    if (chat_id, user_id) not in captcha_pending:
        return
    captcha_pending.pop((chat_id, user_id), None)
    stats["captcha_fail"] += 1
    stats["total"] += 1
    db_save_stats()
    try:
        await bot.delete_message(chat_id, captcha_msg_id)
    except Exception:
        pass
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        _kick_log(user_id, full_name, chat_id, connected_channels.get(chat_id, str(chat_id)), "капча timeout")
        await _notify_admins("⏰ <b>Кикнут по таймауту:</b> {} (<code>{}</code>)\n<b>Чат:</b> {}".format(
            full_name, user_id, chat_id))
    except Exception as e:
        logging.warning("timeout kick failed %s: %s", user_id, e)


# ─────────────────────────────────────────────
# MATH CAPTCHA CALLBACK
# ─────────────────────────────────────────────
@dp.callback_query(F.data.startswith("mathcap_"))
async def cb_math_captcha(call: CallbackQuery):
    parts = call.data.split("_")
    chat_id = int(parts[1])
    user_id = int(parts[2])
    chosen = int(parts[3])

    if call.from_user.id != user_id:
        await call.answer("Эта капча не для вас.", show_alert=True)
        return

    pending = captcha_pending.get((chat_id, user_id))
    if not pending:
        await call.answer("Капча уже недействительна.", show_alert=True)
        return

    correct = pending["answer"]
    full_name = call.from_user.full_name

    if chosen != correct:
        captcha_pending.pop((chat_id, user_id), None)
        stats["captcha_fail"] += 1
        stats["total"] += 1
        db_save_stats()
        _kick_log(user_id, full_name, chat_id, connected_channels.get(chat_id, str(chat_id)), "капча wrong answer")
        try:
            await call.message.edit_text("❌ {} ошибся — исключён.".format(full_name), parse_mode="HTML")
            asyncio.create_task(_delete_message_after(call.message.chat.id, call.message.message_id, 5))
        except Exception:
            pass
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
        except Exception as e:
            logging.warning("kick after wrong captcha %s: %s", user_id, e)
        await call.answer("❌ Неверный ответ!", show_alert=True)
        return

    captcha_pending.pop((chat_id, user_id), None)
    db_add_verified(chat_id, user_id)
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
    except Exception as e:
        logging.warning("unrestrict failed %s: %s", user_id, e)

    mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, full_name)
    try:
        await call.message.edit_text(
            "✅ {} прошёл проверку и может писать!".format(mention), parse_mode="HTML")
        asyncio.create_task(
            _delete_message_after(call.message.chat.id, call.message.message_id, CAPTCHA_SUCCESS_DELETE_AFTER))
    except Exception:
        pass
    await call.answer("✅ Правильно! Добро пожаловать.")


# ─────────────────────────────────────────────
# SERVICE MESSAGES AUTO-DELETE
# ─────────────────────────────────────────────
@dp.message(F.chat.type.in_({"group", "supergroup"}) & F.content_type.in_(SERVICE_CONTENT_TYPES))
async def auto_delete_service(msg: Message):
    try:
        await msg.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────
# MESSAGE MODERATION
# ─────────────────────────────────────────────
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate_message(msg: Message):
    if msg.content_type in SERVICE_CONTENT_TYPES:
        return
    if msg.sender_chat is not None:
        return
    if not msg.from_user:
        return

    user_id = msg.from_user.id
    chat_id = msg.chat.id
    full_name = msg.from_user.full_name or str(user_id)

    if is_admin(user_id) or user_id in whitelist:
        return

    key = (chat_id, user_id)

    # Не верифицирован
    if key not in verified_users:
        if key in captcha_pending:
            try:
                await msg.delete()
                stats["msg_deleted"] += 1
                db_save_stats()
            except Exception:
                pass
            return
        await _send_math_captcha(chat_id, user_id, full_name, reply_to=msg.message_id)
        try:
            await msg.delete()
            stats["msg_deleted"] += 1
            db_save_stats()
        except Exception:
            pass
        return

    text = msg.text or msg.caption or ""

    # CN/AR текст
    if has_cn_or_ar(text):
        try:
            await msg.delete()
            stats["msg_deleted"] += 1
            db_save_stats()
            await bot.send_message(chat_id, "🚫 Сообщение удалено: недопустимые символы.", parse_mode="HTML")
        except Exception:
            pass
        return

    # Мат
    if text and has_swear(text):
        stats["swear"] += 1
        stats["msg_deleted"] += 1
        db_save_stats()
        try:
            await msg.delete()
            warn = await bot.send_message(chat_id, SWEAR_REPLY)
            asyncio.create_task(_delete_message_after(chat_id, warn.message_id, 10))
        except Exception:
            pass
        return

    # Copy-paste flood
    if text and check_copypaste(chat_id, user_id, text):
        stats["copypaste"] += 1
        stats["msg_deleted"] += 1
        db_save_stats()
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(time.time()) + 60
            )
            warn_msg = await bot.send_message(
                chat_id,
                "⚠️ <a href='tg://user?id={}'>{}</a>, обнаружен копипаст спам! Мют <b>60 сек</b>.".format(user_id, full_name),
                parse_mode="HTML"
            )
            asyncio.create_task(_delete_message_after(chat_id, warn_msg.message_id, 15))
            copypaste_tracker.pop(key, None)
        except Exception as e:
            logging.warning("copypaste mute failed %s: %s", user_id, e)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
async def _notify_admins(text: str):
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

def admin_private(msg: Message):
    return msg.chat.type == "private" and is_admin(msg.from_user.id)


# ─────────────────────────────────────────────
# /panel — INLINE CONTROL PANEL
# ─────────────────────────────────────────────
def _panel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="panel_stats"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="panel_settings")],
        [InlineKeyboardButton(text="📜 Лог киков (10)", callback_data="panel_kicklog"),
         InlineKeyboardButton(text="🏆 Топ нарушителей", callback_data="panel_top")],
        [InlineKeyboardButton(text="📬 Каналы", callback_data="panel_channels"),
         InlineKeyboardButton(text="📋 Whitelist", callback_data="panel_whitelist")],
        [InlineKeyboardButton(text="🤬 Слова-фильтры", callback_data="panel_swear"),
         InlineKeyboardButton(text="✅ Статус", callback_data="panel_status")],
    ])

@dp.message(Command("panel"))
async def cmd_panel(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(
        "🛡 <b>Панель управления</b>\n\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=_panel_keyboard()
    )

@dp.callback_query(F.data.startswith("panel_"))
async def cb_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    action = call.data[len("panel_"):]
    await call.answer()

    if action == "status":
        await call.message.answer("✅ <b>Бот работает.</b> Polling активен.", parse_mode="HTML")

    elif action == "stats":
        d = db_read()
        s = d.get("stats", stats)
        await call.message.answer(
            "📊 <b>Статистика (накопительная)</b>\n\n"
            "🇨🇳 cn/ar ник: <b>{}</b>\n"
            "🌊 Flood: <b>{}</b>\n"
            "⏰ Капча провалена: <b>{}</b>\n"
            "📋 Copypaste: <b>{}</b>\n"
            "🤬 Мат: <b>{}</b>\n"
            "🗑 Удалено сообщений: <b>{}</b>\n"
            "──────────────\n"
            "🚫 Всего кикнуто: <b>{}</b>\n"
            "👥 Верифицировано: <b>{}</b>".format(
                s.get("cn_ar", 0), s.get("flood", 0), s.get("captcha_fail", 0),
                s.get("copypaste", 0), s.get("swear", 0), s.get("msg_deleted", 0),
                s.get("total", 0), len(verified_users)
            ), parse_mode="HTML")

    elif action == "settings":
        wl = len(whitelist)
        await call.message.answer(
            "⚙️ <b>Текущие настройки</b>\n\n"
            "Flood-порог: <b>{} чел. за {} сек.</b>\n"
            "Таймаут капчи: <b>{} сек.</b>\n"
            "Copypaste лимит: <b>{} подряд</b>\n"
            "Whitelist: <b>{} чел.</b>\n"
            "Верифицировано: <b>{}</b>\n"
            "Слов в фильтре: <b>{}</b>\n\n"
            "<i>Для изменения используй команды:\n"
            "/setflood /setcaptcha /setcopypaste</i>".format(
                flood_threshold, flood_window, CAPTCHA_TIMEOUT,
                COPYPASTE_LIMIT, wl, len(verified_users), len(swear_words)
            ), parse_mode="HTML")

    elif action == "kicklog":
        d = db_read()
        log = d.get("kick_log", [])
        if not log:
            await call.message.answer("📜 Лог киков пуст.")
            return
        recent = list(reversed(log[-10:]))
        lines = ["📜 <b>Последние 10 киков:</b>\n"]
        for e in recent:
            lines.append("• <b>{}</b> — <i>{}</i> [{}]".format(
                e.get("name", "?"), e.get("reason", "?"), e.get("ts", "?")))
        await call.message.answer("\n".join(lines), parse_mode="HTML")

    elif action == "top":
        d = db_read()
        log = d.get("kick_log", [])
        if not log:
            await call.message.answer("🏆 Нарушителей пока нет.")
            return
        counts = {}
        names = {}
        for e in log:
            uid = e.get("user_id")
            if uid:
                counts[uid] = counts.get(uid, 0) + 1
                names[uid] = e.get("name", str(uid))
        top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = ["🏆 <b>Топ нарушителей:</b>\n"]
        for i, (uid, cnt) in enumerate(top, 1):
            lines.append("{}. {} — {} раз".format(i, names[uid], cnt))
        await call.message.answer("\n".join(lines), parse_mode="HTML")

    elif action == "channels":
        if not connected_channels:
            await call.message.answer("📭 Нет подключённых чатов.")
            return
        lines = ["📬 <b>Подключённые чаты:</b>\n"]
        for cid, title in connected_channels.items():
            lines.append("• {} — <code>{}</code>".format(title, cid))
        await call.message.answer("\n".join(lines), parse_mode="HTML")

    elif action == "whitelist":
        if not whitelist:
            await call.message.answer("📋 Whitelist пуст.")
            return
        wl_str = "\n".join("• <code>{}</code>".format(u) for u in whitelist)
        await call.message.answer("📋 <b>Whitelist:</b>\n" + wl_str, parse_mode="HTML")

    elif action == "swear":
        words_str = ", ".join(sorted(swear_words)[:30])
        more = " и ещё {}...".format(len(swear_words) - 30) if len(swear_words) > 30 else ""
        await call.message.answer(
            "🤬 <b>Фильтр мата</b> ({} слов):\n\n<code>{}{}</code>\n\n"
            "<i>Добавить: /addswear слово\nУдалить: /removeswear слово</i>".format(
                len(swear_words), words_str, more),
            parse_mode="HTML")


# ─────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────
MENU_TEXT = (
    "🛡 <b>Антиспам-бот — Панель управления</b>\n\n"
    "/panel — интерактивная панель (рекомендуется)\n"
    "/status — статус бота\n"
    "/stats — статистика\n"
    "/kicklog [N] — последние N киков\n"
    "/topviolators [N] — топ нарушителей\n"
    "/channels — подключённые чаты\n"
    "/addchannel [chat_id] — подключить\n"
    "/removechannel [chat_id] — отключить\n"
    "/memberstats [chat_id] — статистика участников\n"
    "/setflood [кол-во] [сек] — порог flood\n"
    "/setcaptcha [сек] — таймаут капчи\n"
    "/setcopypaste [N] — лимит copypaste\n"
    "/addswear [слово] — добавить в фильтр мата\n"
    "/removeswear [слово] — убрать из фильтра\n"
    "/listswear — список матных слов\n"
    "/whitelist add/remove/list [user_id]\n"
    "/settings — текущие настройки\n"
    "/help — это меню"
)

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(MENU_TEXT, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(MENU_TEXT, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer("✅ <b>Бот работает.</b> Polling активен.", parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not admin_private(msg):
        return
    d = db_read()
    s = d.get("stats", stats)
    await msg.answer(
        "📊 <b>Статистика (накопительная)</b>\n\n"
        "🇨🇳 cn/ar ник: <b>{}</b>\n"
        "🌊 Flood: <b>{}</b>\n"
        "⏰ Капча провалена: <b>{}</b>\n"
        "📋 Copypaste: <b>{}</b>\n"
        "🤬 Мат: <b>{}</b>\n"
        "🗑 Удалено сообщений: <b>{}</b>\n"
        "──────────────\n"
        "🚫 Всего кикнуто: <b>{}</b>\n"
        "👥 Верифицировано: <b>{}</b>".format(
            s.get("cn_ar", 0), s.get("flood", 0), s.get("captcha_fail", 0),
            s.get("copypaste", 0), s.get("swear", 0), s.get("msg_deleted", 0),
            s.get("total", 0), len(verified_users)
        ), parse_mode="HTML")

@dp.message(Command("kicklog"))
async def cmd_kicklog(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    n = min(int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 10, 50)
    d = db_read()
    log = d.get("kick_log", [])
    if not log:
        await msg.answer("📜 Лог киков пуст.")
        return
    recent = list(reversed(log[-n:]))
    lines = ["📜 <b>Последние {} киков:</b>\n".format(len(recent))]
    for e in recent:
        lines.append(
            "• <b>{}</b> (<code>{}</code>)\n"
            "  Причина: <i>{}</i> | Чат: {} | {}".format(
                e.get("name", "?"), e.get("user_id", "?"),
                e.get("reason", "?"), e.get("chat_title", "?"), e.get("ts", "?")))
    await msg.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("topviolators"))
async def cmd_topviolators(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    n = min(int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 10, 30)
    d = db_read()
    log = d.get("kick_log", [])
    if not log:
        await msg.answer("🏆 Нарушителей пока нет.")
        return
    counts, names = {}, {}
    for e in log:
        uid = e.get("user_id")
        if uid:
            counts[uid] = counts.get(uid, 0) + 1
            names[uid] = e.get("name", str(uid))
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:n]
    lines = ["🏆 <b>Топ нарушителей:</b>\n"]
    for i, (uid, cnt) in enumerate(top, 1):
        lines.append("{}. <b>{}</b> (<code>{}</code>) — {} раз".format(i, names[uid], uid, cnt))
    await msg.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("setflood"))
async def cmd_setflood(msg: Message):
    global flood_threshold, flood_window
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await msg.answer("Использование: <code>/setflood [кол-во] [секунд]</code>", parse_mode="HTML")
        return
    flood_threshold = int(parts[1])
    flood_window = int(parts[2])
    db_save_settings()
    await msg.answer("✅ Flood-порог: <b>{} чел. за {} сек.</b>".format(flood_threshold, flood_window), parse_mode="HTML")

@dp.message(Command("setcaptcha"))
async def cmd_setcaptcha(msg: Message):
    global CAPTCHA_TIMEOUT
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Использование: <code>/setcaptcha [секунд]</code>", parse_mode="HTML")
        return
    CAPTCHA_TIMEOUT = int(parts[1])
    db_save_settings()
    await msg.answer("✅ Таймаут капчи: <b>{} сек.</b>".format(CAPTCHA_TIMEOUT), parse_mode="HTML")

@dp.message(Command("setcopypaste"))
async def cmd_setcopypaste(msg: Message):
    global COPYPASTE_LIMIT
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 2:
        await msg.answer("Использование: <code>/setcopypaste [N]</code> (мин. 2)", parse_mode="HTML")
        return
    COPYPASTE_LIMIT = int(parts[1])
    db_save_settings()
    await msg.answer("✅ Лимит copypaste: <b>{}</b> подряд.".format(COPYPASTE_LIMIT), parse_mode="HTML")

@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(
        "⚙️ <b>Текущие настройки</b>\n\n"
        "Flood-порог: <b>{} чел. за {} сек.</b>\n"
        "Таймаут капчи: <b>{} сек.</b>\n"
        "Copypaste лимит: <b>{} подряд</b>\n"
        "Whitelist: <b>{} чел.</b>\n"
        "Верифицировано: <b>{}</b>\n"
        "Слов в фильтре мата: <b>{}</b>".format(
            flood_threshold, flood_window, CAPTCHA_TIMEOUT,
            COPYPASTE_LIMIT, len(whitelist), len(verified_users), len(swear_words)
        ), parse_mode="HTML")

# --- Swear commands ---
@dp.message(Command("addswear"))
async def cmd_addswear(msg: Message):
    global swear_re
    if not admin_private(msg):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/addswear слово</code>", parse_mode="HTML")
        return
    word = parts[1].strip().lower()
    swear_words.add(word)
    swear_re = _build_swear_re(swear_words)
    db_save_swear_words()
    await msg.answer("✅ Слово <code>{}</code> добавлено в фильтр.".format(word), parse_mode="HTML")

@dp.message(Command("removeswear"))
async def cmd_removeswear(msg: Message):
    global swear_re
    if not admin_private(msg):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/removeswear слово</code>", parse_mode="HTML")
        return
    word = parts[1].strip().lower()
    swear_words.discard(word)
    swear_re = _build_swear_re(swear_words)
    db_save_swear_words()
    await msg.answer("✅ Слово <code>{}</code> удалено из фильтра.".format(word), parse_mode="HTML")

@dp.message(Command("listswear"))
async def cmd_listswear(msg: Message):
    if not admin_private(msg):
        return
    if not swear_words:
        await msg.answer("🤬 Фильтр мата пуст.")
        return
    words_str = ", ".join(sorted(swear_words))
    await msg.answer("🤬 <b>Фильтр мата ({} слов):</b>\n<code>{}</code>".format(
        len(swear_words), words_str), parse_mode="HTML")

# --- Whitelist ---
@dp.message(Command("whitelist"))
async def cmd_whitelist(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.answer(
            "Использование:\n"
            "<code>/whitelist add [user_id]</code>\n"
            "<code>/whitelist remove [user_id]</code>\n"
            "<code>/whitelist list</code>",
            parse_mode="HTML")
        return
    action = parts[1].lower()
    if action == "list":
        wl = ", ".join(str(u) for u in whitelist) if whitelist else "пусто"
        await msg.answer("📋 Whitelist: <b>{}</b>".format(wl), parse_mode="HTML")
    elif action in ("add", "remove") and len(parts) == 3 and parts[2].lstrip("-").isdigit():
        uid = int(parts[2])
        if action == "add":
            whitelist.add(uid)
            await msg.answer("✅ <code>{}</code> добавлен в whitelist.".format(uid), parse_mode="HTML")
        else:
            whitelist.discard(uid)
            await msg.answer("✅ <code>{}</code> удалён из whitelist.".format(uid), parse_mode="HTML")
        db_save_whitelist()
    else:
        await msg.answer("Неверный формат. Напиши /help")

# --- Channel management ---
@dp.message(Command("channels"))
async def cmd_channels(msg: Message):
    if not admin_private(msg):
        return
    if not connected_channels:
        await msg.answer(
            "📭 <b>Нет подключённых чатов.</b>\n\n"
            "Добавьте бота как администратора или: <code>/addchannel [chat_id]</code>",
            parse_mode="HTML")
        return
    lines = ["📬 <b>Подключённые чаты:</b>\n"]
    keyboard_buttons = []
    for i, (cid, title) in enumerate(connected_channels.items(), 1):
        lines.append("{}. {} — <code>{}</code>".format(i, title, cid))
        keyboard_buttons.append([
            InlineKeyboardButton(text="📊 {}".format(title[:30]), callback_data="memberstats_{}".format(cid)),
            InlineKeyboardButton(text="🗑 Отключить", callback_data="removechan_{}".format(cid))
        ])
    await msg.answer("\n".join(lines), parse_mode="HTML",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons))

@dp.message(Command("addchannel"))
async def cmd_addchannel(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("Использование: <code>/addchannel [chat_id]</code>", parse_mode="HTML")
        return
    cid = int(parts[1])
    try:
        chat = await bot.get_chat(cid)
        connected_channels[cid] = chat.title or str(cid)
        db_save_channels()
        await msg.answer("✅ Подключён: <b>{}</b> (<code>{}</code>)".format(chat.title or "—", cid), parse_mode="HTML")
    except Exception as e:
        await msg.answer("❌ Ошибка: <code>{}</code>".format(e), parse_mode="HTML")

@dp.message(Command("removechannel"))
async def cmd_removechannel(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("Использование: <code>/removechannel [chat_id]</code>", parse_mode="HTML")
        return
    cid = int(parts[1])
    if cid in connected_channels:
        title = connected_channels.pop(cid)
        db_save_channels()
        await msg.answer("✅ Канал <b>{}</b> отключён.".format(title), parse_mode="HTML")
    else:
        await msg.answer("❌ Канал не найден.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("removechan_"))
async def cb_removechan(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    cid = int(call.data.split("_", 1)[1])
    if cid in connected_channels:
        title = connected_channels.pop(cid)
        db_save_channels()
        await call.answer("Канал «{}» отключён.".format(title), show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("✅ Канал <b>{}</b> отключён.".format(title), parse_mode="HTML")
    else:
        await call.answer("Уже отключён.", show_alert=True)

# --- Member stats ---
@dp.message(Command("memberstats"))
async def cmd_memberstats(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) == 1:
        if len(connected_channels) == 1:
            cid = list(connected_channels.keys())[0]
        elif len(connected_channels) == 0:
            await msg.answer("❌ Нет подключённых каналов.")
            return
        else:
            buttons = [[InlineKeyboardButton(text=title[:40], callback_data="memberstats_{}".format(cid))]
                       for cid, title in connected_channels.items()]
            await msg.answer("📊 Выберите канал:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            return
    else:
        cid = int(parts[1])
    await _send_memberstats(msg.chat.id, cid)

@dp.callback_query(F.data.startswith("memberstats_"))
async def cb_memberstats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    cid = int(call.data.split("_", 1)[1])
    await call.answer()
    await _send_memberstats(call.message.chat.id, cid)

async def _send_memberstats(target_chat_id, source_chat_id):
    title = connected_channels.get(source_chat_id, str(source_chat_id))
    await bot.send_message(target_chat_id, "⏳ Собираю статистику для <b>{}</b>...".format(title), parse_mode="HTML")
    try:
        count = await bot.get_chat_member_count(source_chat_id)
        admins = await bot.get_chat_administrators(source_chat_id)
        bots = sum(1 for a in admins if a.user.is_bot)
        premium = sum(1 for a in admins if getattr(a.user, "is_premium", False))
    except Exception as e:
        await bot.send_message(target_chat_id, "❌ Ошибка: <code>{}</code>".format(e), parse_mode="HTML")
        return
    await bot.send_message(
        target_chat_id,
        "📊 <b>Статистика участников</b>\n"
        "📬 Чат: <b>{}</b> (<code>{}</code>)\n\n"
        "👥 Всего: <b>{}</b>\n"
        "👑 Администраторов: <b>{}</b>\n"
        "🤖 Ботов (из адм.): <b>{}</b>\n"
        "⭐ Premium (из адм.): <b>{}</b>".format(
            title, source_chat_id, count, len(admins), bots, premium),
        parse_mode="HTML")


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
