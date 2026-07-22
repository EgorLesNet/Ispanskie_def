#!/bin/bash
# ==============================================
# Установщик автозапуска Ispanskie Def Bot
# Запускать от имени обычного пользователя (не root)
# ==============================================

set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
SERVICE_NAME="ispanskie-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON="${BOT_DIR}/venv/bin/python"

echo "[1/5] Проверка виртуального окружения..."
if [ ! -f "${PYTHON}" ]; then
  echo "  -> Создаём venv..."
  python3 -m venv "${BOT_DIR}/venv"
fi

echo "[2/5] Установка зависимостей..."
"${BOT_DIR}/venv/bin/pip" install -q -r "${BOT_DIR}/requirements.txt"

echo "[3/5] Генерация systemd-юнита..."
cat > /tmp/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Ispanskie Def Antispam Bot
After=network.target

[Service]
User=${USER_NAME}
WorkingDirectory=${BOT_DIR}
ExecStart=${PYTHON} ${BOT_DIR}/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo "[4/5] Установка и активация сервиса (нужен sudo)..."
sudo cp /tmp/${SERVICE_NAME}.service "${SERVICE_FILE}"
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}

echo ""
echo "============================================"
echo " Готово! Бот запущен и включён в автостарт."
echo "============================================"
echo ""
echo "Полезные команды:"
echo "  Статус:  sudo systemctl status ${SERVICE_NAME}"
echo "  Логи:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Стоп:    sudo systemctl stop ${SERVICE_NAME}"
echo "  Рестарт: sudo systemctl restart ${SERVICE_NAME}"
echo "  Убрать из автозапуска: sudo systemctl disable ${SERVICE_NAME}"
