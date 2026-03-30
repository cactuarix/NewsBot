"""
bot.py — Telegram-бот на aiogram 3 (polling).

User flow:
  /start             → главное меню
  📰 Новый дайджест  → выбор каналов → выбор темы → дайджест
  ⚙️ Мои каналы      → список каналов + добавить/удалить

FSM-состояния:
  select_channels  — выбор каналов (inline чекбоксы)
  select_topic     — выбор темы (inline кнопки)
  enter_topic      — ввод своей темы текстом
  add_channel      — ввод username нового канала
"""

import asyncio
import logging
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

from crawler import fetch_messages
from llm import filter_by_topic, deduplicate, summarize
from db import init_db, get_user_channels, add_user_channel, remove_user_channel

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

TG_MESSAGE_LIMIT = 4096


DEFAULT_CHANNELS = [
]

POPULAR_TOPICS = [
    "Политика", "Экономика", "Технологии",
    "Спорт",    "Наука",     "Война",
]


# ─── FSM ──────────────────────────────────────────────────────────────────────

class DigestStates(StatesGroup):
    select_channels = State()
    select_topic    = State()
    enter_topic     = State()
    add_channel     = State()   # пользователь вводит username нового канала


# ─── Хелпер: все каналы пользователя (дефолт + свои) ─────────────────────────

async def get_all_channels(user_id: int) -> list[dict]:
    """Объединяет дефолтные каналы и пользовательские."""
    user_channels = await get_user_channels(user_id)
    # Дефолтные идут первыми; убираем дубли по username
    user_usernames = {ch["username"] for ch in user_channels}
    combined = [ch for ch in DEFAULT_CHANNELS if ch["username"] not in user_usernames]
    combined.extend(user_channels)
    return combined


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📰 Новый дайджест")],
            [KeyboardButton(text="⚙️ Мои каналы")],
        ],
        resize_keyboard=True,
    )


def channels_kb(all_channels: list[dict], selected: set[str]) -> InlineKeyboardMarkup:
    """
    Inline-клавиатура выбора каналов.
    Пользовательские каналы помечены звёздочкой ★.
    """
    buttons = []
    default_usernames = {ch["username"] for ch in DEFAULT_CHANNELS}

    for ch in all_channels:
        mark = "✅" if ch["username"] in selected else "☐"
        star = "" if ch["username"] in default_usernames else " ★"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {ch['title']}{star}",
            callback_data=f"ch:{ch['username']}",
        )])

    all_selected = len(selected) == len(all_channels)
    buttons.append([InlineKeyboardButton(
        text="✅ Выбрать все" if not all_selected else "☐ Снять все",
        callback_data="ch:all",
    )])
    buttons.append([InlineKeyboardButton(
        text="➡️ Далее",
        callback_data="ch:done",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def topics_kb() -> InlineKeyboardMarkup:
    buttons = []
    for i in range(0, len(POPULAR_TOPICS), 2):
        row = [InlineKeyboardButton(
            text=POPULAR_TOPICS[i],
            callback_data=f"topic:{POPULAR_TOPICS[i]}",
        )]
        if i + 1 < len(POPULAR_TOPICS):
            row.append(InlineKeyboardButton(
                text=POPULAR_TOPICS[i + 1],
                callback_data=f"topic:{POPULAR_TOPICS[i + 1]}",
            ))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✏️ Своя тема", callback_data="topic:custom")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def manage_channels_kb(user_channels: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура управления каналами: список с кнопками удаления + кнопка добавить."""
    buttons = []
    for ch in user_channels:
        buttons.append([
            InlineKeyboardButton(text=f"@{ch['username']} — {ch['title']}", callback_data="noop"),
            InlineKeyboardButton(text="🗑", callback_data=f"del:{ch['username']}"),
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Хэндлеры: старт ─────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я собираю дайджест новостей из Telegram-каналов по твоей теме.\n\n"
        "Нажми кнопку чтобы начать 👇",
        reply_markup=main_menu_kb(),
    )


# ─── Хэндлеры: новый дайджест ────────────────────────────────────────────────

@dp.message(F.text == "📰 Новый дайджест")
async def start_digest(message: Message, state: FSMContext):
    await state.set_state(DigestStates.select_channels)
    await state.update_data(selected_channels=set())

    all_channels = await get_all_channels(message.from_user.id)
    await message.answer(
        "📡 <b>Выбери каналы для поиска</b> (можно несколько):\n"
        "<i>★ — твои каналы</i>",
        reply_markup=channels_kb(all_channels, set()),
        parse_mode="HTML",
    )


@dp.callback_query(DigestStates.select_channels, F.data.startswith("ch:"))
async def handle_channel_selection(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: set = data.get("selected_channels", set())
    action = cb.data.split(":", 1)[1]
    all_channels = await get_all_channels(cb.from_user.id)

    if action == "done":
        if not selected:
            await cb.answer("⚠️ Выбери хотя бы один канал!", show_alert=True)
            return
        await state.update_data(selected_channels=selected)
        await state.set_state(DigestStates.select_topic)
        channel_list = ", ".join(f"@{ch}" for ch in selected)
        await cb.message.edit_text(
            f"📡 Каналы: {channel_list}\n\n🔎 <b>Выбери тему дайджеста:</b>",
            reply_markup=topics_kb(),
            parse_mode="HTML",
        )

    elif action == "all":
        if len(selected) == len(all_channels):
            selected = set()
        else:
            selected = {ch["username"] for ch in all_channels}
        await state.update_data(selected_channels=selected)
        await cb.message.edit_reply_markup(reply_markup=channels_kb(all_channels, selected))

    else:
        if action in selected:
            selected.discard(action)
        else:
            selected.add(action)
        await state.update_data(selected_channels=selected)
        await cb.message.edit_reply_markup(reply_markup=channels_kb(all_channels, selected))

    await cb.answer()


# ─── Хэндлеры: выбор темы ────────────────────────────────────────────────────

@dp.callback_query(DigestStates.select_topic, F.data.startswith("topic:"))
async def handle_topic_selection(cb: CallbackQuery, state: FSMContext):
    topic_value = cb.data.split(":", 1)[1]
    if topic_value == "custom":
        await state.set_state(DigestStates.enter_topic)
        await cb.message.edit_text(
            "✏️ Напиши свою тему для поиска новостей:\n\n"
            "<i>Например: искусственный интеллект, криптовалюта, выборы...</i>",
            parse_mode="HTML",
        )
    else:
        await cb.message.edit_reply_markup(reply_markup=None)
        await run_pipeline(cb.message, state, topic_value)
    await cb.answer()


@dp.message(DigestStates.enter_topic)
async def handle_custom_topic(message: Message, state: FSMContext):
    topic = message.text.strip()
    if len(topic) < 2:
        await message.answer("⚠️ Тема слишком короткая, попробуй ещё раз:")
        return
    await run_pipeline(message, state, topic)


# ─── Хэндлеры: управление каналами ───────────────────────────────────────────

@dp.message(F.text == "⚙️ Мои каналы")
async def manage_channels(message: Message, state: FSMContext):
    await state.clear()
    user_channels = await get_user_channels(message.from_user.id)

    if user_channels:
        text = "📋 <b>Твои каналы:</b>\n\nНажми 🗑 чтобы удалить или добавь новый."
    else:
        text = "У тебя пока нет добавленных каналов.\nДобавь свои — они появятся в выборе при создании дайджеста."

    await message.answer(
        text,
        reply_markup=manage_channels_kb(user_channels),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "add_channel")
async def prompt_add_channel(cb: CallbackQuery, state: FSMContext):
    await state.set_state(DigestStates.add_channel)
    await cb.message.edit_text(
        "Введи username канала:\n\n"
        "<i>Например: @durov или просто durov</i>\n\n"
        "Канал должен быть публичным.",
        parse_mode="HTML",
    )
    await cb.answer()


@dp.message(DigestStates.add_channel)
async def handle_add_channel(message: Message, state: FSMContext):
    """
    Пользователь ввёл username канала.
    Валидируем через Telethon — проверяем что канал существует и публичный.
    """
    from crawler import get_client
    raw = message.text.strip().lstrip("@").lower()

    # Базовая валидация username
    if not raw or len(raw) < 3 or " " in raw:
        await message.answer("⚠️ Некорректный username. Попробуй ещё раз:")
        return

    checking_msg = await message.answer(f"🔍 Проверяю @{raw}...")

    try:
        # Проверяем через Telethon что канал реально существует
        client = await get_client()
        entity = await client.get_entity(raw)
        title = getattr(entity, "title", raw)  # берём реальное название канала
    except Exception:
        await checking_msg.edit_text(
            f"❌ Канал @{raw} не найден или недоступен.\n"
            "Убедись что канал публичный и username написан верно."
        )
        await state.clear()
        return

    result = await add_user_channel(message.from_user.id, raw, title)
    await state.clear()

    if result == "ok":
        await checking_msg.edit_text(
            f"✅ Канал <b>{title}</b> (@{raw}) добавлен!\n"
            "Он появится в списке при следующем дайджесте.",
            parse_mode="HTML",
        )
    elif result == "already_exists":
        await checking_msg.edit_text(f"ℹ️ Канал @{raw} уже добавлен.")
    elif result == "limit_reached":
        await checking_msg.edit_text(
            "⚠️ Достигнут лимит каналов (10). Удали ненужные через ⚙️ Мои каналы."
        )

    await message.answer("Что дальше? 👇", reply_markup=main_menu_kb())


@dp.callback_query(F.data.startswith("del:"))
async def handle_delete_channel(cb: CallbackQuery):
    username = cb.data.split(":", 1)[1]
    await remove_user_channel(cb.from_user.id, username)

    # Обновляем клавиатуру после удаления
    user_channels = await get_user_channels(cb.from_user.id)
    if user_channels:
        text = "📋 <b>Твои каналы:</b>\n\nНажми 🗑 чтобы удалить или добавь новый."
    else:
        text = "Каналов не осталось. Добавь новые 👇"

    await cb.message.edit_text(
        text,
        reply_markup=manage_channels_kb(user_channels),
        parse_mode="HTML",
    )
    await cb.answer(f"@{username} удалён")


@dp.callback_query(F.data == "noop")
async def handle_noop(cb: CallbackQuery):
    """Заглушка для нажатия на название канала (не кнопка действия)."""
    await cb.answer()


# ─── Основной пайплайн ───────────────────────────────────────────────────────

async def run_pipeline(message: Message, state: FSMContext, topic: str):
    data = await state.get_data()
    selected_channels: set = data.get("selected_channels", set())
    await state.clear()

    channels = list(selected_channels)
    channel_list = ", ".join(f"@{ch}" for ch in channels)

    status_msg = await message.answer(
        f"⏳ Собираю новости из {channel_list}\nпо теме «{topic}»...",
        reply_markup=ReplyKeyboardRemove(),
    )

    try:
        # Шаг 1: параллельный сбор
        await _edit(status_msg, f"📥 Загружаю сообщения из {len(channels)} каналов...")
        results = await asyncio.gather(
            *[fetch_messages(ch, hours=24) for ch in channels],
            return_exceptions=True,
        )

        all_messages = []
        for ch, result in zip(channels, results):
            if isinstance(result, Exception):
                logging.warning(f"Канал @{ch} упал: {result}")
                continue
            for msg in result:
                msg["channel"] = ch
            all_messages.extend(result)

        if not all_messages:
            await _edit(status_msg,
                "😔 Не удалось получить сообщения ни из одного канала.\n"
                "Проверь, что каналы публичные."
            )
            await bot.send_message(message.chat.id, "Попробуй ещё раз 👇", reply_markup=main_menu_kb())
            return

        # Шаг 2: фильтрация
        await _edit(status_msg, f"🔍 Фильтрую {len(all_messages)} сообщений по теме «{topic}»...")
        relevant = await filter_by_topic(all_messages, topic)

        if not relevant:
            await _edit(status_msg,
                f"🤷 По теме «{topic}» ничего не нашлось за 24 часа.\n"
                "Попробуй другую тему или каналы."
            )
            await bot.send_message(message.chat.id, "Попробуй ещё раз 👇", reply_markup=main_menu_kb())
            return

        # Шаг 3: дедупликация (только при нескольких каналах)
        if len(channels) > 1 and len(relevant) > 1:
            await _edit(status_msg, f"🗂 Убираю дубли из {len(relevant)} новостей...")
            relevant = await deduplicate(relevant, topic)

        # Шаг 4: суммаризация
        await _edit(status_msg, f"✍️ Суммаризирую {len(relevant)} уникальных новостей...")
        digest = await summarize(relevant, topic)

    except Exception as e:
        logging.exception("Ошибка пайплайна")
        await _edit(status_msg, f"❌ Что-то пошло не так: {e}")
        await bot.send_message(message.chat.id, "Попробуй ещё раз 👇", reply_markup=main_menu_kb())
        return

    try:
        await status_msg.delete()
    except Exception:
        pass
    await send_digest(message, channels, topic, digest)
    await bot.send_message(message.chat.id, "Готово! Что дальше? 👇", reply_markup=main_menu_kb())


# ─── Форматирование и отправка ────────────────────────────────────────────────

async def send_digest(message: Message, channels: list, topic: str, digest: list[dict]):
    header = (
        f"📰 <b>Дайджест по теме: {topic}</b>\n"
        f"📡 Каналы: {', '.join(f'@{ch}' for ch in channels)}\n"
        f"📊 Новостей: {len(digest)}\n"
        f"{'─' * 30}\n\n"
    )
    items = []
    for i, item in enumerate(digest, 1):
        source = f"@{item.get('channel', '?')}"
        items.append(
            f"{i}. {item['summary']}\n"
            f"   <a href='{item['url']}'>🔗 {source}</a> · {item['date']}\n\n"
        )
    for chunk in _split_into_chunks(header, items, TG_MESSAGE_LIMIT):
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


def _split_into_chunks(header: str, items: list[str], limit: int) -> list[str]:
    chunks, current = [], header
    for item in items:
        if len(current) + len(item) > limit:
            chunks.append(current)
            current = item
        else:
            current += item
    if current:
        chunks.append(current)
    return chunks


async def _edit(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        pass


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    await init_db()  # создаём таблицы при старте
    logging.info("Бот запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())