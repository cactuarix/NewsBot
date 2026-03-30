"""
crawler.py — сбор сообщений из Telegram-канала через Telethon (MTProto).

Telethon работает от ЛИЧНОГО аккаунта, не от бота.
При первом запуске запросит код подтверждения из Telegram.
Сессия сохраняется в файл digest_session.session — не коммитить в git!
"""

import asyncio
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient
from telethon.tl.types import Message
import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")

# Клиент — один на всё приложение, переиспользуется

_client = None
_client_lock = asyncio.Lock()

async def get_client():
    global _client
    async with _client_lock:  # только один корутин инициализирует клиент
        if _client is None or not _client.is_connected():
            _client = TelegramClient("digest_session", API_ID, API_HASH)
            await _client.start(phone=PHONE)
    return _client


async def fetch_messages(channel: str, hours: int = 24) -> list[dict]:
    """
    Получает сообщения из канала за последние `hours` часов.

    Args:
        channel: username канала, например "bbcrussian" или "@bbcrussian"
        hours:   глубина выборки в часах (по умолчанию — сутки)

    Returns:
        Список словарей: [{"id": ..., "date": ..., "text": ..., "url": ...}, ...]
    """
    client = await get_client()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    messages = []
    async for msg in client.iter_messages(channel, offset_date=None, reverse=False):
        if not isinstance(msg, Message):
            continue
        if msg.date < since:
            # iter_messages идёт от новых к старым → как только вышли за окно, стоп
            break
        if not msg.text or len(msg.text.strip()) < 30:
            # Пропускаем пустые/очень короткие сообщения (стикеры, фото без подписи)
            continue

        # Небольшая пауза, чтобы не триггерить flood-wait
        await asyncio.sleep(0.05)

        messages.append({
            "id": msg.id,
            "date": msg.date.strftime("%H:%M"),  # для отображения пользователю
            "text": msg.text,
            "url": f"https://t.me/{channel.lstrip('@')}/{msg.id}",
        })

    return messages
