# Деплой на VPS (Ubuntu 22.04 / 24.04)

Вы можете развернуть проект двумя способами:
1. **Через Docker Compose** (рекомендуется — работает на любой ОС за 1 минуту).
2. **Нативно через systemd + nginx** с помощью скрипта `deploy/install.sh`.

---

## Вариант 1: Docker Compose (в одну команду)

```bash
cp .env.example .env
nano .env   # укажите BOT_TOKEN, ADMIN_TG_ID, BRAND_NAME и реквизиты
docker compose up -d --build
```

Что поднимется автоматически:
- `web` (порт `8081`) — лендинг (`/`), юридические страницы (`/oferta`, `/privacy`, `/contacts`), короткие ссылки оплаты (`/pay/{id}`), веб-админка (`/admin`) и кабинет редактора (`/editor`).
- `bot` (порт `8080`) — Telegram-бот (`aiogram`) с встроенным LibreOffice для генерации PDF-превью с водяным знаком + вебхук Робокассы (`/robokassa/result`).

---

## Вариант 2: Нативная установка на Ubuntu (systemd + nginx + Let's Encrypt)

```bash
sudo REPO_URL="https://github.com/<your-username>/<your-repo>.git" \
     bash deploy/install.sh your-domain.com admin@your-domain.com
```

После установки:
1. Отредактируйте `/home/bot/Bot_bid_univer/.env`
2. Запустите сервисы:
   ```bash
   sudo systemctl start pishu24-bot pishu24-admin
   ```
3. В личном кабинете Робокассы (Магазины → Технические настройки) укажите:
   - **Result URL:** `https://your-domain.com/robokassa/result` (метод `POST`)
   - **Алгоритм расчёта хеша:** `SHA256`
