#!/bin/bash

# ============================================================
# start_bot.sh — обёртка для автозапуска бота на macOS
# Делает git pull перед запуском, затем стартует bot.py
# ============================================================

BOT_DIR="$HOME/Ispanskie_def"
PYTHON="/usr/local/bin/python3"
LOG="$BOT_DIR/bot.log"

cd "$BOT_DIR" || exit 1

echo "[$(date)] === Запуск бота ==" >> "$LOG"

# Обновляем код с GitHub
echo "[$(date)] git pull..." >> "$LOG"
git pull origin main >> "$LOG" 2>&1

# Устанавливаем/обновляем зависимости
echo "[$(date)] pip install..." >> "$LOG"
$PYTHON -m pip install -r requirements.txt --quiet >> "$LOG" 2>&1

# Запускаем бота
echo "[$(date)] Запускаем bot.py..." >> "$LOG"
exec $PYTHON bot.py >> "$LOG" 2>&1
