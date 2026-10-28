"""
Инициализация асинхронного подключения к SQLite через SQLAlchemy + aiosqlite
"""
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# Путь к SQLite-базе (по умолчанию bot.db в корне проекта; в Docker — data/bot.db)
DB_PATH = os.getenv("DB_PATH", "bot.db")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_async_engine(
    f"sqlite+aiosqlite:///{DB_PATH}",
    echo=False,          # True — выводит все SQL-запросы в консоль (для дебага)
)

# Фабрика асинхронных сессий
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей"""
    pass


async def init_db() -> None:
    """Создаёт все таблицы при первом запуске (если не существуют)"""
    # Импортируем модели здесь, чтобы они были зарегистрированы в Base.metadata
    import db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Контекстный менеджер для получения сессии (для использования в хендлерах)"""
    async with AsyncSessionLocal() as session:
        yield session
