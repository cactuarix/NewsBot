# 📰 News Digest Bot

Telegram-бот, который парсит новостные каналы через Telethon и делает
AI-дайджест по заданной теме.

## Архитектура

```
bot.py       — aiogram 3, polling, FSM для диалога
crawler.py   — Telethon, получение сообщений из TG-каналов
llm.py       — OpenAI: фильтрация по теме + суммаризация
```

### Пайплайн

```
Пользователь: @bbcrussian искусственный интеллект
      ↓
crawler.py   fetch_messages()      → все посты за 24 ч
      ↓
llm.py       filter_by_topic()     → только релевантные (1 LLM-запрос)
      ↓
llm.py       summarize()           → 1-2 предложения на каждую новость
      ↓
bot.py       send_digest()         → отправка пользователю с ссылками
```

## Быстрый старт

### 1. Получи credentials

- **Bot token** → @BotFather в Telegram
- **API ID + Hash** → https://my.telegram.org → "API development tools"
- **OpenAI key** → https://platform.openai.com/api-keys

### 2. Установи зависимости

```bash
pip install -r requirements.txt
```

### 3. Настрой .env

```bash
cp .env.example .env
# заполни все значения в .env
```

### 4. Первый запуск (авторизация Telethon)

```bash
python crawler.py
```

Telethon попросит ввести код из Telegram. После этого создастся файл
`digest_session.session` — он содержит сессию, не коммить его в git!

### 5. Запуск бота

```bash
python bot.py
```

## Структура диалога

```
/start  → приветствие
/digest → бот просит: "@канал тема"
          пользователь: "@bbcrussian крипта"
          бот: ⏳ ... → 📰 дайджест с ссылками на источники
```

## Подводные камни

| Проблема | Решение |
|---|---|
| FloodWait от Telethon | asyncio.sleep(0.05) между сообщениями |
| LLM вернула не JSON | Fallback на оригинальный текст |
| Сообщение > 4096 символов | _split_into_chunks() в bot.py |
| Приватный канал | Аккаунт должен быть подписчиком |

## Возможные улучшения

- [ ] Поддержка нескольких каналов сразу
- [ ] Выбор периода (1д / 7д / 30д)
- [ ] Сохранение настроек пользователя (SQLite)
- [ ] Ежедневная рассылка по расписанию (APScheduler)
- [ ] Пользователь добавляет свои каналы
