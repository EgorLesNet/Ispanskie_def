# Ispanskie Antispam Bot

Телеграм-бот для автоматической защиты группы от спамеров.

## Функции

- 🚫 Удаляет пользователей с китайскими/арабскими символами в нике
- 🌊 Детектирует и кикает участников при массовом вступлении (flood-join)
- ✅ Команда `/status` для проверки работы бота

## Установка на macOS 10.13

```bash
# Установи pyenv
brew install pyenv
echo 'eval "$(pyenv init -)"' >> ~/.bash_profile
source ~/.bash_profile

# Установи Python 3.8
pyenv install 3.8.18
pyenv global 3.8.18

# Установи зависимости
pip install aiogram==3.7.0

# Настрой config.py — укажи BOT_TOKEN и ADMIN_IDS

# Запусти бота
python bot.py
```

## Настройка

Отредактируй `config.py`:

| Параметр | Описание | По умолчанию |
|---|---|---|
| `BOT_TOKEN` | Токен от @BotFather | — |
| `ADMIN_IDS` | Список Telegram ID администраторов | — |
| `FLOOD_THRESHOLD` | Кол-во вступлений для триггера flood | 5 |
| `FLOOD_WINDOW` | Временное окно flood в секундах | 10 |

## Права бота

Бот должен быть администратором группы с правом **«Блокировка пользователей»**.
