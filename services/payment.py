"""
Интеграция с Робокассой (Robokassa).

Создаёт URL для оплаты, формирует чек 54-ФЗ для самозанятого,
проверяет подписи Result-уведомлений.

Документация:
- API: https://docs.robokassa.ru/#5790
- 54-ФЗ чеки: https://docs.robokassa.ru/#7506
- Самозанятые: https://docs.robokassa.ru/legal-info/#self-employed
"""
import base64
import hashlib
import json
import logging
from typing import Optional
from urllib.parse import urlencode

from config import settings

logger = logging.getLogger(__name__)


# Базовый URL платёжной формы Робокассы (один на тестовый и боевой режимы)
ROBOKASSA_PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"


# ─── Хеш-функция ─────────────────────────────────────────────────────────────

def _hash(data: str, algo: Optional[str] = None) -> str:
    """Считает хеш по выбранному алгоритму. По умолчанию — из settings."""
    algo = (algo or settings.ROBOKASSA_HASH_ALGO or "sha256").lower()
    data_bytes = data.encode("utf-8")
    if algo == "md5":
        h = hashlib.md5(data_bytes)
    elif algo == "sha256":
        h = hashlib.sha256(data_bytes)
    elif algo == "sha384":
        h = hashlib.sha384(data_bytes)
    elif algo == "sha512":
        h = hashlib.sha512(data_bytes)
    else:
        raise ValueError(f"Неизвестный алгоритм хеша: {algo}")
    # Робокасса принимает хеш в верхнем регистре
    return h.hexdigest().upper()


# ─── Какой пароль используем ─────────────────────────────────────────────────

def _password_1() -> str:
    """Пароль #1 (для подписи запросов на создание платежа)."""
    if settings.ROBOKASSA_IS_TEST:
        return settings.ROBOKASSA_TEST_PASSWORD_1
    return settings.ROBOKASSA_PASSWORD_1


def _password_2() -> str:
    """Пароль #2 (для проверки подписи Result URL)."""
    if settings.ROBOKASSA_IS_TEST:
        return settings.ROBOKASSA_TEST_PASSWORD_2
    return settings.ROBOKASSA_PASSWORD_2


# ─── Чек 54-ФЗ для самозанятого ─────────────────────────────────────────────

def _build_receipt(
    amount: float,
    description: str,
) -> dict:
    """
    Формирует receipt для 54-ФЗ. Для самозанятых Робокасса использует
    интеграцию с приложением «Мой налог» (НПД), чек уходит автоматически.

    Параметры по справочнику Робокассы:
    - tax: "none" — без НДС (НПД не предусматривает НДС)
    - payment_method: "full_payment" — полный расчёт
    - payment_object: "service" — услуга
    """
    return {
        "items": [
            {
                "name": description[:128],
                "quantity": 1,
                "sum": round(float(amount), 2),
                "payment_method": "full_payment",
                "payment_object": "service",
                "tax": "none",
            }
        ],
    }


def _receipt_encoded(receipt: dict) -> str:
    """
    Робокасса принимает Receipt как URL-encoded JSON-строку (внутри URL).
    Для подписи нужна СЫРАЯ JSON-строка (без urlencode).
    """
    return json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))


# ─── Создание URL для оплаты ────────────────────────────────────────────────

async def create_payment_url(
    order_id: int,
    amount: float,
    description: str,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None,
) -> dict:
    """
    Создаёт ссылку на оплату через Робокассу.

    :param order_id: ID заказа (передаётся в Робокассу как InvId)
    :param amount: сумма в рублях
    :param description: описание платежа (видно клиенту и в чеке)
    :param customer_email: email для отправки чека 54-ФЗ
    :param customer_phone: телефон (альтернатива email)
    :return: dict с полями payment_id (= InvId), confirmation_url, status
    :raises RuntimeError: если магазин не настроен
    """
    if not settings.ROBOKASSA_LOGIN:
        raise RuntimeError(
            "Робокасса не настроена: ROBOKASSA_LOGIN пуст в .env"
        )
    pwd1 = _password_1()
    if not pwd1:
        mode = "тестовый" if settings.ROBOKASSA_IS_TEST else "боевой"
        raise RuntimeError(
            f"Робокасса не настроена: пароль #1 ({mode}) пуст в .env"
        )

    # Сумма всегда с двумя знаками после запятой
    out_sum = f"{float(amount):.2f}"
    inv_id = str(order_id)

    # Email для чека 54-ФЗ — обязателен для самозанятого
    email = customer_email or settings.ROBOKASSA_FALLBACK_EMAIL

    # Receipt — JSON в base64-кодированной форме (для URL) + raw для подписи
    receipt_dict = _build_receipt(amount, description)
    receipt_json = _receipt_encoded(receipt_dict)

    # Подпись: MerchantLogin:OutSum:InvId:Receipt:Password1
    # Если Receipt не используется — без него. У нас всегда есть.
    signature_str = f"{settings.ROBOKASSA_LOGIN}:{out_sum}:{inv_id}:{receipt_json}:{pwd1}"
    signature = _hash(signature_str)

    params = {
        "MerchantLogin": settings.ROBOKASSA_LOGIN,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": description[:100],
        "SignatureValue": signature,
        "Receipt": receipt_json,
        "Email": email,
        "Culture": "ru",
    }
    if customer_phone:
        phone_clean = "".join(c for c in customer_phone if c.isdigit() or c == "+")
        params["Phone"] = phone_clean
    if settings.ROBOKASSA_IS_TEST:
        params["IsTest"] = "1"

    payment_url = f"{ROBOKASSA_PAY_URL}?{urlencode(params, safe=':,/[]\"')}"

    logger.info(
        f"Создан URL оплаты Робокасса: order_id={order_id} amount={amount} "
        f"is_test={settings.ROBOKASSA_IS_TEST}"
    )

    # payment_id для совместимости с прежним интерфейсом — используем inv_id
    # (Робокасса возвращает уникальный OperationId только в Result-уведомлении)
    return {
        "payment_id": inv_id,
        "confirmation_url": payment_url,
        "status": "pending",
    }


# ─── Проверка Result URL подписи ────────────────────────────────────────────

def verify_result_signature(out_sum: str, inv_id: str, signature: str) -> bool:
    """
    Проверяет подпись Result URL.
    Подпись = HASH(OutSum:InvId:Password2)

    :return: True если подпись корректна
    """
    pwd2 = _password_2()
    if not pwd2:
        logger.error("Робокасса: пароль #2 не задан, проверка подписи невозможна")
        return False
    expected_str = f"{out_sum}:{inv_id}:{pwd2}"
    expected = _hash(expected_str)
    return expected.upper() == (signature or "").upper()


def parse_result_params(data: dict) -> tuple[str, float, bool]:
    """
    Разбирает параметры Result URL от Робокассы (POST или GET).

    :param data: dict параметров (form-encoded или query-string)
    :return: (inv_id, out_sum, is_signature_valid)
    :raises ValueError: если обязательных параметров нет
    """
    out_sum = (data.get("OutSum") or "").strip()
    inv_id = (data.get("InvId") or "").strip()
    signature = (data.get("SignatureValue") or "").strip()

    if not out_sum or not inv_id or not signature:
        raise ValueError(
            "Robokassa Result: отсутствуют обязательные параметры "
            "(OutSum, InvId, SignatureValue)"
        )

    is_valid = verify_result_signature(out_sum, inv_id, signature)
    try:
        amount = float(out_sum)
    except ValueError as e:
        raise ValueError(f"Неверный формат OutSum: {out_sum}") from e

    return inv_id, amount, is_valid


def build_result_response(inv_id: str) -> str:
    """
    Формирует обязательный ответ Робокассе на Result URL.
    Без этого ответа Робокасса будет ретраить запрос.
    Формат: `OK<InvId>`
    """
    return f"OK{inv_id}"


# ─── Возврат средств ─────────────────────────────────────────────────────────

async def refund_payment(
    inv_id: str,
    amount: float,
    description: Optional[str] = None,
) -> dict:
    """
    Возврат средств через Робокассу.

    Робокасса не имеет публичного API для автоматических возвратов на низких
    тарифах — возвраты делаются вручную через личный кабинет Робокассы:
    https://partner.robokassa.ru/Cancel

    Эта функция — заглушка, которая логирует попытку возврата для админа.
    """
    logger.warning(
        f"⚠️ Робокасса: автоматический возврат не поддерживается. "
        f"Сделайте возврат вручную через личный кабинет: "
        f"https://partner.robokassa.ru/Cancel | inv_id={inv_id} amount={amount}"
    )
    return {
        "refund_id": None,
        "status": "manual_required",
        "message": (
            "Автоматический возврат не настроен. Сделайте возврат вручную "
            "в личном кабинете Робокассы: https://partner.robokassa.ru/Cancel"
        ),
    }


# ─── Обратная совместимость со старым кодом ─────────────────────────────────
# Старые вызовы create_yukassa_payment / refund_yukassa_payment ведут сюда,
# чтобы не ломать импорты в bot/handlers/.

async def create_yukassa_payment(
    order_id: int,
    amount: float,
    description: str,
    return_url: Optional[str] = None,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None,
) -> dict:
    """DEPRECATED. Используйте create_payment_url. Сохранено для совместимости."""
    return await create_payment_url(
        order_id=order_id,
        amount=amount,
        description=description,
        customer_email=customer_email,
        customer_phone=customer_phone,
    )


async def refund_yukassa_payment(
    payment_id: str,
    amount: float,
    description: Optional[str] = None,
) -> dict:
    """DEPRECATED. Используйте refund_payment."""
    return await refund_payment(inv_id=payment_id, amount=amount, description=description)


def parse_webhook(data: dict) -> tuple[str, str, str]:
    """
    DEPRECATED — этот формат был для ЮКассы.
    Для Робокассы используйте parse_result_params.
    Оставлено для тестов и обратной совместимости.
    """
    raise NotImplementedError(
        "parse_webhook больше не используется — переход на Робокассу. "
        "Используйте parse_result_params(data) для новой логики."
    )
