"""
Главное приложение веб-админки.

Запуск:
    uvicorn web.main:app --host 127.0.0.1 --port 8081 --reload

Архитектура:
- FastAPI + Jinja2 + HTMX (без SPA)
- Авторизация: Telegram OTP (см. web/security/auth.py)
- Защита: CSRF, rate-limit, security headers
- БД: тот же SQLAlchemy/aiosqlite, что и у бота
"""
from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from config import settings
from db.session import init_db
from web.templating import templates
from web.security.headers import SecurityHeadersMiddleware
from web.security.rate_limit import limiter
from web.routes import (
    auth_routes, dashboard, orders, admin_pages,
    order_actions, refund_actions, withdrawal_actions, user_actions,
    editor_auth, editor_dashboard, editor_tasks, editor_task_actions, editor_files,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("admin.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title=f"{settings.BRAND_NAME} · Веб-платформа",
    lifespan=lifespan,
    docs_url=None,           # Отключаем Swagger UI
    redoc_url=None,          # Отключаем ReDoc
    openapi_url=None,        # Отключаем OpenAPI JSON (защита от reconnaissance)
)

# ─── Middleware ──────────────────────────────────────────────────────────────

app.add_middleware(SecurityHeadersMiddleware)

# ─── Rate-limiter ────────────────────────────────────────────────────────────

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"error": "Слишком много запросов. Попробуйте позже."},
    )


# ─── Redirect 307 (require_admin) ───────────────────────────────────────────

@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException):
    # require_admin бросает 307 с Location в headers — обрабатываем как редирект
    if exc.status_code == 307 and "Location" in exc.headers:
        return RedirectResponse(url=exc.headers["Location"], status_code=307)
    # Прочие — отдаём JSON или базовую страницу
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            content=f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
            status_code=exc.status_code,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# ─── Статика ─────────────────────────────────────────────────────────────────

app.mount("/admin/static", StaticFiles(directory="web/static"), name="admin-static")


# ─── Роуты ───────────────────────────────────────────────────────────────────

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(orders.router)
app.include_router(admin_pages.router)
app.include_router(order_actions.router)
app.include_router(refund_actions.router)
app.include_router(withdrawal_actions.router)
app.include_router(user_actions.router)

# ─── Editor panel ───────────────────────────────────────────────────────────
app.include_router(editor_auth.router)
app.include_router(editor_dashboard.router)
app.include_router(editor_tasks.router)
app.include_router(editor_task_actions.router)
app.include_router(editor_files.router)


# ─── Healthcheck ─────────────────────────────────────────────────────────────

@app.get("/health")
@app.get("/admin/health")
async def healthcheck():
    return {"status": "ok"}


# ─── Короткий редирект для inline-кнопки оплаты ─────────────────────────────
# Robokassa URL с Receipt раздут до >700 символов (лимит Telegram ~256),
# поэтому в кнопку кладём короткий /pay/{id} → редирект на полный URL.

@app.get("/pay/{payment_id}")
async def pay_redirect(payment_id: int):
    from sqlalchemy import select
    from db.session import AsyncSessionLocal
    from db.models import Payment

    async with AsyncSessionLocal() as session:
        r = await session.execute(select(Payment).where(Payment.id == payment_id))
        p = r.scalar_one_or_none()

    if p is None or not p.confirmation_url:
        return HTMLResponse(
            "<h1>Платёж не найден</h1><p>Свяжитесь с поддержкой через /support в Telegram-боте.</p>",
            status_code=404,
        )
    return RedirectResponse(p.confirmation_url, status_code=302)


# ─── Публичный лендинг и юридические страницы (White-label из .env) ──────────

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def landing_index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/oferta", response_class=HTMLResponse)
@app.get("/oferta.html", response_class=HTMLResponse)
@app.get("/agreement", response_class=HTMLResponse)
@app.get("/refund", response_class=HTMLResponse)
async def landing_oferta(request: Request):
    return templates.TemplateResponse(request, "oferta.html", {})


@app.get("/privacy", response_class=HTMLResponse)
@app.get("/privacy.html", response_class=HTMLResponse)
async def landing_privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


@app.get("/contacts", response_class=HTMLResponse)
@app.get("/contacts.html", response_class=HTMLResponse)
async def landing_contacts(request: Request):
    return templates.TemplateResponse(request, "contacts.html", {})


@app.get("/styles.css")
async def landing_styles():
    return FileResponse("landing/styles.css", media_type="text/css")


# ─── Запуск через uvicorn ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from config import settings
    uvicorn.run(
        "web.main:app",
        host="127.0.0.1",
        port=settings.ADMIN_WEB_PORT,
        log_level="info",
    )
