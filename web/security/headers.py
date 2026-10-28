"""
Security headers middleware.

Применяется ко всем ответам админ-панели. Защищает от:
- XSS (CSP)
- Clickjacking (X-Frame-Options)
- MIME-sniffing (X-Content-Type-Options)
- MITM (Strict-Transport-Security)
- Утечки реферера (Referrer-Policy)
- Излишних API браузера (Permissions-Policy)
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# CSP разрешает только наши ресурсы + CDN для Tailwind, HTMX, Lucide, Chart.js
# 'unsafe-inline' для styles нужен потому что Tailwind генерит inline стили
# и мы используем Jinja-template inlined styles
CSP_POLICY = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com https://cdn.jsdelivr.net; "
    "img-src 'self' data: https:; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)


SECURITY_HEADERS = {
    "Content-Security-Policy": CSP_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-XSS-Protection": "1; mode=block",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Добавляет заголовки безопасности ко всем ответам."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        return response
