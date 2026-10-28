"""
Rate limiting через slowapi.

Лимиты:
- /admin/login (POST): 5 попыток в минуту с одного IP
- /admin/verify (POST): 10 попыток в минуту с одного IP
- Остальные защищённые роуты: достаточно сессионной проверки
"""
from slowapi import Limiter
from slowapi.util import get_remote_address


# Лимиты определяются inline в декораторах роутов.
# Например: @limiter.limit("5/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])


def get_client_ip(request) -> str:
    """
    Получает IP клиента. Учитывает заголовки X-Forwarded-For от nginx.
    """
    # При проксировании nginx → uvicorn, X-Real-IP содержит реальный IP клиента
    forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "")
    if forwarded:
        # X-Forwarded-For может содержать список IP через запятую (первый = клиент)
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)
