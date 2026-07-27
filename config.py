BOT_TOKEN = "YOUR_BOT_TOKEN"
ADMIN_IDS = [123456789]  # твои Telegram user_id

# Порог массового вступления: если за FLOOD_WINDOW секунд
# вступило >= FLOOD_THRESHOLD человек — удаляем всех из пачки
FLOOD_THRESHOLD = 5
FLOOD_WINDOW = 10  # секунд

# Groq API key — получить на https://console.groq.com/keys (бесплатно)
GROQ_API_KEY = "YOUR_GROQ_API_KEY"

# ID канала (не чата комментариев, а именно канала)
# Узнать: переслать пост боту @userinfobot или @RawDataBot
CHANNEL_ID = -1001234567890

# Правила канала — публикуются в комментариях под каждым постом
CHANNEL_RULES = (
    "📋 <b>Правила канала Испанские кварталы:</b>\n"
    "1. Уважайте друг друга\n"
    "2. Без спама и рекламы\n"
    "3. Только по теме района\n"
    "4. Без мата и оскорблений\n"
    "\n<i>Нарушение → предупреждение или бан</i>"
)
