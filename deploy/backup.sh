#!/usr/bin/env bash
# Бэкап SQLite-базы каждый час. Хранит последние 48 копий.
# Запуск через cron: 0 * * * * /usr/local/bin/pishu24-backup.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/home/bot/Bot_bid_univer}"
BACKUP_DIR="${BACKUP_DIR:-/home/bot/backups}"
KEEP_COUNT=48

mkdir -p "${BACKUP_DIR}"

TS=$(date +%F_%H%M)
DEST="${BACKUP_DIR}/bot_${TS}.db"

# Безопасный бэкап SQLite (даже при активной записи)
sqlite3 "${APP_DIR}/bot.db" ".backup ${DEST}"
gzip "${DEST}"

# Ротация: удаляем старые
ls -1t "${BACKUP_DIR}"/bot_*.db.gz 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) | xargs -r rm -f

echo "✓ Backup: ${DEST}.gz"
