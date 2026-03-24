"""
llm.py — фильтрация и суммаризация новостей через Google Gemini.

Пайплайн:
  1. filter_by_topic()  — фильтрация через Gemini (точная, понимает контекст и синонимы)
  2. summarize()        — суммаризация через Gemini (1-2 предложения на новость)

Почему Gemini справится с большим объёмом:
  Gemini 2.5 Flash поддерживает контекстное окно 1 048 576 токенов.
  100 новостей по 200 символов ≈ 20 000 токенов — это ~2% от лимита.
  Весь список новостей за день влезает в ОДИН запрос без батчей.

Бесплатный тариф: 10 RPM / 250 запросов в день.
Ключ: https://aistudio.google.com/apikey (только Google-аккаунт, без карты)
"""

import json
import asyncio
import logging
import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

logger = logging.getLogger(__name__)

# ─── Настройка клиента ────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-2.5-flash"

FILTER_CONFIG = genai.types.GenerationConfig(temperature=0.0)   # детерминизм для фильтра
SUMMARY_CONFIG = genai.types.GenerationConfig(temperature=0.3)  # чуть живее для суммари

gemini = genai.GenerativeModel(MODEL_NAME)

# Бесплатный тариф: 10 RPM → пауза между запросами
RATE_LIMIT_DELAY = 7.0

# Суммаризация: батчи по 20 новостей (фильтрация — всегда одним запросом)
BATCH_SIZE = 20


# ─── Вспомогательные функции ──────────────────────────────────────────────────

async def _generate(prompt: str, config: genai.types.GenerationConfig) -> str:
    """
    Асинхронная обёртка над синхронным Gemini SDK.
    Запускает в executor чтобы не блокировать event loop бота.
    При 429 (rate limit) — ждёт и повторяет, до 3 попыток.
    """
    loop = asyncio.get_event_loop()

    for attempt in range(3):
        try:
            response = await loop.run_in_executor(
                None,
                lambda: gemini.generate_content(prompt, generation_config=config)
            )
            return response.text

        except Exception as e:
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str or "rate" in error_str:
                wait = RATE_LIMIT_DELAY * (attempt + 1)
                logger.warning(f"Rate limit от Gemini, жду {wait:.0f}с (попытка {attempt+1}/3)")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Ошибка Gemini API: {e}")
                raise

    raise RuntimeError("Gemini API: превышен лимит попыток (rate limit)")


def _parse_json_response(raw: str) -> list | dict:
    """
    Парсит JSON из ответа Gemini.
    Gemini часто оборачивает ответ в ```json ... ``` — чистим это.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]).strip()
    return json.loads(cleaned)


# ─── Шаг 1: Фильтрация через Gemini ─────────────────────────────────────────

async def filter_by_topic(messages: list[dict], topic: str) -> list[dict]:
    """
    Фильтрует сообщения по теме через Gemini — один запрос на весь список.

    Преимущества перед эмбеддингами:
      - Понимает синонимы, контекст, косвенные упоминания
      - Не требует настройки порога — модель сама решает что релевантно
      - Огромный контекст (1M токенов) позволяет отправить все новости за день сразу

    Запрос считается дешёвым: отправляем только первые 300 символов каждого поста,
    получаем обратно только массив id — минимум выходных токенов.

    Args:
        messages: список сообщений из crawler.fetch_messages()
        topic:    тема пользователя (например, "искусственный интеллект")

    Returns:
        Отфильтрованный список в исходном порядке
    """
    if not messages:
        return []

    # Отправляем только id + первые 300 символов — экономим токены
    items = [
        {"id": m["id"], "text": m["text"][:300]}
        for m in messages
    ]

    prompt = f"""Ты — фильтр новостей. Пользователь ищет новости по теме: "{topic}".

Вот список новостей в формате JSON:
{json.dumps(items, ensure_ascii=False, indent=2)}

Задача: верни JSON-массив id новостей, которые относятся к теме "{topic}".
Включай новости, которые хотя бы косвенно связаны с темой.
Исключай только те, что явно про другое.

Формат ответа — строго JSON-массив чисел, без пояснений:
[12345, 12346, 12350]

Если ничего не подходит — верни пустой массив: []"""

    try:
        raw = await _generate(prompt, FILTER_CONFIG)
        parsed = _parse_json_response(raw)

        if isinstance(parsed, list):
            relevant_ids = set(parsed)
        elif isinstance(parsed, dict):
            # На случай если Gemini обернул в {"ids": [...]}
            relevant_ids = set(next(iter(parsed.values())))
        else:
            return messages  # fallback: не теряем данные

        filtered = [m for m in messages if m["id"] in relevant_ids]
        logger.info(f"filter_by_topic: {len(messages)} → {len(filtered)} сообщений")

        await asyncio.sleep(RATE_LIMIT_DELAY)  # пауза перед следующим запросом
        return filtered

    except Exception as e:
        logger.error(f"filter_by_topic упал: {e}, возвращаю все сообщения")
        return messages  # fallback: лучше показать лишнее, чем потерять нужное


# ─── Шаг 2: Суммаризация ─────────────────────────────────────────────────────

async def summarize(messages: list[dict], topic: str) -> list[dict]:
    """
    Делает 1-2 предложения на каждую новость.
    Батчи по BATCH_SIZE с паузой между ними (соблюдение rate limit 10 RPM).
    """
    if not messages:
        return []

    results = []
    total_batches = (len(messages) + BATCH_SIZE - 1) // BATCH_SIZE

    for i, batch_start in enumerate(range(0, len(messages), BATCH_SIZE)):
        batch = messages[batch_start : batch_start + BATCH_SIZE]
        logger.info(f"Суммаризирую батч {i+1}/{total_batches} ({len(batch)} новостей)")

        batch_results = await _summarize_batch(batch, topic)
        results.extend(batch_results)

        if i < total_batches - 1:
            await asyncio.sleep(RATE_LIMIT_DELAY)

    return results


async def _summarize_batch(batch: list[dict], topic: str) -> list[dict]:
    """Суммаризирует один батч. При ошибке парсинга — fallback на оригинал."""
    items = [
        {"id": m["id"], "text": m["text"][:500]}
        for m in batch
    ]

    prompt = f"""Ты — новостной редактор. Контекст: пользователь интересуется темой "{topic}".

Суммаризируй каждую новость в 1-2 предложения:
- Сохраняй конкретные факты, цифры, имена
- Пиши нейтрально, без оценок
- Язык: русский

Новости:
{json.dumps(items, ensure_ascii=False, indent=2)}

Верни строго JSON-массив без пояснений:
[
  {{"id": 12345, "summary": "Краткое изложение первой новости."}},
  {{"id": 12346, "summary": "Краткое изложение второй новости."}}
]"""

    try:
        raw = await _generate(prompt, SUMMARY_CONFIG)
        parsed = _parse_json_response(raw)

        if isinstance(parsed, list):
            summaries_list = parsed
        elif isinstance(parsed, dict):
            summaries_list = next(iter(parsed.values()))
        else:
            raise ValueError("Неожиданный формат ответа")

        summary_map = {
            int(item["id"]): item["summary"]
            for item in summaries_list
            if "id" in item and "summary" in item
        }

    except Exception as e:
        logger.warning(f"_summarize_batch: ошибка ({e}), fallback на оригинальный текст")
        summary_map = {m["id"]: m["text"][:150].rstrip() + "..." for m in batch}

    return [
        {
            "summary": summary_map.get(m["id"], m["text"][:150] + "..."),
            "url": m["url"],
            "date": m["date"],
        }
        for m in batch
    ]