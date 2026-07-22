import re
import time
import logging
from collections import deque
from aiogram import Bot, Dispatcher, F
from aiogram.types import ChatMemberUpdated, Message
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
import asyncio
import config

logging.basicConfig(level=logging.INFO)
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# ─── Детектор китайских/арабских символов ───────────────────────────────────
CHINESE_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')
ARABIC_RE  = re.compile(r'[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]')

def has_cn_or_ar(text: str) -> bool:
    return bool(CHINESE_RE.search(text) or ARABIC_RE.search(text))

# ─── Очередь для flood-детекции ──────────────────────────────────────────────
join_times: dict[int, deque] = {}

def is_flood_join(chat_id: int) -> bool:
    now = time.time()
    if chat_id not in join_times:
        join_times[chat_id] = deque()
    q = join_times[chat_id]
    q.append(now)
    while q and now - q[0] > config.FLOOD_WINDOW:
        q.popleft()
    return len(q) >= config.FLOOD_THRESHOLD

# ─── Обработчик новых участников ─────────────────────────────────────────────
@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    user = event.new_chat_member.user
    chat_id = event.chat.id
    user_id = user.id

    should_ban = False
    reason = ""

    full_name = (user.full_name or "") + (user.username or "")
    if has_cn_or_ar(full_name):
        should_ban = True
        reason = f"китайский/арабский ник: {user.full_name}"

    if not should_ban and is_flood_join(chat_id):
        should_ban = True
        reason = f"массовое вступление (flood)"

    if should_ban:
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            logging.info(f"Кикнут {user.full_name} ({user_id}): {reason}")
        except Exception as e:
            logging.warning(f"Не удалось кикнуть {user_id}: {e}")

# ─── Команда /status для проверки работы ─────────────────────────────────────
@dp.message(F.text == "/status")
async def status(msg: Message):
    if msg.from_user.id in config.ADMIN_IDS:
        await msg.answer("✅ Антиспам-бот работает")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
