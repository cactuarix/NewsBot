# 📰 News Digest Bot

Telegram-бот, который парсит новостные каналы через Telethon и делает
AI-дайджест по заданной теме.

## Архитектура

```
bot.py       — aiogram 3, polling, FSM для диалога
crawler.py   — Telethon, получение сообщений из TG-каналов
llm.py       — OpenAI: фильтрация по теме + суммаризация
```