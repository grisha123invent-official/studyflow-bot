"""
Точка входа — запуск бота.
Запуск: python main.py
"""
import asyncio
import logging
import os
import json
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from db.session import init_db
from bot.middlewares import RoleMiddleware
from bot.handlers import student, admin, editor

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Webhook Робокассы (Result URL) ───────────────────────────────────────────

async def robokassa_result_handler(request: web.Request) -> web.Response:
    """
    Обрабатывает Result URL от Робокассы.

    Робокасса дёргает этот URL после успешной оплаты (POST или GET).
    Параметры: OutSum, InvId, SignatureValue (+ Shp_* кастомные).

    В ответ нужно вернуть `OK<InvId>` — без этого Робокасса будет ретраить.
    """
    # Робокасса может прислать как POST form-data, так и GET query string
    if request.method == "POST":
        try:
            data = dict(await request.post())
            if not data:
                data = dict(request.query)
        except Exception:
            data = dict(request.query)
    else:
        data = dict(request.query)

    from services.payment import parse_result_params, build_result_response
    from db.session import AsyncSessionLocal
    from db.crud import (
        get_order, get_file, count_paid_orders, add_referral_bonus,
        update_order_status,
    )
    from db.models import PaymentStatus, OrderStatus, FileType, BonusType, Payment
    from sqlalchemy import select, update as sql_update

    try:
        inv_id_str, amount, is_valid = parse_result_params(data)
    except ValueError as e:
        logger.error(f"Robokassa Result parse error: {e} | data={data}")
        return web.Response(status=400, text=str(e))

    if not is_valid:
        logger.error(
            f"Robokassa Result: невалидная подпись для InvId={inv_id_str} OutSum={amount}"
        )
        return web.Response(status=403, text="Invalid signature")

    logger.info(f"Robokassa Result: InvId={inv_id_str} OutSum={amount} ✓ подпись валидна")

    if not inv_id_str.isdigit():
        logger.error(f"Robokassa Result: InvId не число: {inv_id_str}")
        return web.Response(status=400, text="Bad InvId")

    bot: Bot = request.app["bot"]
    order_id = int(inv_id_str)

    async with AsyncSessionLocal() as session:
        order = await get_order(session, order_id)
        if order is None:
            logger.warning(f"Robokassa Result: заказ #{order_id} не найден")
            # Возвращаем OK чтобы Робокасса не ретраила несуществующий заказ
            return web.Response(status=200, text=build_result_response(inv_id_str))

        # Идемпотентность: если заказ уже DONE — повторный Result
        order_status_val = order.status.value if hasattr(order.status, "value") else order.status
        if order_status_val == OrderStatus.DONE.value:
            logger.info(f"Robokassa Result: заказ #{order_id} уже DONE, пропускаем")
            return web.Response(status=200, text=build_result_response(inv_id_str))

        # Проверяем, что оплаченная сумма совпадает с ценой заказа.
        # Подпись Робокассы валидна, но это НЕ гарантирует корректную сумму —
        # без этой проверки заказ можно «оплатить» меньшей суммой. Не помечаем
        # заказ DONE и не отдаём чистый файл при расхождении.
        expected = order.price or 0.0
        if expected > 0 and abs(amount - expected) > 0.01:
            logger.error(
                f"Robokassa Result: сумма не совпадает для заказа #{order_id}: "
                f"пришло {amount}, ожидалось {expected}. Заказ НЕ помечен оплаченным."
            )
            return web.Response(status=400, text="Amount mismatch")

        # Обновляем платёж в БД (если есть запись)
        r = await session.execute(
            select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc()).limit(1)
        )
        payment = r.scalar_one_or_none()
        if payment is not None:
            payment.status = PaymentStatus.PAID.value
            from datetime import datetime, timezone
            payment.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()

        # Обновляем статус заказа
        await update_order_status(session, order_id, OrderStatus.DONE)

        # ─── Реферальный бонус ────────────────────────────────────────
        payer = order.user
        if payer.referred_by_id is not None:
            # Считаем оплаченные заказы (DONE, включая текущий)
            paid_count = await count_paid_orders(session, payer.id)
            # Первый — фикс 200 ₽, последующие — 5% от суммы заказа
            if paid_count == 1:
                bonus_amount = 200.0
                bonus_type = BonusType.FIRST_ORDER
            else:
                bonus_amount = round((order.price or 0.0) * 0.05, 2)
                bonus_type = BonusType.REPEAT

            if bonus_amount > 0:
                await add_referral_bonus(
                    session=session,
                    referrer_id=payer.referred_by_id,
                    referred_id=payer.id,
                    order_id=order_id,
                    bonus_type=bonus_type,
                    amount=bonus_amount,
                )
                # Уведомляем реферера
                from db.models import User
                result = await session.execute(
                    select(User).where(User.id == payer.referred_by_id)
                )
                referrer = result.scalar_one_or_none()
                if referrer:
                    bonus_desc = (
                        "первый заказ друга" if bonus_type == BonusType.FIRST_ORDER
                        else f"повторный заказ #{order_id}"
                    )
                    try:
                        await bot.send_message(
                            chat_id=referrer.tg_id,
                            text=(
                                f"🎉 <b>Реферальный бонус!</b>\n\n"
                                f"+<b>{bonus_amount:.0f} руб.</b> за {bonus_desc}.\n"
                                f"Ваш баланс пополнен."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error(f"Ошибка уведомления реферера: {e}")
        # ─────────────────────────────────────────────────────────────

        # Ищем финальный .docx
        final_file = await get_file(session, order_id, FileType.FINAL)
        student_tg_id = order.user.tg_id

    # Отправляем чистый .docx студенту (вне сессии, чтобы не блокировать БД)
    if final_file and final_file.tg_file_id:
        try:
            await bot.send_document(
                chat_id=student_tg_id,
                document=final_file.tg_file_id,
                caption=(
                    f"✅ <b>Оплата получена!</b>\n\n"
                    f"Вот ваш файл по заказу #{order_id}. Спасибо за заказ!"
                ),
                parse_mode="HTML",
            )
            logger.info(f"Чистый .docx отправлен студенту (order_id={order_id})")
        except Exception as e:
            logger.error(f"Ошибка отправки .docx студенту: {e}")
    elif final_file and final_file.local_path and os.path.isfile(final_file.local_path):
        try:
            from aiogram.types import FSInputFile
            await bot.send_document(
                chat_id=student_tg_id,
                document=FSInputFile(final_file.local_path),
                caption=(
                    f"✅ <b>Оплата получена!</b>\n\n"
                    f"Вот ваш файл по заказу #{order_id}. Спасибо за заказ!"
                ),
                parse_mode="HTML",
            )
            logger.info(f"Чистый .docx отправлен студенту (order_id={order_id}, local_path)")
        except Exception as e:
            logger.error(f"Ошибка отправки .docx студенту: {e}", exc_info=True)

    # Обязательный ответ Робокассе — иначе ретраит
    return web.Response(status=200, text=build_result_response(inv_id_str))


# ─── Запуск приложения ────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Инициализация базы данных...")
    await init_db()

    # Создаём директории для файлов
    os.makedirs(settings.UPLOADS_DIR, exist_ok=True)
    os.makedirs(settings.TEMPLATES_DIR, exist_ok=True)

    # Инициализируем бота. Увеличенный таймаут сессии: склеенные превью весят
    # несколько МБ, а канал до Telegram с сервера временами тормозит.
    from aiogram.client.session.aiohttp import AiohttpSession
    bot = Bot(
        token=settings.BOT_TOKEN,
        session=AiohttpSession(timeout=180),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Диспетчер с MemoryStorage (Redis не нужен)
    dp = Dispatcher(storage=MemoryStorage())

    # Подключаем middleware для всех сообщений и коллбэков.
    # outer_middleware запускается ДО проверки фильтров, чтобы RoleFilter
    # видел user в data.
    dp.message.outer_middleware(RoleMiddleware())
    dp.callback_query.outer_middleware(RoleMiddleware())

    # Подключаем роутеры
    # Важен порядок: editor → admin → student
    # (потому что document-хендлеры в editor/admin проверяют роль)
    dp.include_router(editor.router)
    dp.include_router(admin.router)
    dp.include_router(student.router)

    # Запускаем aiohttp-сервер для Result URL Робокассы (если WEBHOOK_HOST задан)
    webhook_runner = None
    if settings.ROBOKASSA_LOGIN and settings.WEBHOOK_HOST:
        app = web.Application()
        app["bot"] = bot
        # Робокасса присылает Result URL — поддерживаем и POST, и GET
        app.router.add_route("POST", "/robokassa/result", robokassa_result_handler)
        app.router.add_route("GET",  "/robokassa/result", robokassa_result_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", settings.PAYMENT_WEBHOOK_PORT)
        await site.start()
        webhook_runner = runner
        mode = "ТЕСТОВЫЙ" if settings.ROBOKASSA_IS_TEST else "БОЕВОЙ"
        logger.info(
            f"Робокасса Result URL сервер запущен на порту {settings.PAYMENT_WEBHOOK_PORT} "
            f"(режим: {mode})"
        )
    else:
        logger.warning(
            "Робокасса не настроена (ROBOKASSA_LOGIN или WEBHOOK_HOST не заданы). "
            "Result URL сервер не запущен."
        )

    logger.info("Бот запущен. Polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if webhook_runner:
            await webhook_runner.cleanup()
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
