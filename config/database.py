# config/database.py

from contextlib import contextmanager
from typing import AsyncGenerator, Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from config.settings import settings

# Ensure the database URL uses the asyncpg driver for async operations
ASYNC_DATABASE_URL = str(settings.db.async_url)
# Standard psycopg2 driver for synchronous Celery operations
SYNC_DATABASE_URL = str(settings.db.sync_url)

# 1. ASYNC ENGINE & SESSION (For FastAPI endpoints & async workers)
async_engine: AsyncEngine = create_async_engine(
    ASYNC_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.db.pool_size,
    max_overflow=settings.db.max_overflow,
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# 2. SYNC ENGINE & SESSION (For Celery Tasks)
sync_engine = create_engine(
    SYNC_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.db.pool_size,
    max_overflow=settings.db.max_overflow,
    pool_recycle=1800,
)

SyncSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


# --- INITIALIZATION UTILS ---


# async def init_db() -> None:
#     async with async_engine.begin() as conn:
#         await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await async_engine.dispose()


# --- DATABASE DEPENDENCIES ---


# Dependency for FastAPI async routes
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# Context manager dependency for Celery Tasks
@contextmanager
def get_sync_db() -> Generator[Session, None, None]:
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
