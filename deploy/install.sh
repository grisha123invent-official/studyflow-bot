#!/usr/bin/env bash
# Автоматическая установка на Ubuntu 22.04 / 24.04
# Запускать от root: sudo bash deploy/install.sh your-domain.com admin@your-domain.com

set -euo pipefail

DOMAIN="${1:-example.com}"
EMAIL="${2:-admin@${DOMAIN}}"
APP_USER="bot"
APP_DIR="/home/${APP_USER}/Bot_bid_univer"
REPO_URL="${REPO_URL:-}"

echo "▶️  Установка для домена: ${DOMAIN}, email Let's Encrypt: ${EMAIL}"

# 1. Системные пакеты (включая LibreOffice + кириллические шрифты для водяных знаков)
apt update
apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx \
               libreoffice-core libreoffice-writer libreoffice-impress fonts-dejavu fonts-liberation \
               git ufw sqlite3 cron

# 2. Firewall
ufw allow ssh
ufw allow http
ufw allow https
ufw --force enable

# 3. Пользователь bot
if ! id "${APP_USER}" &>/dev/null; then
    useradd -m -s /bin/bash "${APP_USER}"
fi

# 4. Код
if [ ! -d "${APP_DIR}" ]; then
    if [ -n "${REPO_URL}" ]; then
        sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
    else
        echo "⚠️  Скопируйте проект в ${APP_DIR} или укажите REPO_URL"
        exit 1
    fi
fi

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# 5. Виртуальное окружение и зависимости
sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/.venv"
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# 6. .env
if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    sed -i "s|https://example.com|https://${DOMAIN}|g" "${APP_DIR}/.env"
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
    echo "⚠️  Заполните ${APP_DIR}/.env своими данными (BOT_TOKEN, ADMIN_TG_ID, BRAND_NAME, ROBOKASSA_*)"
fi

# 7. Папки uploads и backups
sudo -u "${APP_USER}" mkdir -p "${APP_DIR}/uploads" "/home/${APP_USER}/backups"

# 8. systemd-сервисы (бот + веб-панель)
cp "${APP_DIR}/deploy/pishu24-bot.service" /etc/systemd/system/
cp "${APP_DIR}/deploy/pishu24-admin.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable pishu24-bot pishu24-admin

# 9. nginx
sed "s/example.com/${DOMAIN}/g" "${APP_DIR}/deploy/nginx-pishu24.conf" > /etc/nginx/sites-available/studyflow
ln -sf /etc/nginx/sites-available/studyflow /etc/nginx/sites-enabled/studyflow
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# 10. HTTPS (Let's Encrypt)
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${EMAIL}" --redirect || true

# 11. Бэкапы SQLite (cron каждый час)
cp "${APP_DIR}/deploy/backup.sh" /usr/local/bin/pishu24-backup.sh
chmod +x /usr/local/bin/pishu24-backup.sh
(crontab -u "${APP_USER}" -l 2>/dev/null; echo "0 * * * * /usr/local/bin/pishu24-backup.sh") | sort -u | crontab -u "${APP_USER}" -

echo ""
echo "✅ Установка завершена!"
echo "  1. Заполните ${APP_DIR}/.env"
echo "  2. Запустите сервисы: systemctl start pishu24-bot pishu24-admin"
echo "  3. Проверьте логи: journalctl -u pishu24-bot -u pishu24-admin -f"
