# Notion Telegram Assistant

Личный помощник для задач: добавляет задачи в Notion, показывает их по группам, отмечает выполненными, умеет напоминать и отвечать как нейросеть через бесплатную локальную модель.

## Что умеет

- Добавлять задачу: `/add <группа> | <задача>`
- Добавлять свободную задачу без группы: `/quick <задача>` (бот сам определяет группу и сообщает, куда записал)
- Показывать задачи по группам: `/list` или `/list <группа>`
- Показывать список групп: `/groups`
- Отмечать задачу выполненной: `/done <page_id_или_часть_названия>`
- Ставить напоминание: `/remind <минуты> | <текст>`
- Отвечать как нейросеть: `/ask <вопрос>` или просто текстом в чат
- Кнопки команд: показываются через `/start`

## 1) Подготовка Notion

1. Создай Notion Integration в [Notion Developers](https://www.notion.so/my-integrations) и скопируй токен.
2. Создай базу задач в Notion и добавь свойства:
   - `Name` (Title)
   - `Group` (Select)
   - `Status` (Status)
   - `Completed` (Checkbox)
3. Подели базу с интеграцией: `Share` -> выбери интеграцию.
4. Скопируй `database_id` из URL базы.

## 2) Подготовка Telegram

1. Создай бота через [@BotFather](https://t.me/BotFather).
2. Получи `TELEGRAM_BOT_TOKEN`.

## 3) Запуск

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Заполни `.env`, затем:

```bash
python src/bot.py
```

## 4) Бесплатный AI-режим (без платных API)

Используется локальный Ollama:

1. Установи [Ollama](https://ollama.com/).
2. Скачай модель:
   - `ollama pull llama3.1:8b`
3. Запусти Ollama (обычно он работает как сервис на `http://127.0.0.1:11434`).
4. Укажи в `.env`:
   - `OLLAMA_BASE_URL=http://127.0.0.1:11434`
   - `OLLAMA_MODEL=llama3.1:8b`

## Настройка имен колонок

Если в Notion другие названия полей, поменяй переменные в `.env`:

- `NOTION_TITLE_PROP`
- `NOTION_GROUP_PROP`
- `NOTION_STATUS_PROP`
- `NOTION_COMPLETED_PROP`
- `DEFAULT_GROUP` (группа по умолчанию, если бот не смог подобрать лучшую)
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `OLLAMA_TIMEOUT_SEC`

## Примечание

- Команда `/done` работает по `page_id` (надежно) или по части названия (если найдено одно совпадение).
- Для напоминаний используется встроенный `JobQueue` из `python-telegram-bot`.
- Если Ollama не запущен или модель не скачана, AI-ответы временно не работают.
