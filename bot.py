# -*- coding: utf-8 -*-
import re
import time
import logging
from collections import deque
from aiogram import Bot, Dispatcher, F
from aiogram.types import ChatMemberUpdated, Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

# --- Connected channels store: {chat_id: chat_title} ---
connected_channels = {}

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

    # Auto-register channel in connected_channels when bot is added
    if user_id == (await bot.get_me()).id:
        connected_channels[chat_id] = event.chat.title or str(chat_id)
        logging.info("Bot added to chat: %s (%s)", event.chat.title, chat_id)
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "\u2705 <b>Бот подключён к чату:</b> {}\n<code>{}</code>".format(
                        event.chat.title or "без названия", chat_id
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return

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
    "/channels — подключённые каналы/группы\n"
    "/addchannel [chat_id] — подключить канал вручную\n"
    "/removechannel [chat_id] — отключить канал\n"
    "/memberstats [chat_id] — статистика подписчиков\n"
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

# --- Channel management ---

@dp.message(Command("channels"))
async def cmd_channels(msg: Message):
    if not admin_private(msg):
        return
    if not connected_channels:
        await msg.answer(
            "\U0001f4ed <b>Подключённые каналы/группы</b>\n\nПока нет подключённых чатов.\n\n"
            "Добавьте бота в нужный канал/группу как администратора — он автоматически зарегистрируется.\n"
            "Или используйте: <code>/addchannel [chat_id]</code>",
            parse_mode="HTML"
        )
        return

    lines = ["\U0001f4ec <b>Подключённые каналы/группы</b>\n"]
    keyboard_buttons = []
    for i, (cid, title) in enumerate(connected_channels.items(), 1):
        lines.append("{}. {} — <code>{}</code>".format(i, title, cid))
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="\U0001f4ca {}".format(title[:30]),
                callback_data="memberstats_{}".format(cid)
            ),
            InlineKeyboardButton(
                text="\U0001f5d1 Отключить",
                callback_data="removechan_{}".format(cid)
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("addchannel"))
async def cmd_addchannel(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("Использование: <code>/addchannel [chat_id]</code>\nПример: <code>/addchannel -1001234567890</code>", parse_mode="HTML")
        return
    cid = int(parts[1])
    try:
        chat = await bot.get_chat(cid)
        connected_channels[cid] = chat.title or str(cid)
        await msg.answer(
            "\u2705 Канал подключён: <b>{}</b> (<code>{}</code>)".format(chat.title or "без названия", cid),
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.answer(
            "\u274c Не удалось подключить канал. Убедитесь, что бот добавлен туда как администратор.\n<code>{}</code>".format(e),
            parse_mode="HTML"
        )

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
        await msg.answer("\u2705 Канал <b>{}</b> отключён.".format(title), parse_mode="HTML")
    else:
        await msg.answer("\u274c Канал с ID <code>{}</code> не найден в списке.".format(cid), parse_mode="HTML")

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
        await call.message.answer("\u2705 Канал <b>{}</b> (<code>{}</code>) отключён.".format(title, cid), parse_mode="HTML")
    else:
        await call.answer("Канал уже отключён.", show_alert=True)

# --- Subscriber stats ---

async def get_member_stats(chat_id: int) -> dict:
    """Collect subscriber stats for a given chat."""
    result = {
        "total": 0,
        "bots": 0,
        "premium": 0,
        "male": 0,
        "female": 0,
        "unknown_gender": 0,
        "restricted": 0,
        "admins": 0,
    }
    try:
        count = await bot.get_chat_member_count(chat_id)
        result["total"] = count

        # Get admins
        admins = await bot.get_chat_administrators(chat_id)
        result["admins"] = len(admins)

        # Collect info from admins (only accessible members)
        for admin in admins:
            user = admin.user
            if user.is_bot:
                result["bots"] += 1
            if getattr(user, "is_premium", False):
                result["premium"] += 1

    except Exception as e:
        logging.warning("Error getting member stats for %s: %s", chat_id, e)

    return result

@dp.message(Command("memberstats"))
async def cmd_memberstats(msg: Message):
    if not admin_private(msg):
        return
    parts = msg.text.split()

    # If no chat_id given and only one channel connected — use it
    if len(parts) == 1:
        if len(connected_channels) == 1:
            cid = list(connected_channels.keys())[0]
        elif len(connected_channels) == 0:
            await msg.answer("\u274c Нет подключённых каналов. Используйте <code>/addchannel [chat_id]</code>", parse_mode="HTML")
            return
        else:
            # Show picker
            lines = ["\U0001f4ca <b>Выберите канал для статистики:</b>\n"]
            buttons = []
            for cid, title in connected_channels.items():
                lines.append("• {} — <code>{}</code>".format(title, cid))
                buttons.append([InlineKeyboardButton(
                    text=title[:40],
                    callback_data="memberstats_{}".format(cid)
                )])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
            return
    else:
        if not parts[1].lstrip("-").isdigit():
            await msg.answer("Использование: <code>/memberstats [chat_id]</code>", parse_mode="HTML")
            return
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

async def _send_memberstats(target_chat_id: int, source_chat_id: int):
    """Fetch and send formatted member stats."""
    title = connected_channels.get(source_chat_id, str(source_chat_id))
    await bot.send_message(target_chat_id, "\u23f3 Собираю статистику для <b>{}</b>...".format(title), parse_mode="HTML")

    s = await get_member_stats(source_chat_id)

    # Telegram doesn't expose gender via Bot API, so we note the limitation
    text = (
        "\U0001f4ca <b>Статистика подписчиков</b>\n"
        "\U0001f4ec Канал: <b>{}</b> (<code>{}</code>)\n\n"
        "\U0001f465 Всего участников: <b>{}</b>\n"
        "\U0001f451 Администраторов: <b>{}</b>\n"
        "\U0001f916 Ботов (из числа адм.): <b>{}</b>\n"
        "\u2b50 Premium-аккаунты (из адм.): <b>{}</b>\n\n"
        "<i>\u2139\ufe0f Пол участников недоступен через Bot API Telegram.\n"
        "Для полной аналитики (м/ж, все боты, все premium) нужен User API (Telethon/Pyrogram).</i>"
    ).format(
        title, source_chat_id,
        s["total"], s["admins"], s["bots"], s["premium"]
    )
    await bot.send_message(target_chat_id, text, parse_mode="HTML")

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
