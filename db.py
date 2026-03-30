"""
db.py — работа с SQLite через aiosqlite.

Хранит пользовательские каналы между сессиями.
База создаётся автоматически при первом запуске (файл digest.db).

Схема:
  user_channels (user_id, username, title, added_at)
"""

import aiosqlite
import logging

DB_PATH = "digest.db"
MAX_CHANNELS_PER_USER = 10  # лимит каналов на одного пользователя

logger = logging.getLogger(__name__)


async def init_db():
    """Создаёт таблицы если не существуют. Вызывается при старте бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_channels (
                user_id    INTEGER NOT NULL,
                username   TEXT    NOT NULL,
                title      TEXT    NOT NULL,
                added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, username)
            )
        """)
        await db.commit()
    logger.info("БД инициализирована ✅")


async def get_user_channels(user_id: int) -> list[dict]:
    """Возвращает список каналов пользователя, отсортированных по дате добавления."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT username, title FROM user_channels WHERE user_id = ? ORDER BY added_at",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"username": row["username"], "title": row["title"]} for row in rows]


async def add_user_channel(user_id: int, username: str, title: str) -> str:
    """
    Добавляет канал пользователю.

    Returns:
        "ok"             — успешно добавлен
        "already_exists" — канал уже есть у пользователя
        "limit_reached"  — превышен лимит MAX_CHANNELS_PER_USER
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем лимит
        async with db.execute(
            "SELECT COUNT(*) FROM user_channels WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            count = (await cursor.fetchone())[0]

        if count >= MAX_CHANNELS_PER_USER:
            return "limit_reached"

        try:
            await db.execute(
                "INSERT INTO user_channels (user_id, username, title) VALUES (?, ?, ?)",
                (user_id, username, title)
            )
            await db.commit()
            return "ok"
        except aiosqlite.IntegrityError:
            # PRIMARY KEY (user_id, username) нарушен — канал уже добавлен
            return "already_exists"


async def remove_user_channel(user_id: int, username: str):
    """Удаляет канал пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM user_channels WHERE user_id = ? AND username = ?",
            (user_id, username)
        )
        await db.commit()
