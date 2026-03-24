"""
llm.py — фильтрация и суммаризация новостей.

Пайплайн:
  1. filter_by_topic()  — локальная фильтрация через эмбеддинги (без API, бесплатно)
  2. summarize()        — суммаризация через Google Gemini (250 запросов/день бесплатно)

Фильтрация:
  Используем sentence-transformers с моделью paraphrase-multilingual-MiniLM-L12-v2.
  Модель ~120 МБ, скачивается один раз при первом запуске, работает локально.
  Поддерживает русский язык, понимает семантику (не просто ключевые слова).

Суммаризация:
  Gemini 2.5 Flash — быстрый, бесплатный тариф.
  Ключ: https://aistudio.google.com/apikey (только Google-аккаунт, без карты)
"""

import json
import asyncio
import logging
import os
from dotenv import load_dotenv
import google.generativeai as genai
from sentence_transformers import SentenceTransformer, util

load_dotenv()

logger = logging.getLogger(__name__)

# ─── Настройка клиента ────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# gemini-2.5-flash — лучший выбор для бесплатного тарифа:
# быстрый, умный, 250 запросов/день бесплатно
MODEL_NAME = "gemini-2.5-flash"

SUMMARY_CONFIG = genai.types.GenerationConfig(temperature=0.3)

gemini = genai.GenerativeModel(MODEL_NAME)

BATCH_SIZE = 15

# Бесплатный тариф: 10 RPM = 1 запрос / 6 сек → ставим 7 сек с запасом
RATE_LIMIT_DELAY = 7.0

# ─── Модель эмбеддингов (локальная) ──────────────────────────────────────────

# Мультиязычная модель, поддерживает русский, ~120 МБ.
# Скачивается один раз при первом запуске в ~/.cache/huggingface/
EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

logger.info(f"Загружаю модель эмбеддингов {EMBEDDING_MODEL_NAME}...")
embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
logger.info("Модель эмбеддингов загружена ✅")

# Порог схожести: 0.0 — 1.0.
# 0.25 — достаточно мягко, ловит синонимы и косвенные упоминания.
# Увеличь до 0.35-0.4 если слишком много нерелевантного.
SIMILARITY_THRESHOLD = 0.5


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


# ─── Шаг 1: Фильтрация (локальные эмбеддинги, без API) ──────────────────────

def filter_by_topic(messages: list[dict], topic: str) -> list[dict]:
    """
    Фильтрует сообщения по теме через семантические эмбеддинги.
    Работает локально — без интернета, без API-ключей, без лимитов.

    Как это работает:
      1. Кодируем тему пользователя в вектор (эмбеддинг)
      2. Кодируем каждое сообщение в вектор
      3. Считаем косинусное сходство между темой и каждым сообщением
      4. Оставляем сообщения с similarity >= SIMILARITY_THRESHOLD

    Косинусное сходство:
      1.0 — одинаковые по смыслу
      0.5 — похожие темы
      0.25 — косвенная связь (наш порог)
      0.0 — несвязанные тексты

    Args:
        messages: список сообщений из crawler.fetch_messages()
        topic:    тема пользователя (например, "искусственный интеллект")

    Returns:
        Отфильтрованный список, отсортированный по релевантности (самые похожие — первыми)
    """
    if not messages:
        return []

    # Берём первые 300 символов текста — достаточно для определения темы,
    # не тратим лишнее время на кодирование длинных постов
    texts = [m["text"][:300] for m in messages]

    # encode() — синхронный, но быстрый (~1 сек на 100 новостей на CPU)
    # show_progress_bar=False чтобы не засорять логи бота
    topic_emb = embedding_model.encode(topic, convert_to_tensor=True, show_progress_bar=False)
    texts_emb = embedding_model.encode(texts, convert_to_tensor=True, show_progress_bar=False)

    # cos_sim возвращает матрицу [1 x N], берём первую строку → тензор длины N
    scores = util.cos_sim(topic_emb, texts_emb)[0]

    # Собираем пары (сообщение, score) и фильтруем по порогу
    scored = [
        (msg, float(score))
        for msg, score in zip(messages, scores)
        if float(score) >= SIMILARITY_THRESHOLD
    ]

    # Сортируем по убыванию релевантности — самые похожие идут в дайджест первыми
    scored.sort(key=lambda x: x[1], reverse=True)

    filtered = [msg for msg, _ in scored]
    logger.info(
        f"filter_by_topic: {len(messages)} → {len(filtered)} сообщений "
        f"(порог={SIMILARITY_THRESHOLD})"
    )
    return filtered


# ─── Шаг 2: Суммаризация ─────────────────────────────────────────────────────

async def summarize(messages: list[dict], topic: str) -> list[dict]:
    """
    Делает 1-2 предложения на каждую новость.
    Батчи по BATCH_SIZE с паузой между ними (соблюдение rate limit).
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
