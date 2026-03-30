"""
llm.py — фильтрация, дедупликация и суммаризация новостей через Google Gemini.

Пайплайн (три шага):
  1. filter_by_topic()  — один запрос, отбирает релевантные новости
  2. deduplicate()      — один запрос, убирает дубли из разных каналов
  3. summarize()        — батчи по 20, суммаризирует уникальные новости

Gemini 2.5 Flash: контекст 1M токенов, бесплатно 250 запросов/день.
Типичный расход на один дайджест: 3-4 запроса.
"""

import json
import asyncio
import logging
import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

logger = logging.getLogger(__name__)

# ─── Настройка ───────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-2.5-flash"
FILTER_CONFIG = genai.types.GenerationConfig(temperature=0.0)
SUMMARY_CONFIG = genai.types.GenerationConfig(temperature=0.3)

gemini = genai.GenerativeModel(MODEL_NAME)

RATE_LIMIT_DELAY = 7.0  # 10 RPM на бесплатном тарифе → пауза между запросами
BATCH_SIZE = 20


# ─── Вспомогательные функции ─────────────────────────────────────────────────

async def _generate(prompt: str, config: genai.types.GenerationConfig) -> str:
    """Асинхронная обёртка над синхронным SDK. При 429 — ждёт и повторяет."""
    loop = asyncio.get_event_loop()
    for attempt in range(3):
        try:
            response = await loop.run_in_executor(
                None,
                lambda: gemini.generate_content(prompt, generation_config=config)
            )
            return response.text
        except Exception as e:
            if any(x in str(e).lower() for x in ("429", "quota", "rate")):
                wait = RATE_LIMIT_DELAY * (attempt + 1)
                logger.warning(f"Rate limit, жду {wait:.0f}с (попытка {attempt+1}/3)")
                await asyncio.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini: превышен лимит попыток")


def _parse_json(raw: str) -> list | dict:
    """Чистит ```json ... ``` обёртку и парсит JSON."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]).strip()
    return json.loads(cleaned)


# ─── Шаг 1: Фильтрация ───────────────────────────────────────────────────────

async def filter_by_topic(messages: list[dict], topic: str) -> list[dict]:
    """
    Один запрос к Gemini — возвращает только релевантные теме сообщения.
    Отправляем первые 300 символов каждого поста (экономия токенов).
    """
    if not messages:
        return []

    items = [{"id": m["id"], "text": m["text"][:300]} for m in messages]

    prompt = f"""Ты помогаешь пользователю найти новости по теме: "{topic}".

Ниже список новостей в формате JSON (id + текст):
{json.dumps(items, ensure_ascii=False, indent=2)}

Отбери новости, которые имеют отношение к теме "{topic}".
Трактуй тему широко — включай синонимы, смежные события, упоминания.
Исключай только те новости, которые ТОЧНО не связаны с темой.
Если сомневаешься — включай.

Верни JSON-массив числовых id отобранных новостей.
Только JSON, никаких пояснений.
Пример: [111, 222, 333]
Если совсем ничего не подходит: []"""

    try:
        raw = await _generate(prompt, FILTER_CONFIG)
        logger.debug(f"filter_by_topic raw response: {raw[:200]}")
        parsed = _parse_json(raw)
        relevant_ids = set(parsed if isinstance(parsed, list) else next(iter(parsed.values())))
        filtered = [m for m in messages if m["id"] in relevant_ids]
        logger.info(f"filter_by_topic: {len(messages)} → {len(filtered)}")
        await asyncio.sleep(RATE_LIMIT_DELAY)
        return filtered
    except Exception as e:
        logger.error(f"filter_by_topic упал: {e}, возвращаю все сообщения")
        return messages


# ─── Шаг 2: Дедупликация ─────────────────────────────────────────────────────

async def deduplicate(messages: list[dict], topic: str) -> list[dict]:
    """
    Убирает дубли — новости об одном событии из разных каналов.

    Алгоритм:
      - Отправляем все отфильтрованные новости в один запрос
      - Просим Gemini сгруппировать дубли и выбрать лучший из каждой группы
      - Возвращаем только уникальные новости (лучшие представители групп)

    "Лучший" = наиболее полный текст (больше деталей, фактов, цифр).
    Если новость уникальна — она остаётся как есть.
    """
    if len(messages) <= 1:
        return messages

    items = [
        {
            "id": m["id"],
            "channel": m.get("channel", "?"),
            "text": m["text"][:400],
        }
        for m in messages
    ]

    prompt = f"""Ты — редактор новостного дайджеста. Тема: "{topic}".

Ниже список новостей из нескольких Telegram-каналов.
Некоторые новости — об одном и том же событии, просто из разных источников.

Задача:
1. Найди группы новостей об одном событии
2. Из каждой группы оставь ОДНУ — самую полную и информативную
3. Уникальные новости (без дублей) оставь все

Новости (JSON):
{json.dumps(items, ensure_ascii=False, indent=2)}

Верни JSON-массив id новостей которые нужно ОСТАВИТЬ (по одной из каждой группы + все уникальные).
Ответ — строго JSON-массив чисел, без пояснений: [123, 456, 789]"""

    try:
        raw = await _generate(prompt, FILTER_CONFIG)
        parsed = _parse_json(raw)
        keep_ids = set(parsed if isinstance(parsed, list) else next(iter(parsed.values())))
        deduped = [m for m in messages if m["id"] in keep_ids]
        logger.info(f"deduplicate: {len(messages)} → {len(deduped)}")
        await asyncio.sleep(RATE_LIMIT_DELAY)
        return deduped
    except Exception as e:
        logger.error(f"deduplicate упал: {e}, возвращаю без дедупликации")
        return messages


# ─── Шаг 3: Суммаризация ─────────────────────────────────────────────────────

async def summarize(messages: list[dict], topic: str) -> list[dict]:
    """Батчи по BATCH_SIZE с паузой между ними."""
    if not messages:
        return []

    results = []
    total = (len(messages) + BATCH_SIZE - 1) // BATCH_SIZE

    for i, start in enumerate(range(0, len(messages), BATCH_SIZE)):
        batch = messages[start: start + BATCH_SIZE]
        logger.info(f"Суммаризирую батч {i+1}/{total} ({len(batch)} новостей)")
        results.extend(await _summarize_batch(batch, topic))
        if i < total - 1:
            await asyncio.sleep(RATE_LIMIT_DELAY)

    return results


async def _summarize_batch(batch: list[dict], topic: str) -> list[dict]:
    items = [{"id": m["id"], "text": m["text"][:500]} for m in batch]

    prompt = f"""Ты — новостной редактор. Тема пользователя: "{topic}".

Суммаризируй каждую новость в 1-2 предложения.
Сохраняй факты, цифры, имена. Пиши нейтрально. Язык: русский.

Новости:
{json.dumps(items, ensure_ascii=False, indent=2)}

Верни строго JSON-массив без пояснений:
[
  {{"id": 123, "summary": "Краткое изложение."}},
  {{"id": 456, "summary": "Другая новость."}}
]"""

    try:
        raw = await _generate(prompt, SUMMARY_CONFIG)
        parsed = _parse_json(raw)
        summaries_list = parsed if isinstance(parsed, list) else next(iter(parsed.values()))
        summary_map = {
            int(item["id"]): item["summary"]
            for item in summaries_list
            if "id" in item and "summary" in item
        }
    except Exception as e:
        logger.warning(f"_summarize_batch ошибка: {e}, fallback")
        summary_map = {m["id"]: m["text"][:150].rstrip() + "..." for m in batch}

    return [
        {
            "summary": summary_map.get(m["id"], m["text"][:150] + "..."),
            "url": m["url"],
            "date": m["date"],
            "channel": m.get("channel", "?"),
        }
        for m in batch
    ]