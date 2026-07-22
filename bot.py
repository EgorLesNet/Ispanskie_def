# -*- coding: utf-8 -*-
import re
import time
import logging
from collections import deque
from aiogram import Bot, Dispatcher, F
from aiogram.types import ChatMemberUpdated, Message
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.filters import Command
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

# --- Runtime state ---
join_times = {}
whitelist = set()
stats = {"cn_ar": 0, "flood": 0, "total": 0}
flood_threshold = config.FLOOD_THRESHOLD
flood_window = config.FLOOD_WINDOW

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

# --- New member handler ---
@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    user = event.new_chat_member.user
    chat_id = event.chat.id
    user_id = user.id

    if user_id in whitelist:
        return

    should_ban = False
    reason = ""

    full_name = (user.full_name or "") + (user.username or "")
    if has_cn_or_ar(full_name):
        should_ban = True
        reason = "cn/ar nick"
        stats["cn_ar"] += 1

    if not should_ban and is_flood_join(chat_id):
        should_ban = True
        reason = "flood join"
        stats["flood"] += 1

    if should_ban:
        stats["total"] += 1
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            logging.info("Kicked %s (%s): %s", user.full_name, user_id, reason)
            # Notify admin in private
            for admin_id in config.ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        "\U0001f6ab <b>Кикнут:</b> {} (<code>{}</code>)\n"
                        "<b>Причина:</b> {}\n"
                        "<b>Чат:</b> {}".format(
                            user.full_name, user_id, reason, event.chat.title or chat_id
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        except Exception as e:
            logging.warning("Failed to kick %s: %s", user_id, e)

# --- Helper: only private messages from admins ---
def admin_private(msg: Message):
    return msg.chat.type == "private" and is_admin(msg.from_user.id)

MENU_TEXT = (
    "\U0001f6e1 <b>Антиспам-бот — Панель управления</b>\n\n"
    "/status — статус бота\n"
    "/stats — статистика киков\n"
    "/setflood [кол-во] [секунд] — порог flood\n"
    "  пример: <code>/setflood 3 5</code>\n"
    "/whitelist add [user_id] — добавить в белый список\n"
    "/whitelist remove [user_id] — убрать из белого списка\n"
    "/whitelist list — показать белый список\n"
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
    await msg.answer("\u2705 <b>Бот работает</b>\nPolling активен.", parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not admin_private(msg):
        return
    await msg.answer(
        "\U0001f4ca <b>Статистика киков</b>\n\n"
        "\U0001f1e8\U0001f1f3 Китайский/арабский ник: <b>{}</b>\n"
        "\U0001f30a Flood-вступление: <b>{}</b>\n"
        "\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n"
        "\U0001f6ab Всего кикнуто: <b>{}</b>".format(
            stats["cn_ar"], stats["flood"], stats["total"]
        ),
        parse_mode="HTML"
    )

@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if not admin_private(msg):
        return
    wl = ", ".join(str(u) for u in whitelist) if whitelist else "пусто"
    await msg.answer(
        "\u2699\ufe0f <b>Текущие настройки</b>\n\n"
        "Flood-порог: <b>{} чел. за {} сек.</b>\n"
        "Белый список: <b>{}</b>".format(flood_threshold, flood_window, wl),
        parse_mode="HTML"
    )

@dp.message(Command("setflood"))
async def cmd_setflood(msg: Message):
    global flood_threshold, flood_window
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await msg.answer("Использование: <code>/setflood [кол-во] [секунд]</code>\nПример: <code>/setflood 3 5</code>", parse_mode="HTML")
        return
    flood_threshold = int(parts[1])
    flood_window = int(parts[2])
    await msg.answer(
        "\u2705 Flood-порог обновлён: <b>{} чел. за {} сек.</b>".format(flood_threshold, flood_window),
        parse_mode="HTML"
    )

@dp.message(Command("whitelist"))
async def cmd_whitelist(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.answer("Использование:\n<code>/whitelist add [user_id]</code>\n<code>/whitelist remove [user_id]</code>\n<code>/whitelist list</code>", parse_mode="HTML")
        return
    action = parts[1].lower()
    if action == "list":
        wl = ", ".join(str(u) for u in whitelist) if whitelist else "пусто"
        await msg.answer("\U0001f4cb Белый список: <b>{}</b>".format(wl), parse_mode="HTML")
    elif action in ("add", "remove") and len(parts) == 3 and parts[2].lstrip("-").isdigit():
        uid = int(parts[2])
        if action == "add":
            whitelist.add(uid)
            await msg.answer("\u2705 <code>{}</code> добавлен в белый список.".format(uid), parse_mode="HTML")
        else:
            whitelist.discard(uid)
            await msg.answer("\u2705 <code>{}</code> удалён из белого списка.".format(uid), parse_mode="HTML")
    else:
        await msg.answer("Неверный формат. Напиши /help", parse_mode="HTML")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
