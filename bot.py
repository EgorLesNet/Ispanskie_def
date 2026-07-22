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
# DB
# ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.json")

DEFAULT_DB = {
    "verified_users": [],
    "stats": {"cn_ar": 0, "flood": 0, "total": 0, "captcha_fail": 0, "msg_deleted": 0, "copypaste": 0},
    "kick_log": []
}

def db_read() -> dict:
    if not os.path.exists(DB_PATH):
        return {k: v for k, v in DEFAULT_DB.items()}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Дополняем отсутствующие поля если старый db.json
        for k, v in DEFAULT_DB.items():
            if k not in data:
                data[k] = v
        if "copypaste" not in data.get("stats", {}):
            data["stats"]["copypaste"] = 0
        return data
    except Exception as e:
        logging.warning("db_read error: %s", e)
        return {k: v for k, v in DEFAULT_DB.items()}

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

def db_add_kick_log(entry: dict):
    """entry: {user_id, name, chat_id, chat_title, reason, ts}"""
    d = db_read()
    log = d.get("kick_log", [])
    log.append(entry)
    # Храним не более 1000 записей
    if len(log) > 1000:
        log = log[-1000:]
    d["kick_log"] = log
    db_write(d)

# --- Runtime state ---
_db_init = db_read()
verified_users = set(tuple(pair) for pair in _db_init.get("verified_users", []))
stats = _db_init.get("stats", dict(DEFAULT_DB["stats"]))

logging.info("Loaded %d verified users from db.json", len(verified_users))
logging.info("Loaded stats from db.json: %s", stats)

# join_flood: {chat_id: deque of (timestamp, user_id, full_name)}
join_flood = {}
whitelist = set()
flood_threshold = config.FLOOD_THRESHOLD
flood_window = config.FLOOD_WINDOW

connected_channels = {}
captcha_pending = {}  # (chat_id, user_id) -> {msg_id, expire, answer}

# copy-paste flood: {(chat_id, user_id): deque of (timestamp, text_hash)}
copypaste_tracker = {}
COPYPASTE_LIMIT = 3      # сколько одинаковых сообщений подря��

CAPTCHA_TIMEOUT = 120
CAPTCHA_SUCCESS_DELETE_AFTER = 20

# Сервисные типы для автоудаления
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
# MATH CAPTCHA
# ─────────────────────────────────────────────
def gen_math_captcha():
    """Generate a simple math question and return (question_str, correct_answer, wrong_answers_list)"""
    a = random.randint(1, 15)
    b = random.randint(1, 15)
    op = random.choice(["+", "-", "*"])
    if op == "+":
        answer = a + b
        question = "{} + {}".format(a, b)
    elif op == "-":
        # чтобы не было отрицательных
        if a < b:
            a, b = b, a
        answer = a - b
        question = "{} - {}".format(a, b)
    else:
        a = random.randint(2, 9)
        b = random.randint(2, 9)
        answer = a * b
        question = "{} × {}".format(a, b)

    # 3 неправильных ответа
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
    # В 2 ряда по 2
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])

# ─────────────────────────────────────────────
# COPY-PASTE FLOOD
# ─────────────────────────────────────────────
def check_copypaste(chat_id: int, user_id: int, text: str) -> bool:
    """
    Возвращает True, если пользователь превысил COPYPASTE_LIMIT одинаковых сообщений подряд.
    """
    if not text or len(text.strip()) < 5:
        return False
    key = (chat_id, user_id)
    text_hash = hash(text.strip().lower())
    now = time.time()
    if key not in copypaste_tracker:
        copypaste_tracker[key] = deque()
    q = copypaste_tracker[key]
    # убираем записи старше 60 сек
    while q and now - q[0][0] > 60:
        q.popleft()
    # если последний хэш не совпадает — сбрасываем цепочку
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
    else:
        return False, []

async def _delete_message_after(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def _kick_log(user_id, name, chat_id, chat_title, reason):
    entry = {
        "user_id": user_id,
        "name": name,
        "chat_id": chat_id,
        "chat_title": chat_title or str(chat_id),
        "reason": reason,
        "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    }
    db_add_kick_log(entry)

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
        logging.info("Bot added to chat: %s (%s)", chat_title, chat_id)
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

    # CN/AR ник
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
            logging.warning("Failed to kick %s: %s", user_id, e)
        return

    # Flood join
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
                logging.info("Flood kick: %s (%s) from %s", name, uid, chat_id)
            except Exception as e:
                logging.warning("Flood kick failed for %s: %s", uid, e)
        db_save_stats()
        if kicked_names:
            await _notify_admins(
                "🌊 <b>Флуд-кик:</b> кикнуто {} чел.\n{}\n<b>Чат:</b> {}".format(
                    len(kicked_names), "\n".join(kicked_names), chat_title
                )
            )
        return

    # Не флуд — математическая капча
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + CAPTCHA_TIMEOUT + 5
        )
    except Exception as e:
        logging.warning("Failed to restrict %s: %s", user_id, e)

    await _send_math_captcha(chat_id, user_id, full_name, reply_to=None)


async def _send_math_captcha(chat_id, user_id, full_name, reply_to=None):
    """Send math captcha. reply_to = message_id to reply to, or None."""
    question, answer, wrongs = gen_math_captcha()
    keyboard = make_captcha_keyboard(chat_id, user_id, answer, wrongs)
    mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, full_name)
    text = (
        "🧠 {}, добро пожаловать!\n\n"
        "Чтобы подтвердить, что вы не бот, решите пример за <b>{} сек</b>:\n\n"
        "<b>{} = ?</b>\n\n"
        "Выберите правильный ответ:"
    ).format(mention, CAPTCHA_TIMEOUT, question)
    try:
        if reply_to:
            captcha_msg = await bot.send_message(
                chat_id, text, parse_mode="HTML", reply_markup=keyboard,
                reply_to_message_id=reply_to
            )
        else:
            captcha_msg = await bot.send_message(
                chat_id, text, parse_mode="HTML", reply_markup=keyboard
            )
        captcha_pending[(chat_id, user_id)] = {
            "msg_id": captcha_msg.message_id,
            "expire": time.time() + CAPTCHA_TIMEOUT,
            "answer": answer
        }
        asyncio.create_task(_captcha_timeout(chat_id, user_id, full_name, captcha_msg.message_id))
    except Exception as e:
        logging.warning("Failed to send math captcha for %s: %s", user_id, e)


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
        logging.info("Kicked %s (%s): captcha timeout", full_name, user_id)
        await _notify_admins("⏰ <b>Кикнут по таймауту капчи:</b> {} (<code>{}</code>)\n<b>Чат:</b> {}".format(
            full_name, user_id, chat_id))
    except Exception as e:
        logging.warning("Captcha timeout kick failed for %s: %s", user_id, e)


# ─────────────────────────────────────────────
# MATH CAPTCHA CALLBACK
# ─────────────────────────────────────────────
@dp.callback_query(F.data.startswith("mathcap_"))
async def cb_math_captcha(call: CallbackQuery):
    parts = call.data.split("_")
    # mathcap_{chat_id}_{user_id}_{chosen}
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

    if chosen != correct:
        # Неверный ответ — кикаем
        captcha_pending.pop((chat_id, user_id), None)
        stats["captcha_fail"] += 1
        stats["total"] += 1
        db_save_stats()
        full_name = call.from_user.full_name
        _kick_log(user_id, full_name, chat_id, connected_channels.get(chat_id, str(chat_id)), "капча wrong answer")
        try:
            await call.message.edit_text(
                "❌ {} ошибся — исключён.".format(full_name),
                parse_mode="HTML"
            )
            asyncio.create_task(_delete_message_after(call.message.chat.id, call.message.message_id, 5))
        except Exception:
            pass
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
        except Exception as e:
            logging.warning("Failed to kick after wrong captcha %s: %s", user_id, e)
        await call.answer("❌ Неверный ответ!", show_alert=True)
        return

    # Верный ответ
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
        logging.warning("Failed to unrestrict %s: %s", user_id, e)

    mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, call.from_user.full_name)
    try:
        await call.message.edit_text(
            "✅ {} успешно прошёл проверку и может писать в чате!".format(mention),
            parse_mode="HTML"
        )
        asyncio.create_task(
            _delete_message_after(call.message.chat.id, call.message.message_id, CAPTCHA_SUCCESS_DELETE_AFTER)
        )
    except Exception:
        pass
    await call.answer("✅ Правильно! Добро пожаловать.")
    logging.info("Math captcha passed by %s (%s) in chat %s", call.from_user.full_name, user_id, chat_id)


# ─────────────────────────────────────────────
# SERVICE MESSAGES AUTO-DELETE
# ─────────────────────────────────────────────
@dp.message(F.chat.type.in_({"group", "supergroup"}) & F.content_type.in_(SERVICE_CONTENT_TYPES))
async def auto_delete_service(msg: Message):
    """Auto-delete service messages (join/leave/pin etc.) to keep chat clean."""
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

    # --- Неверифицирован ---
    if key not in verified_users:
        if key in captcha_pending:
            try:
                await msg.delete()
                stats["msg_deleted"] += 1
                db_save_stats()
            except Exception:
                pass
            return

        # Сначала отправляем капчу реплаем, потом удаляем сообщение
        await _send_math_captcha(chat_id, user_id, full_name, reply_to=msg.message_id)
        try:
            await msg.delete()
            stats["msg_deleted"] += 1
            db_save_stats()
        except Exception:
            pass
        return

    # --- Верифицирован ---
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

    # Copy-paste flood
    if text and check_copypaste(chat_id, user_id, text):
        stats["copypaste"] += 1
        stats["msg_deleted"] += 1
        db_save_stats()
        try:
            await msg.delete()
        except Exception:
            pass
        # Мют на 60 сек
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
            # сбрасываем трекер
            copypaste_tracker.pop(key, None)
        except Exception as e:
            logging.warning("Copypaste mute failed for %s: %s", user_id, e)


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
# ADMIN COMMANDS
# ─────────────────────────────────────────────
MENU_TEXT = (
    "🛡 <b>Антиспам-бот — Панель управления</b>\n\n"
    "/status — статус бота\n"
    "/stats — статистика (персистентная)\n"
    "/kicklog [N] — последние N киков (по умолчанию 10)\n"
    "/topviolators [N] — топ N нарушителей\n"
    "/channels — подключённые каналы/группы\n"
    "/addchannel [chat_id] — подключить вручную\n"
    "/removechannel [chat_id] — отключить\n"
    "/memberstats [chat_id] — статистика участников\n"
    "/setflood [кол-во] [секунд] — порог flood\n"
    "  пример: <code>/setflood 3 5</code>\n"
    "/setcaptcha [секунд] — таймаут капчи (сейчас: {})\n"
    "/setcopypaste [N] — лимит одинаковых сообщений (сейчас: {})\n"
    "/whitelist add [user_id] — добавить в белый список\n"
    "/whitelist remove [user_id] — убрать\n"
    "/whitelist list — показать белый список\n"
    "/settings — текущие настройки\n"
    "/help — это меню"
)

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(MENU_TEXT.format(CAPTCHA_TIMEOUT, COPYPASTE_LIMIT), parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(MENU_TEXT.format(CAPTCHA_TIMEOUT, COPYPASTE_LIMIT), parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer("✅ <b>Бот работает</b>\nPolling активен.", parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not admin_private(msg):
        return
    d = db_read()
    s = d.get("stats", stats)
    await msg.answer(
        "📊 <b>Статистика (накопительная)</b>\n\n"
        "🇨🇳 cn/ar ник: <b>{}</b>\n"
        "🌊 Flood-вступление: <b>{}</b>\n"
        "⏰ Не прошли капчу: <b>{}</b>\n"
        "📋 Copy-paste спам: <b>{}</b>\n"
        "🗑 Удалено сообщений: <b>{}</b>\n"
        "──────────────\n"
        "🚫 Всего кикнуто: <b>{}</b>\n"
        "👥 Верифицировано в БД: <b>{}</b>".format(
            s.get("cn_ar", 0), s.get("flood", 0), s.get("captcha_fail", 0),
            s.get("copypaste", 0), s.get("msg_deleted", 0),
            s.get("total", 0), len(verified_users)
        ),
        parse_mode="HTML"
    )

@dp.message(Command("kicklog"))
async def cmd_kicklog(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    n = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 10
    n = min(n, 50)
    d = db_read()
    log = d.get("kick_log", [])
    if not log:
        await msg.answer("📜 Лог киков пуст.", parse_mode="HTML")
        return
    recent = log[-n:]
    recent.reverse()
    lines = ["📜 <b>Последние {} киков:</b>\n".format(len(recent))]
    for e in recent:
        lines.append(
            "• <b>{}</b> (<code>{}</code>)\n"
            "  Причина: <i>{}</i>\n"
            "  Чат: {}\n"
            "  Время: {}".format(
                e.get("name", "?"), e.get("user_id", "?"),
                e.get("reason", "?"),
                e.get("chat_title", "?"),
                e.get("ts", "?")
            )
        )
    await msg.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("topviolators"))
async def cmd_topviolators(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    n = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 10
    n = min(n, 30)
    d = db_read()
    log = d.get("kick_log", [])
    if not log:
        await msg.answer("🏆 Нарушителей пока нет.", parse_mode="HTML")
        return
    counts = {}
    names = {}
    for e in log:
        uid = e.get("user_id")
        if uid:
            counts[uid] = counts.get(uid, 0) + 1
            names[uid] = e.get("name", str(uid))
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:n]
    lines = ["🏆 <b>Топ нарушителей:</b>\n"]
    for i, (uid, cnt) in enumerate(top, 1):
        lines.append("{}. <b>{}</b> (<code>{}</code>) — {} раз()".format(
            i, names[uid], uid, cnt
        ))
    await msg.answer("\n".join(lines), parse_mode="HTML")

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
    await msg.answer("✅ Таймаут капчи: <b>{} сек.</b>".format(CAPTCHA_TIMEOUT), parse_mode="HTML")

@dp.message(Command("setcopypaste"))
async def cmd_setcopypaste(msg: Message):
    global COPYPASTE_LIMIT
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 2:
        await msg.answer("Использование: <code>/setcopypaste [N]</code> (минимум 2)", parse_mode="HTML")
        return
    COPYPASTE_LIMIT = int(parts[1])
    await msg.answer("✅ Лимит copy-paste: <b>{}</b> одинаковых сообщений подряд.".format(COPYPASTE_LIMIT), parse_mode="HTML")

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
    await msg.answer("✅ Flood-порог: <b>{} чел. за {} сек.</b>".format(flood_threshold, flood_window), parse_mode="HTML")

@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if not admin_private(msg):
        return
    wl = ", ".join(str(u) for u in whitelist) if whitelist else "пусто"
    await msg.answer(
        "⚙️ <b>Текущие настройки</b>\n\n"
        "Flood-порог: <b>{} чел. за {} сек.</b>\n"
        "Таймаут капчи: <b>{} сек.</b>\n"
        "Copy-paste лимит: <b>{} подряд</b>\n"
        "Белый список: <b>{}</b>\n"
        "Верифицировано в БД: <b>{}</b>".format(
            flood_threshold, flood_window, CAPTCHA_TIMEOUT,
            COPYPASTE_LIMIT, wl, len(verified_users)
        ),
        parse_mode="HTML"
    )

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
            parse_mode="HTML"
        )
        return
    action = parts[1].lower()
    if action == "list":
        wl = ", ".join(str(u) for u in whitelist) if whitelist else "пусто"
        await msg.answer("📋 Белый список: <b>{}</b>".format(wl), parse_mode="HTML")
    elif action in ("add", "remove") and len(parts) == 3 and parts[2].lstrip("-").isdigit():
        uid = int(parts[2])
        if action == "add":
            whitelist.add(uid)
            await msg.answer("✅ <code>{}</code> добавлен в белый список.".format(uid), parse_mode="HTML")
        else:
            whitelist.discard(uid)
            await msg.answer("✅ <code>{}</code> удалён из белого списка.".format(uid), parse_mode="HTML")
    else:
        await msg.answer("Неверный ф��рмат. Напиши /help", parse_mode="HTML")

# --- Channel management ---
@dp.message(Command("channels"))
async def cmd_channels(msg: Message):
    if not admin_private(msg):
        return
    if not connected_channels:
        await msg.answer(
            "📭 <b>Подключённые каналы/группы</b>\n\nПока нет подключённых чатов.\n\n"
            "Добавьте бота как администратора — он зарегистрируется автоматически.\n"
            "Или: <code>/addchannel [chat_id]</code>",
            parse_mode="HTML"
        )
        return
    lines = ["📬 <b>Подключённые каналы/группы</b>\n"]
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
        await msg.answer("✅ Канал <b>{}</b> отключён.".format(title), parse_mode="HTML")
    else:
        await msg.answer("❌ Канал <code>{}</code> не найден.".format(cid), parse_mode="HTML")

@dp.callback_query(F.data.startswith("removechan_"))
async def cb_removechan(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    cid = int(call.data.split("_", 1)[1])
    if cid in connected_channels:
        title = connected_channels.pop(cid)
        await call.answer("Канал «{}» отключён.".format(title), show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("✅ Канал <b>{}</b> (<code>{}</code>) отключён.".format(title, cid), parse_mode="HTML")
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
            await msg.answer("❌ Нет подключённых каналов.", parse_mode="HTML")
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
    await bot.send_message(target_chat_id, "⏳ Собиႈаю статистику для <b>{}</b>...".format(title), parse_mode="HTML")
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
        "📬 Канал: <b>{}</b> (<code>{}</code>)\n\n"
        "👥 Всего: <b>{}</b>\n"
        "👑 Администраторов: <b>{}</b>\n"
        "🤖 Ботов (из адм.): <b>{}</b>\n"
        "⭐ Premium (из адм.): <b>{}</b>".format(
            title, source_chat_id, count, len(admins), bots, premium),
        parse_mode="HTML"
    )


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
