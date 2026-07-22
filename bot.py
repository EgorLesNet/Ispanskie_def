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
# DB
# ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.json")

def db_load() -> set:
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
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump({"verified_users": [list(pair) for pair in verified]}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("db_save error: %s", e)

def db_add(chat_id: int, user_id: int):
    verified_users.add((chat_id, user_id))
    db_save(verified_users)

# --- Runtime state ---
# join_flood: {chat_id: deque of (timestamp, user_id, full_name)}
join_flood = {}
whitelist = set()
stats = {"cn_ar": 0, "flood": 0, "total": 0, "captcha_fail": 0, "msg_deleted": 0}
flood_threshold = config.FLOOD_THRESHOLD
flood_window = config.FLOOD_WINDOW

connected_channels = {}
captcha_pending = {}

verified_users = db_load()
logging.info("Loaded %d verified users from db.json", len(verified_users))

CAPTCHA_TIMEOUT = 120
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

def track_flood_join(chat_id: int, user_id: int, full_name: str):
    """
    Добавляет запись о вступлении в окно флуда.
    Возвращает (is_flood: bool, to_kick: list of (user_id, full_name)).
    to_kick заполняется только при первом превышении порога (чтобы кикнуть всех сразу).
    """
    now = time.time()
    if chat_id not in join_flood:
        join_flood[chat_id] = deque()
    q = join_flood[chat_id]

    # Убираем устаревшие записи за пределами окна
    while q and now - q[0][0] > flood_window:
        q.popleft()

    was_flood = len(q) >= flood_threshold
    q.append((now, user_id, full_name))
    is_flood = len(q) >= flood_threshold

    if is_flood and not was_flood:
        # Порог только что превышен — возвращаем всех в окне для кика
        to_kick = [(uid, name) for (_, uid, name) in q]
        q.clear()
        return True, to_kick
    elif is_flood:
        # Флуд уже шёл — кикаем только текущего
        return True, [(user_id, full_name)]
    else:
        return False, []

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
                    "✅ <b>Бот подключён к чату:</b> {}\n<code>{}</code>".format(
                        event.chat.title or "без названия", chat_id),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return

    if user_id in whitelist:
        db_add(chat_id, user_id)
        return

    full_name = user.full_name or str(user_id)

    # Проверка CN/AR ника
    if has_cn_or_ar(full_name + (user.username or "")):
        stats["cn_ar"] += 1
        stats["total"] += 1
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await _notify_admins("🚫 <b>Кикнут:</b> {} (<code>{}</code>)\n<b>Причина:</b> cn/ar ник\n<b>Чат:</b> {}".format(
                full_name, user_id, event.chat.title or chat_id))
        except Exception as e:
            logging.warning("Failed to kick %s: %s", user_id, e)
        return

    # Проверка flood
    is_flood, to_kick = track_flood_join(chat_id, user_id, full_name)
    if is_flood:
        kicked_names = []
        for (uid, name) in to_kick:
            try:
                await bot.ban_chat_member(chat_id, uid)
                await bot.unban_chat_member(chat_id, uid)
                # Снимаем капчу если была отправлена
                pending = captcha_pending.pop((chat_id, uid), None)
                if pending:
                    try:
                        await bot.delete_message(chat_id, pending["msg_id"])
                    except Exception:
                        pass
                stats["flood"] += 1
                stats["total"] += 1
                kicked_names.append("{} (<code>{}</code>)".format(name, uid))
                logging.info("Flood kick: %s (%s) from %s", name, uid, chat_id)
            except Exception as e:
                logging.warning("Flood kick failed for %s: %s", uid, e)

        if kicked_names:
            await _notify_admins(
                "🌊 <b>Флуд-кик:</b> кикнуто {} чел.\n{}\n<b>Чат:</b> {}".format(
                    len(kicked_names),
                    "\n".join(kicked_names),
                    event.chat.title or chat_id
                )
            )
        return

    # Не флуд — отправляем капчу
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + CAPTCHA_TIMEOUT + 5
        )
    except Exception as e:
        logging.warning("Failed to restrict %s: %s", user_id, e)

    mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, full_name)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Я не бот — подтвердить",
            callback_data="captcha_ok_{}_{}".format(chat_id, user_id)
        )
    ]])
    try:
        captcha_msg = await bot.send_message(
            chat_id,
            "👋 {}, добро пожаловать!\n\n"
            "Пожалуйста, подтвердите, что вы не бот — нажмите кнопку в течение <b>{} секунд</b>.\n"
            "Иначе вы будете исключены.".format(mention, CAPTCHA_TIMEOUT),
            parse_mode="HTML",
            reply_markup=keyboard
        )
        captcha_pending[(chat_id, user_id)] = {
            "msg_id": captcha_msg.message_id,
            "expire": time.time() + CAPTCHA_TIMEOUT
        }
        asyncio.create_task(_captcha_timeout(chat_id, user_id, full_name, captcha_msg.message_id))
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
        await _notify_admins("⏰ <b>Кикнут по таймауту капчи:</b> {} (<code>{}</code>)\n<b>Чат:</b> {}".format(
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
        await call.answer("Эта кнопка не для вас.", show_alert=True)
        return

    if (chat_id, user_id) not in captcha_pending:
        await call.answer("Капча уже недействительна.", show_alert=True)
        return

    captcha_pending.pop((chat_id, user_id), None)
    db_add(chat_id, user_id)

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

    await call.answer("✅ Проверка пройдена! Добро пожаловать.")
    logging.info("Captcha passed by %s (%s) in chat %s", call.from_user.full_name, user_id, chat_id)


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

    if is_admin(user_id) or user_id in whitelist:
        return

    key = (chat_id, user_id)

    if key not in verified_users:
        if key in captcha_pending:
            try:
                await msg.delete()
                stats["msg_deleted"] += 1
            except Exception:
                pass
            return

        mention = '<a href="tg://user?id={}">{}</a>'.format(user_id, msg.from_user.full_name)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Я не бот — подтвердить",
                callback_data="captcha_ok_{}_{}".format(chat_id, user_id)
            )
        ]])
        try:
            captcha_msg = await msg.reply(
                "👋 {}, прежде чем писать — подтвердите, что вы не бот.\n\n"
                "Нажмите кнопку в течение <b>{} секунд</b>.".format(mention, CAPTCHA_TIMEOUT),
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

        try:
            await msg.delete()
            stats["msg_deleted"] += 1
        except Exception:
            pass
        return

    text = msg.text or msg.caption or ""
    if has_cn_or_ar(text):
        try:
            await msg.delete()
            stats["msg_deleted"] += 1
            await bot.send_message(chat_id, "🚫 Сообщение удалено: недопустимые символы.", parse_mode="HTML")
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
    "🛡 <b>Антиспам-бот — Панель управления</b>\n\n"
    "/status — статус бота\n"
    "/stats — статистика\n"
    "/channels — подключённые каналы/группы\n"
    "/addchannel [chat_id] — подключить вручную\n"
    "/removechannel [chat_id] — отключить\n"
    "/memberstats [chat_id] — статистика участников\n"
    "/setflood [кол-во] [секунд] — порог flood\n"
    "  пример: <code>/setflood 3 5</code>\n"
    "/setcaptcha [секунд] — таймаут капчи (сейчас: {})\n"
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
    await msg.answer("✅ <b>Бот работает</b>\nPolling активен.", parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(
        "📊 <b>Статистика</b>\n\n"
        "🇨🇳 Китайский/арабский ник: <b>{}</b>\n"
        "🌊 Flood-вступление: <b>{}</b>\n"
        "⏰ Не прошли капчу: <b>{}</b>\n"
        "🗑 Удалено сообщений: <b>{}</b>\n"
        "──────────────\n"
        "🚫 Всего кикнуто: <b>{}</b>\n"
        "👥 Верифицировано в БД: <b>{}</b>".format(
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
        await msg.answer("Использование: <code>/setcaptcha [секунд]</code>\nПример: <code>/setcaptcha 60</code>", parse_mode="HTML")
        return
    CAPTCHA_TIMEOUT = int(parts[1])
    await msg.answer("✅ Таймаут капчи: <b>{} сек.</b>".format(CAPTCHA_TIMEOUT), parse_mode="HTML")

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
        "Белый список: <b>{}</b>\n"
        "Верифицировано в БД: <b>{}</b>".format(
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
        await msg.answer("Неверный формат. Напиши /help", parse_mode="HTML")

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
