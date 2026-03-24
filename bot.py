"""
bot.py — Telegram-бот на aiogram 3 (polling).

User flow:
  1. /start          → приветствие
  2. /digest         → бот просит ввести канал и тему
  3. Пользователь пишет: @bbcrussian искусственный интеллект
  4. Бот парсит канал за 24 ч, фильтрует, суммаризирует, отправляет дайджест

Состояния (FSM):
  WaitingInput → пользователь вводит "канал тема"
"""

import asyncio
import logging
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from crawler import fetch_messages
from llm import filter_by_topic, summarize

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Лимит Telegram на длину одного сообщения
TG_MESSAGE_LIMIT = 4096


# ─── FSM-состояния ────────────────────────────────────────────────────────────

class DigestStates(StatesGroup):
    waiting_input = State()   # ждём "канал тема" от пользователя


# ─── Хэндлеры ─────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я делаю дайджест новостей из Telegram-каналов.\n\n"
        "Команды:\n"
        "/digest — получить дайджест за сегодня\n"
        "/help   — подробная справка"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Как пользоваться:</b>\n\n"
        "1. Введи /digest\n"
        "2. Отправь сообщение в формате:\n"
        "   <code>@канал тема для поиска</code>\n\n"
        "Пример:\n"
        "   <code>@bbcrussian искусственный интеллект</code>\n\n"
        "Я найду все новости за последние 24 часа и сделаю выжимку 📰",
        parse_mode="HTML"
    )


@dp.message(Command("digest"))
async def cmd_digest(message: Message, state: FSMContext):
    await state.set_state(DigestStates.waiting_input)
    await message.answer(
        "Введи канал и тему в формате:\n"
        "<code>@канал тема для поиска</code>\n\n"
        "Например: <code>@bbcrussian искусственный интеллект</code>",
        parse_mode="HTML"
    )


@dp.message(DigestStates.waiting_input)
async def process_digest_input(message: Message, state: FSMContext):
    """
    Основной хэндлер: парсит ввод, запускает пайплайн, отправляет дайджест.
    """
    await state.clear()

    # ── 1. Парсим ввод ─────────────────────────────────────────────────────
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "⚠️ Неверный формат. Нужно: <code>@канал тема</code>\n"
            "Пример: <code>@bbcrussian крипта</code>",
            parse_mode="HTML"
        )
        return

    channel, topic = parts[0], parts[1]
    channel = channel.lstrip("@")  # telethon принимает без @

    # ── 2. Сообщаем пользователю что начали работу ─────────────────────────
    status_msg = await message.answer(
        f"⏳ Собираю новости из @{channel} по теме «{topic}»...\n"
        f"Это может занять 10–30 секунд."
    )

    # ── 3. Пайплайн ────────────────────────────────────────────────────────
    try:
        # Шаг 1: получаем сообщения за 24 ч
        await bot.edit_message_text(
            f"📥 Загружаю сообщения из @{channel}...",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id
        )
        raw_messages = await fetch_messages(channel, hours=24)

        if not raw_messages:
            await status_msg.edit_text(
                f"😔 В канале @{channel} за последние 24 часа нет сообщений."
            )
            return

        # Шаг 2: фильтрация по теме
        await bot.edit_message_text(
            f"🔍 Фильтрую {len(raw_messages)} сообщений по теме «{topic}»...",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id
        )
        # filter_by_topic — синхронная (локальные эмбеддинги, без API)
        relevant = filter_by_topic(raw_messages, topic)

        if not relevant:
            await status_msg.edit_text(
                f"🤷 По теме «{topic}» в @{channel} за 24 часа ничего не нашлось.\n"
                f"Попробуй другую тему или канал."
            )
            return

        # Шаг 3: суммаризация
        await bot.edit_message_text(
            f"✍️ Суммаризирую {len(relevant)} новостей...",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id
        )
        digest = await summarize(relevant, topic)

    except Exception as e:
        logging.exception("Ошибка пайплайна")
        await status_msg.edit_text(
            f"❌ Что-то пошло не так: {e}\n"
            "Проверь, что канал публичный и username написан верно."
        )
        return

    # ── 4. Форматируем и отправляем дайджест ───────────────────────────────
    await status_msg.delete()
    await send_digest(message, channel, topic, digest)


async def send_digest(message: Message, channel: str, topic: str, digest: list[dict]):
    """
    Форматирует дайджест и отправляет его.
    Разбивает на несколько сообщений если длина > 4096 символов.
    """
    header = (
        f"📰 <b>Дайджест @{channel}</b>\n"
        f"🔎 Тема: {topic}\n"
        f"📊 Найдено новостей: {len(digest)}\n"
        f"{'─' * 30}\n\n"
    )

    items = []
    for i, item in enumerate(digest, 1):
        items.append(
            f"{i}. {item['summary']}\n"
            f"   <a href='{item['url']}'>🔗 источник</a> · {item['date']}\n"
        )

    # Разбиваем на части если не влезает в одно сообщение
    chunks = _split_into_chunks(header, items, TG_MESSAGE_LIMIT)
    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


def _split_into_chunks(header: str, items: list[str], limit: int) -> list[str]:
    """
    Разбивает список новостей на части, каждая не длиннее `limit` символов.
    Первая часть содержит header.
    """
    chunks = []
    current = header

    for item in items:
        if len(current) + len(item) > limit:
            chunks.append(current)
            current = item
        else:
            current += item

    if current:
        chunks.append(current)

    return chunks


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    logging.info("Бот запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
