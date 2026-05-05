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

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

**Linux / macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполни `.env`, затем:

```bash
python src/bot.py
```

При старте бот пишет в лог, доступна ли Ollama и есть ли выбранная модель в `ollama list`.

## 4) Бесплатный AI-режим (без платных API)

Используется локальный Ollama (HTTP API `/api/generate`, см. `_ask_ollama` в `src/bot.py`):

1. Установи [Ollama](https://ollama.com/) (на Windows/macOS — установщик; на Ubuntu: `curl -fsSL https://ollama.com/install.sh | sh`).
2. Скачай модель:
   - `ollama pull llama3.1:8b`
3. Убедись, что API слушает `OLLAMA_BASE_URL` (по умолчанию `http://127.0.0.1:11434`). Если Ollama на другой машине — укажи её URL в `.env` (и открой порт в firewall при необходимости).
4. В `.env`:
   - `OLLAMA_BASE_URL=http://127.0.0.1:11434`
   - `OLLAMA_MODEL=llama3.1:8b`

## 5) Обновление на сервере (Ubuntu)

Из каталога с клоном репозитория:

```bash
git pull origin master
source .venv/bin/activate
pip install -r requirements.txt
deactivate
sudo systemctl restart notion-bot
sudo systemctl status notion-bot
```

Имя юнита `notion-bot` замени на своё, если настраивал иначе. Логи: `journalctl -u notion-bot -n 100 --no-pager`.

Зависимость `python-telegram-bot[job-queue]` подтягивает `pytz` и `APScheduler` — без этого `JobQueue` не создаётся и бот мог падать при старте напоминаний.

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
