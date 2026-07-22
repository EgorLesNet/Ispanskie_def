# -*- coding: utf-8 -*-
import re
import time
import json
import os
import logging
from collections import deque
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
# DB — сохранение verified_users в db.json
# ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.json")

def db_load() -> set:
    """Загрузить verified_users из db.json. Возвращает set of (chat_id, user_id)."""
    if not os.path.exists(DB_PATH):
        return set()
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(tuple(pair) for pair in data.get("verified_users", []))
    except Exception as e:
        logging.warning("db_load error: %s", e)
        return set()

def db_save(verified: set):
    """Сохранить verified_users в db.json."""
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump({"verified_users": [list(pair) for pair in verified]}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("db_save error: %s", e)

def db_add(chat_id: int, user_id: int):
    verified_users.add((chat_id, user_id))
    db_save(verified_users)

# --- Runtime state ---
join_times = {}
whitelist = set()
stats = {"cn_ar": 0, "flood": 0, "total": 0, "captcha_fail": 0, "msg_deleted": 0}
flood_threshold = config.FLOOD_THRESHOLD
flood_window = config.FLOOD_WINDOW

connected_channels = {}
captcha_pending = {}

# Загружаем верифицированных из БД при старте
verified_users = db_load()
logging.info("Loaded %d verified users from db.json", len(verified_users))

CAPTCHA_TIMEOUT = 120
CAPTCHA_SUCCESS_DELETE_AFTER = 20

# Сервисные типы — не модерируем
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

def is_flood_join(chat_id):
    now = time.time()
    if chat_id not in join_times:
        join_times[chat_id] = deque()
    q = join_times[chat_id]
    q.append(now)
    while q and now - q[0] > flood_window:
        q.popleft()
    return len(q) >= flood_threshold

async def _delete_message_after(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# ─────────────────────────────────────────────
# NEW MEMBER HANDLER
# ─────────────────────────────────────────────
@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    user = event.new_chat_member.user
    chat_id = event.chat.id
    user_id = user.id

    if user_id == (await bot.get_me()).id:
        connected_channels[chat_id] = event.chat.title or str(chat_id)
        logging.info("Bot added to chat: %s (%s)", event.chat.title, chat_id)
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "\u2705 <b>\u0411\u043e\u0442 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d \u043a \u0447\u0430\u0442\u0443:</b> {}\n<code>{}</code>".format(
                        event.chat.title or "\u0431\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f", chat_id),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return

    if user_id in whitelist:
        db_add(chat_id, user_id)
        return

    full_name = (user.full_name or "") + (user.username or "")
    if has_cn_or_ar(full_name):
        stats["cn_ar"] += 1
        stats["total"] += 1
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await _notify_admins("\U0001f6ab <b>\u041a\u0438\u043a\u043d\u0443\u0442:</b> {} (<code>{}</code>)\n<b>\u041f\u0440\u0438\u0447\u0438\u043d\u0430:</b> cn/ar \u043d\u0438\u043a\n<b>\u0427\u0430\u0442:</b> {}".format(
                user.full_name, user_id, event.chat.title or chat_id))
        except Exception as e:
            logging.warning("Failed to kick %s: %s", user_id, e)
        return

    if is_flood_join(chat_id):
        stats["flood"] += 1
        stats["total"] += 1
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await _notify_admins("\U0001f6ab <b>\u041a\u0438\u043a\u043d\u0443\u0442:</b> {} (<code>{}</code>)\n<b>\u041f\u0440\u0438\u0447\u0438\u043d\u0430:</b> flood join\n<b>\u0427\u0430\u0442:</b> {}".format(
                user.full_name, user_id, event.chat.title or chat_id))
        except Exception as e:
            logging.warning("Failed to kick %s: %s", user_id, e)
        return

    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + CAPTCHA_TIMEOUT + 5
        )
    except Exception as e:
        logging.warning("Failed to restrict %s: %s", user_id, e)

    mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, user.full_name)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="\u2705 \u042f \u043d\u0435 \u0431\u043e\u0442 \u2014 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c",
            callback_data="captcha_ok_{}_{}".format(chat_id, user_id)
        )
    ]])
    try:
        captcha_msg = await bot.send_message(
            chat_id,
            "\U0001f44b {}, \u0434\u043e\u0431\u0440\u043e \u043f\u043e\u0436\u0430\u043b\u043e\u0432\u0430\u0442\u044c!\n\n"
            "\u041f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430, \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435, \u0447\u0442\u043e \u0432\u044b \u043d\u0435 \u0431\u043e\u0442 \u2014 \u043d\u0430\u0436\u043c\u0438\u0442\u0435 \u043a\u043d\u043e\u043f\u043a\u0443 \u0432 \u0442\u0435\u0447\u0435\u043d\u0438\u0435 <b>{} \u0441\u0435\u043a\u0443\u043d\u0434</b>.\n"
            "\u0418\u043d\u0430\u0447\u0435 \u0432\u044b \u0431\u0443\u0434\u0435\u0442\u0435 \u0438\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u044b.".format(mention, CAPTCHA_TIMEOUT),
            parse_mode="HTML",
            reply_markup=keyboard
        )
        captcha_pending[(chat_id, user_id)] = {
            "msg_id": captcha_msg.message_id,
            "expire": time.time() + CAPTCHA_TIMEOUT
        }
        asyncio.create_task(_captcha_timeout(chat_id, user_id, user.full_name, captcha_msg.message_id))
    except Exception as e:
        logging.warning("Failed to send captcha for %s: %s", user_id, e)


async def _captcha_timeout(chat_id, user_id, full_name, captcha_msg_id):
    await asyncio.sleep(CAPTCHA_TIMEOUT)
    if (chat_id, user_id) not in captcha_pending:
        return
    captcha_pending.pop((chat_id, user_id), None)
    stats["captcha_fail"] += 1
    stats["total"] += 1
    try:
        await bot.delete_message(chat_id, captcha_msg_id)
    except Exception:
        pass
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        logging.info("Kicked %s (%s): captcha timeout", full_name, user_id)
        await _notify_admins("\u23f0 <b>\u041a\u0438\u043a\u043d\u0443\u0442 \u043f\u043e \u0442\u0430\u0439\u043c\u0430\u0443\u0442\u0443 \u043a\u0430\u043f\u0447\u0438:</b> {} (<code>{}</code>)\n<b>\u0427\u0430\u0442:</b> {}".format(
            full_name, user_id, chat_id))
    except Exception as e:
        logging.warning("Captcha timeout kick failed for %s: %s", user_id, e)


# ─────────────────────────────────────────────
# CAPTCHA CALLBACK
# ─────────────────────────────────────────────
@dp.callback_query(F.data.startswith("captcha_ok_"))
async def cb_captcha_ok(call: CallbackQuery):
    parts = call.data.split("_")
    chat_id = int(parts[2])
    user_id = int(parts[3])

    if call.from_user.id != user_id:
        await call.answer("\u042d\u0442\u0430 \u043a\u043d\u043e\u043f\u043a\u0430 \u043d\u0435 \u0434\u043b\u044f \u0432\u0430\u0441.", show_alert=True)
        return

    if (chat_id, user_id) not in captcha_pending:
        await call.answer("\u041a\u0430\u043f\u0447\u0430 \u0443\u0436\u0435 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u044c\u043d\u0430.", show_alert=True)
        return

    captcha_pending.pop((chat_id, user_id), None)
    db_add(chat_id, user_id)  # сохраняем в БД

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
            "\u2705 {} \u0443\u0441\u043f\u0435\u0448\u043d\u043e \u043f\u0440\u043e\u0448\u0451\u043b \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443 \u0438 \u043c\u043e\u0436\u0435\u0442 \u043f\u0438\u0441\u0430\u0442\u044c \u0432 \u0447\u0430\u0442\u0435!".format(mention),
            parse_mode="HTML"
        )
        asyncio.create_task(
            _delete_message_after(call.message.chat.id, call.message.message_id, CAPTCHA_SUCCESS_DELETE_AFTER)
        )
    except Exception:
        pass

    await call.answer("\u2705 \u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043f\u0440\u043e\u0439\u0434\u0435\u043d\u0430! \u0414\u043e\u0431\u0440\u043e \u043f\u043e\u0436\u0430\u043b\u043e\u0432\u0430\u0442\u044c.")
    logging.info("Captcha passed by %s (%s) in chat %s", call.from_user.full_name, user_id, chat_id)


# ─────────────────────────────────────────────
# MESSAGE MODERATION
# ─────────────────────────────────────────────
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def moderate_message(msg: Message):
    # Пропускаем сервисные сообщения (вход/выход/пин и т.д.)
    if msg.content_type in SERVICE_CONTENT_TYPES:
        return

    # Пропускаем сообщения от анонимных каналов (sender_chat)
    # Каналы, прикреплённые к группе, всегда могут писать
    if msg.sender_chat is not None:
        return

    if not msg.from_user:
        return

    user_id = msg.from_user.id
    chat_id = msg.chat.id

    if is_admin(user_id) or user_id in whitelist:
        return

    key = (chat_id, user_id)

    if key not in verified_users:
        try:
            await msg.delete()
            stats["msg_deleted"] += 1
        except Exception:
            pass

        if key in captcha_pending:
            return

        mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, msg.from_user.full_name)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="\u2705 \u042f \u043d\u0435 \u0431\u043e\u0442 \u2014 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c",
                callback_data="captcha_ok_{}_{}".format(chat_id, user_id)
            )
        ]])
        try:
            captcha_msg = await bot.send_message(
                chat_id,
                "\U0001f44b {}, \u043f\u0440\u0435\u0436\u0434\u0435 \u0447\u0435\u043c \u043f\u0438\u0441\u0430\u0442\u044c \u2014 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435, \u0447\u0442\u043e \u0432\u044b \u043d\u0435 \u0431\u043e\u0442.\n\n"
                "\u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u043a\u043d\u043e\u043f\u043a\u0443 \u0432 \u0442\u0435\u0447\u0435\u043d\u0438\u0435 <b>{} \u0441\u0435\u043a\u0443\u043d\u0434</b>.".format(mention, CAPTCHA_TIMEOUT),
                parse_mode="HTML",
                reply_markup=keyboard
            )
            captcha_pending[key] = {
                "msg_id": captcha_msg.message_id,
                "expire": time.time() + CAPTCHA_TIMEOUT
            }
            asyncio.create_task(
                _captcha_timeout(chat_id, user_id, msg.from_user.full_name, captcha_msg.message_id)
            )
        except Exception as e:
            logging.warning("Failed to send captcha on message: %s", e)
        return

    # Верифицирован — блокируем CN/AR текст
    text = msg.text or msg.caption or ""
    if has_cn_or_ar(text):
        try:
            await msg.delete()
            stats["msg_deleted"] += 1
            await bot.send_message(chat_id, "\U0001f6ab \u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0443\u0434\u0430\u043b\u0435\u043d\u043e: \u043d\u0435\u0434\u043e\u043f\u0443\u0441\u0442\u0438\u043c\u044b\u0435 \u0441\u0438\u043c\u0432\u043e\u043b\u044b.", parse_mode="HTML")
        except Exception:
            pass


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
    "\U0001f6e1 <b>\u0410\u043d\u0442\u0438\u0441\u043f\u0430\u043c-\u0431\u043e\u0442 \u2014 \u041f\u0430\u043d\u0435\u043b\u044c \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f</b>\n\n"
    "/status \u2014 \u0441\u0442\u0430\u0442\u0443\u0441 \u0431\u043e\u0442\u0430\n"
    "/stats \u2014 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430\n"
    "/channels \u2014 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u044b\u0435 \u043a\u0430\u043d\u0430\u043b\u044b/\u0433\u0440\u0443\u043f\u043f\u044b\n"
    "/addchannel [chat_id] \u2014 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0432\u0440\u0443\u0447\u043d\u0443\u044e\n"
    "/removechannel [chat_id] \u2014 \u043e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c\n"
    "/memberstats [chat_id] \u2014 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432\n"
    "/setflood [\u043a\u043e\u043b-\u0432\u043e] [\u0441\u0435\u043a\u0443\u043d\u0434] \u2014 \u043f\u043e\u0440\u043e\u0433 flood\n"
    "  \u043f\u0440\u0438\u043c\u0435\u0440: <code>/setflood 3 5</code>\n"
    "/setcaptcha [\u0441\u0435\u043a\u0443\u043d\u0434] \u2014 \u0442\u0430\u0439\u043c\u0430\u0443\u0442 \u043a\u0430\u043f\u0447\u0438 (\u0441\u0435\u0439\u0447\u0430\u0441: {})\n"
    "/whitelist add [user_id] \u2014 \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0432 \u0431\u0435\u043b\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a\n"
    "/whitelist remove [user_id] \u2014 \u0443\u0431\u0440\u0430\u0442\u044c\n"
    "/whitelist list \u2014 \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0431\u0435\u043b\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a\n"
    "/settings \u2014 \u0442\u0435\u043a\u0443\u0449\u0438\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438\n"
    "/help \u2014 \u044d\u0442\u043e \u043c\u0435\u043d\u044e"
)

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(MENU_TEXT.format(CAPTCHA_TIMEOUT), parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(MENU_TEXT.format(CAPTCHA_TIMEOUT), parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer("\u2705 <b>\u0411\u043e\u0442 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442</b>\nPolling \u0430\u043a\u0442\u0438\u0432\u0435\u043d.", parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(
        "\U0001f4ca <b>\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430</b>\n\n"
        "\U0001f1e8\U0001f1f3 \u041a\u0438\u0442\u0430\u0439\u0441\u043a\u0438\u0439/\u0430\u0440\u0430\u0431\u0441\u043a\u0438\u0439 \u043d\u0438\u043a: <b>{}</b>\n"
        "\U0001f30a Flood-\u0432\u0441\u0442\u0443\u043f\u043b\u0435\u043d\u0438\u0435: <b>{}</b>\n"
        "\u23f0 \u041d\u0435 \u043f\u0440\u043e\u0448\u043b\u0438 \u043a\u0430\u043f\u0447\u0443: <b>{}</b>\n"
        "\U0001f5d1 \u0423\u0434\u0430\u043b\u0435\u043d\u043e \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439: <b>{}</b>\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\U0001f6ab \u0412\u0441\u0435\u0433\u043e \u043a\u0438\u043a\u043d\u0443\u0442\u043e: <b>{}</b>\n"
        "\U0001f465 \u0412\u0435\u0440\u0438\u0444\u0438\u0446\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0432 \u0411\u0414: <b>{}</b>".format(
            stats["cn_ar"], stats["flood"], stats["captcha_fail"],
            stats["msg_deleted"], stats["total"], len(verified_users)
        ),
        parse_mode="HTML"
    )

@dp.message(Command("setcaptcha"))
async def cmd_setcaptcha(msg: Message):
    global CAPTCHA_TIMEOUT
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0435: <code>/setcaptcha [\u0441\u0435\u043a\u0443\u043d\u0434]</code>\n\u041f\u0440\u0438\u043c\u0435\u0440: <code>/setcaptcha 60</code>", parse_mode="HTML")
        return
    CAPTCHA_TIMEOUT = int(parts[1])
    await msg.answer("\u2705 \u0422\u0430\u0439\u043c\u0430\u0443\u0442 \u043a\u0430\u043f\u0447\u0438: <b>{} \u0441\u0435\u043a.</b>".format(CAPTCHA_TIMEOUT), parse_mode="HTML")

@dp.message(Command("setflood"))
async def cmd_setflood(msg: Message):
    global flood_threshold, flood_window
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await msg.answer("\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0435: <code>/setflood [\u043a\u043e\u043b-\u0432\u043e] [\u0441\u0435\u043a\u0443\u043d\u0434]</code>", parse_mode="HTML")
        return
    flood_threshold = int(parts[1])
    flood_window = int(parts[2])
    await msg.answer("\u2705 Flood-\u043f\u043e\u0440\u043e\u0433: <b>{} \u0447\u0435\u043b. \u0437\u0430 {} \u0441\u0435\u043a.</b>".format(flood_threshold, flood_window), parse_mode="HTML")

@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if not admin_private(msg):
        return
    wl = ", ".join(str(u) for u in whitelist) if whitelist else "\u043f\u0443\u0441\u0442\u043e"
    await msg.answer(
        "\u2699\ufe0f <b>\u0422\u0435\u043a\u0443\u0449\u0438\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438</b>\n\n"
        "Flood-\u043f\u043e\u0440\u043e\u0433: <b>{} \u0447\u0435\u043b. \u0437\u0430 {} \u0441\u0435\u043a.</b>\n"
        "\u0422\u0430\u0439\u043c\u0430\u0443\u0442 \u043a\u0430\u043f\u0447\u0438: <b>{} \u0441\u0435\u043a.</b>\n"
        "\u0411\u0435\u043b\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a: <b>{}</b>\n"
        "\u0412\u0435\u0440\u0438\u0444\u0438\u0446\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0432 \u0411\u0414: <b>{}</b>".format(
            flood_threshold, flood_window, CAPTCHA_TIMEOUT, wl, len(verified_users)
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
            "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0435:\n"
            "<code>/whitelist add [user_id]</code>\n"
            "<code>/whitelist remove [user_id]</code>\n"
            "<code>/whitelist list</code>",
            parse_mode="HTML"
        )
        return
    action = parts[1].lower()
    if action == "list":
        wl = ", ".join(str(u) for u in whitelist) if whitelist else "\u043f\u0443\u0441\u0442\u043e"
        await msg.answer("\U0001f4cb \u0411\u0435\u043b\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a: <b>{}</b>".format(wl), parse_mode="HTML")
    elif action in ("add", "remove") and len(parts) == 3 and parts[2].lstrip("-").isdigit():
        uid = int(parts[2])
        if action == "add":
            whitelist.add(uid)
            await msg.answer("\u2705 <code>{}</code> \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d \u0432 \u0431\u0435\u043b\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a.".format(uid), parse_mode="HTML")
        else:
            whitelist.discard(uid)
            await msg.answer("\u2705 <code>{}</code> \u0443\u0434\u0430\u043b\u0451\u043d \u0438\u0437 \u0431\u0435\u043b\u043e\u0433\u043e \u0441\u043f\u0438\u0441\u043a\u0430.".format(uid), parse_mode="HTML")
    else:
        await msg.answer("\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0444\u043e\u0440\u043c\u0430\u0442. \u041d\u0430\u043f\u0438\u0448\u0438 /help", parse_mode="HTML")

# --- Channel management ---
@dp.message(Command("channels"))
async def cmd_channels(msg: Message):
    if not admin_private(msg):
        return
    if not connected_channels:
        await msg.answer(
            "\U0001f4ed <b>\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u044b\u0435 \u043a\u0430\u043d\u0430\u043b\u044b/\u0433\u0440\u0443\u043f\u043f\u044b</b>\n\n\u041f\u043e\u043a\u0430 \u043d\u0435\u0442 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u044b\u0445 \u0447\u0430\u0442\u043e\u0432.\n\n"
            "\u0414\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u0431\u043e\u0442\u0430 \u043a\u0430\u043a \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0430 \u2014 \u043e\u043d \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u0443\u0435\u0442\u0441\u044f \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438.\n"
            "\u0418\u043b\u0438: <code>/addchannel [chat_id]</code>",
            parse_mode="HTML"
        )
        return
    lines = ["\U0001f4ec <b>\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u044b\u0435 \u043a\u0430\u043d\u0430\u043b\u044b/\u0433\u0440\u0443\u043f\u043f\u044b</b>\n"]
    keyboard_buttons = []
    for i, (cid, title) in enumerate(connected_channels.items(), 1):
        lines.append("{}. {} \u2014 <code>{}</code>".format(i, title, cid))
        keyboard_buttons.append([
            InlineKeyboardButton(text="\U0001f4ca {}".format(title[:30]), callback_data="memberstats_{}".format(cid)),
            InlineKeyboardButton(text="\U0001f5d1 \u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c", callback_data="removechan_{}".format(cid))
        ])
    await msg.answer("\n".join(lines), parse_mode="HTML",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons))

@dp.message(Command("addchannel"))
async def cmd_addchannel(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0435: <code>/addchannel [chat_id]</code>", parse_mode="HTML")
        return
    cid = int(parts[1])
    try:
        chat = await bot.get_chat(cid)
        connected_channels[cid] = chat.title or str(cid)
        await msg.answer("\u2705 \u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d: <b>{}</b> (<code>{}</code>)".format(chat.title or "\u2014", cid), parse_mode="HTML")
    except Exception as e:
        await msg.answer("\u274c \u041e\u0448\u0438\u0431\u043a\u0430: <code>{}</code>".format(e), parse_mode="HTML")

@dp.message(Command("removechannel"))
async def cmd_removechannel(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0435: <code>/removechannel [chat_id]</code>", parse_mode="HTML")
        return
    cid = int(parts[1])
    if cid in connected_channels:
        title = connected_channels.pop(cid)
        await msg.answer("\u2705 \u041a\u0430\u043d\u0430\u043b <b>{}</b> \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d.".format(title), parse_mode="HTML")
    else:
        await msg.answer("\u274c \u041a\u0430\u043d\u0430\u043b <code>{}</code> \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d.".format(cid), parse_mode="HTML")

@dp.callback_query(F.data.startswith("removechan_"))
async def cb_removechan(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.", show_alert=True)
        return
    cid = int(call.data.split("_", 1)[1])
    if cid in connected_channels:
        title = connected_channels.pop(cid)
        await call.answer("\u041a\u0430\u043d\u0430\u043b \u00ab{}\u00bb \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d.".format(title), show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("\u2705 \u041a\u0430\u043d\u0430\u043b <b>{}</b> (<code>{}</code>) \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d.".format(title, cid), parse_mode="HTML")
    else:
        await call.answer("\u0423\u0436\u0435 \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d.", show_alert=True)

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
            await msg.answer("\u274c \u041d\u0435\u0442 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u044b\u0445 \u043a\u0430\u043d\u0430\u043b\u043e\u0432.", parse_mode="HTML")
            return
        else:
            buttons = [[InlineKeyboardButton(text=title[:40], callback_data="memberstats_{}".format(cid))]
                       for cid, title in connected_channels.items()]
            await msg.answer("\U0001f4ca \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u043d\u0430\u043b:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            return
    else:
        cid = int(parts[1])
    await _send_memberstats(msg.chat.id, cid)

@dp.callback_query(F.data.startswith("memberstats_"))
async def cb_memberstats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.", show_alert=True)
        return
    cid = int(call.data.split("_", 1)[1])
    await call.answer()
    await _send_memberstats(call.message.chat.id, cid)

async def _send_memberstats(target_chat_id, source_chat_id):
    title = connected_channels.get(source_chat_id, str(source_chat_id))
    await bot.send_message(target_chat_id, "\u23f3 \u0421\u043e\u0431\u0438\u0440\u0430\u044e \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0443 \u0434\u043b\u044f <b>{}</b>...".format(title), parse_mode="HTML")
    try:
        count = await bot.get_chat_member_count(source_chat_id)
        admins = await bot.get_chat_administrators(source_chat_id)
        bots = sum(1 for a in admins if a.user.is_bot)
        premium = sum(1 for a in admins if getattr(a.user, "is_premium", False))
    except Exception as e:
        await bot.send_message(target_chat_id, "\u274c \u041e\u0448\u0438\u0431\u043a\u0430: <code>{}</code>".format(e), parse_mode="HTML")
        return
    await bot.send_message(
        target_chat_id,
        "\U0001f4ca <b>\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432</b>\n"
        "\U0001f4ec \u041a\u0430\u043d\u0430\u043b: <b>{}</b> (<code>{}</code>)\n\n"
        "\U0001f465 \u0412\u0441\u0435\u0433\u043e: <b>{}</b>\n"
        "\U0001f451 \u0410\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u043e\u0432: <b>{}</b>\n"
        "\U0001f916 \u0411\u043e\u0442\u043e\u0432 (\u0438\u0437 \u0430\u0434\u043c.): <b>{}</b>\n"
        "\u2b50 Premium (\u0438\u0437 \u0430\u0434\u043c.): <b>{}</b>".format(
            title, source_chat_id, count, len(admins), bots, premium),
        parse_mode="HTML"
    )


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
