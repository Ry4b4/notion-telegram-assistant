import logging
import os
import json
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

import requests
from dotenv import load_dotenv
from notion_client import Client
from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_TIMEOUT_SEC = int(os.getenv("OLLAMA_TIMEOUT_SEC", "60"))
INTENT_CONFIDENCE_THRESHOLD = 0.65
CHAT_MEMORY_LIMIT = 10
BASIC_GROUPS = ["Работа", "Учеба", "Дом", "Здоровье", "Финансы", "Покупки", "Личное", "Inbox"]
MORNING_CHECKIN_TIME = os.getenv("MORNING_CHECKIN_TIME", "09:00")
EVENING_CHECKIN_TIME = os.getenv("EVENING_CHECKIN_TIME", "21:00")

TITLE_PROP = os.getenv("NOTION_TITLE_PROP", "Name")
GROUP_PROP = os.getenv("NOTION_GROUP_PROP", "Group")
STATUS_PROP = os.getenv("NOTION_STATUS_PROP", "Status")
COMPLETED_PROP = os.getenv("NOTION_COMPLETED_PROP", "Completed")
PRIORITY_PROP = os.getenv("NOTION_PRIORITY_PROP", "Priority")
DUE_PROP = os.getenv("NOTION_DUE_PROP", "Due")
DEFAULT_GROUP = os.getenv("DEFAULT_GROUP", "Inbox")

DONE_STATUSES = {"done", "completed", "выполнено", "готово"}

notion = Client(auth=NOTION_TOKEN)
_db_properties_cache: dict[str, Any] | None = None

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/start"), KeyboardButton("/list"), KeyboardButton("/groups")],
        [KeyboardButton("/add"), KeyboardButton("/quick"), KeyboardButton("/done")],
        [KeyboardButton("/remind"), KeyboardButton("/plan"), KeyboardButton("/ask")],
        [KeyboardButton("Скрыть кнопки")],
    ],
    resize_keyboard=True,
)


def _extract_title(page: dict[str, Any]) -> str:
    title = page["properties"].get(TITLE_PROP, {}).get("title", [])
    if not title:
        return "(без названия)"
    return "".join(chunk.get("plain_text", "") for chunk in title).strip() or "(без названия)"


def _extract_group(page: dict[str, Any]) -> str:
    prop = page["properties"].get(GROUP_PROP, {})
    select_data = prop.get("select")
    if select_data and select_data.get("name"):
        return select_data["name"]
    return "Без группы"


def _is_completed(page: dict[str, Any]) -> bool:
    props = page["properties"]
    completed_checkbox = props.get(COMPLETED_PROP, {}).get("checkbox")
    if completed_checkbox is True:
        return True
    status_name = (
        props.get(STATUS_PROP, {}).get("status", {}) or props.get(STATUS_PROP, {}).get("select", {})
    ).get("name", "")
    return status_name.strip().lower() in DONE_STATUSES


def _build_create_payload(group_name: str, task_text: str) -> dict[str, Any]:
    return _build_create_payload_with_planning(group_name, task_text, due_date=None, priority=None)


def _db_properties() -> dict[str, Any]:
    global _db_properties_cache
    if _db_properties_cache is None:
        db_info = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
        _db_properties_cache = db_info.get("properties", {})
    return _db_properties_cache or {}


def _property_exists(prop_name: str) -> bool:
    return prop_name in _db_properties()


def _build_create_payload_with_planning(
    group_name: str,
    task_text: str,
    due_date: str | None,
    priority: str | None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {TITLE_PROP: {"title": [{"text": {"content": task_text}}]}}

    if _property_exists(GROUP_PROP):
        properties[GROUP_PROP] = {"select": {"name": group_name}}
    if _property_exists(STATUS_PROP):
        status_type = _db_properties().get(STATUS_PROP, {}).get("type")
        if status_type == "status":
            properties[STATUS_PROP] = {"status": {"name": "To Do"}}
        elif status_type == "select":
            properties[STATUS_PROP] = {"select": {"name": "To Do"}}
    if _property_exists(COMPLETED_PROP):
        properties[COMPLETED_PROP] = {"checkbox": False}
    if due_date and _property_exists(DUE_PROP):
        properties[DUE_PROP] = {"date": {"start": due_date}}
    if priority and _property_exists(PRIORITY_PROP):
        priority_type = _db_properties().get(PRIORITY_PROP, {}).get("type")
        if priority_type in {"select", "status"}:
            key = "select" if priority_type == "select" else "status"
            properties[PRIORITY_PROP] = {key: {"name": priority}}

    return {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties}


def _existing_group_names() -> list[str]:
    result = notion.databases.query(database_id=NOTION_DATABASE_ID, page_size=100)
    detected = {
        _extract_group(page)
        for page in result.get("results", [])
        if _extract_group(page).strip() and _extract_group(page) != "Без группы"
    }
    detected.update(BASIC_GROUPS)
    return sorted(detected)


def _smart_pick_group(task_text: str) -> str:
    text = task_text.lower()
    groups = _existing_group_names()
    for group in groups:
        if group.lower() in text:
            return group

    keyword_map = {
        "работ": "Работа",
        "проект": "Работа",
        "код": "Работа",
        "встреч": "Работа",
        "учеб": "Учеба",
        "курс": "Учеба",
        "дом": "Дом",
        "квартир": "Дом",
        "купить": "Покупки",
        "магазин": "Покупки",
        "здоров": "Здоровье",
        "тренир": "Здоровье",
        "доктор": "Здоровье",
    }
    for key, mapped_group in keyword_map.items():
        if key in text:
            return mapped_group
    return DEFAULT_GROUP


def _smart_pick_group_with_ai(task_text: str) -> str:
    groups = _existing_group_names()
    if not groups:
        return _smart_pick_group(task_text)
    prompt = (
        "Выбери лучшую группу для задачи. Верни только название группы из списка, без пояснений.\n"
        f"Список групп: {', '.join(groups)}\n"
        f"Задача: {task_text}"
    )
    try:
        suggestion = _ask_ollama(prompt).strip().strip("\"'")
    except Exception:
        suggestion = ""
    for group in groups:
        if suggestion.lower() == group.lower():
            return group
    return _smart_pick_group(task_text)


def _ask_ollama(prompt: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": (
            "Ты полезный персональный ассистент на русском языке. "
            "Отвечай кратко и по делу.\n\n"
            f"Запрос пользователя:\n{prompt}"
        ),
        "stream": False,
    }
    response = requests.post(
        f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        json=payload,
        timeout=OLLAMA_TIMEOUT_SEC,
    )
    response.raise_for_status()
    data = response.json()
    return (data.get("response") or "").strip()


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _detect_intent(text: str) -> dict[str, Any]:
    parser_prompt = (
        "Ты анализируешь сообщение пользователя и возвращаешь только JSON без пояснений.\n"
        "Допустимые intent: add, quick, list, plan, groups, done, remind, chat.\n"
        "Формат:\n"
        '{'
        '"intent":"chat",'
        '"confidence":0.0,'
        '"needs_clarification":false,'
        '"clarification_question":"",'
        '"task_text":"",'
        '"group":"",'
        '"group_filter":"",'
        '"plan_day":"",'
        '"done_query":"",'
        '"minutes":0,'
        '"reminder_text":"",'
        '"due_date":"",'
        '"priority":"",'
        '"chat_prompt":""'
        '}\n'
        "Правила:\n"
        "- confidence от 0 до 1.\n"
        "- если не уверен в intent или не хватает данных, needs_clarification=true и задай короткий clarification_question.\n"
        "- add: если явно про добавление задачи с группой.\n"
        "- quick: если добавление задачи без явной группы.\n"
        "- list: если про показать/вывести задачи (может быть group_filter).\n"
        "- plan: если про план на дату/сегодня/завтра (используй plan_day).\n"
        "- groups: если про показать группы.\n"
        "- done: если про отметить выполнение.\n"
        "- remind: если про напоминание и есть время.\n"
        "- due_date для add/quick: дата в формате YYYY-MM-DD, если пользователь указал срок.\n"
        "- priority для add/quick: один из Низкий, Средний, Высокий, если пользователь указал приоритет.\n"
        "- chat: обычный разговор/вопрос.\n"
        f"Сообщение пользователя: {text}"
    )
    raw = _ask_ollama(parser_prompt)
    data = _extract_json(raw)
    if not data or not isinstance(data, dict):
        return {"intent": "chat", "chat_prompt": text}
    data.setdefault("intent", "chat")
    data.setdefault("confidence", 0.0)
    data.setdefault("needs_clarification", False)
    data.setdefault("clarification_question", "")
    data.setdefault("task_text", "")
    data.setdefault("group", "")
    data.setdefault("group_filter", "")
    data.setdefault("plan_day", "")
    data.setdefault("done_query", "")
    data.setdefault("minutes", 0)
    data.setdefault("reminder_text", "")
    data.setdefault("due_date", "")
    data.setdefault("priority", "")
    data.setdefault("chat_prompt", text)
    return data


def _is_affirmative(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"да", "ага", "ок", "окей", "yes", "y", "верно", "подтверждаю", "подтвердить"}


def _is_negative(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"нет", "не", "no", "n", "отмена", "cancel", "неверно"}


def _looks_like_internet_required(text: str) -> bool:
    normalized = text.strip().lower()
    internet_markers = [
        "сегодня",
        "сейчас",
        "последние новости",
        "новости",
        "курс доллара",
        "курс биткоина",
        "погода",
        "пробки",
        "что происходит",
        "актуальн",
        "за сегодня",
        "на этой неделе",
        "в интернете",
        "найди в интернете",
        "поищи",
        "поищи в сети",
        "загугли",
    ]
    return any(marker in normalized for marker in internet_markers)


def _normalize_task_text(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Zа-яА-Я0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _token_overlap_ratio(a: str, b: str) -> float:
    tokens_a = set(_normalize_task_text(a).split())
    tokens_b = set(_normalize_task_text(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / max(1, len(tokens_a | tokens_b))


def _find_possible_duplicates(task_text: str) -> list[dict[str, Any]]:
    pages = _query_active_tasks()
    candidate_pages: list[dict[str, Any]] = []
    target = _normalize_task_text(task_text)
    for page in pages:
        title = _extract_title(page)
        normalized_title = _normalize_task_text(title)
        if not normalized_title:
            continue
        overlap = _token_overlap_ratio(task_text, title)
        if target == normalized_title or target in normalized_title or normalized_title in target or overlap >= 0.6:
            candidate_pages.append(page)
    return candidate_pages[:3]


def _format_duplicate_warning(duplicates: list[dict[str, Any]]) -> str:
    lines = ["Похоже, такая задача уже есть в активных:"]
    for page in duplicates:
        lines.append(f"- {_extract_title(page)} (`{page['id']}`)")
    lines.append("Если все равно нужно создать новую, напиши: 'добавь как новую: ...'")
    return "\n".join(lines)


def _normalize_priority(priority: str | None) -> str | None:
    if not priority:
        return None
    mapping = {
        "low": "Низкий",
        "низкий": "Низкий",
        "medium": "Средний",
        "med": "Средний",
        "средний": "Средний",
        "high": "Высокий",
        "высокий": "Высокий",
        "urgent": "Высокий",
        "срочно": "Высокий",
    }
    normalized = priority.strip().lower()
    return mapping.get(normalized, priority.strip().title())


def _normalize_due_date(due_date: str | None, task_text: str) -> str | None:
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
            return due_date
        except ValueError:
            pass
    text = task_text.lower()
    today = date.today()
    if "сегодня" in text:
        return today.isoformat()
    if "завтра" in text:
        return (today + timedelta(days=1)).isoformat()
    return None


async def _create_task_with_guardrails(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    task_text: str,
    explicit_group: str | None = None,
    due_date: str | None = None,
    priority: str | None = None,
) -> None:
    force_create = task_text.lower().startswith("добавь как новую:")
    cleaned_task_text = task_text[len("добавь как новую:") :].strip() if force_create else task_text.strip()
    if not cleaned_task_text:
        await update.message.reply_text("Не вижу текста задачи. Напиши, что именно нужно добавить.")
        return

    if not force_create:
        duplicates = _find_possible_duplicates(cleaned_task_text)
        if duplicates:
            await update.message.reply_text(_format_duplicate_warning(duplicates), parse_mode="Markdown")
            return

    target_group = explicit_group.strip() if explicit_group else _smart_pick_group_with_ai(cleaned_task_text)
    normalized_due = _normalize_due_date(due_date, cleaned_task_text)
    normalized_priority = _normalize_priority(priority)
    page = notion.pages.create(
        **_build_create_payload_with_planning(
            target_group,
            cleaned_task_text,
            due_date=normalized_due,
            priority=normalized_priority,
        )
    )
    suffix: list[str] = []
    if normalized_due:
        suffix.append(f"Срок: {normalized_due}")
    if normalized_priority:
        suffix.append(f"Приоритет: {normalized_priority}")
    extra = ("\n" + "\n".join(suffix)) if suffix else ""
    await update.message.reply_text(
        f"Записал задачу: {cleaned_task_text}\nОпределил группу: {target_group}{extra}\nID: {page['id']}"
    )


def _build_chat_memory_prompt(history: list[dict[str, str]], user_prompt: str) -> str:
    lines = [
        "Ты полезный персональный ассистент на русском языке. Отвечай кратко и по делу.",
        "Ниже последние сообщения диалога:",
    ]
    for item in history[-CHAT_MEMORY_LIMIT:]:
        role = item.get("role", "user")
        content = item.get("content", "")
        lines.append(f"{role}: {content}")
    lines.append(f"user: {user_prompt}")
    return "\n".join(lines)


def _build_confirmation_text(intent_data: dict[str, Any], source_text: str) -> str:
    intent = str(intent_data.get("intent", "chat")).strip().lower()
    if intent in {"add", "quick"}:
        task_text = str(intent_data.get("task_text", "")).strip() or source_text
        group_name = str(intent_data.get("group", "")).strip()
        target_group = group_name if (intent == "add" and group_name) else _smart_pick_group(task_text)
        return f"Подтверждаю: добавляю задачу '{task_text}' в группу '{target_group}'. Верно?"
    if intent == "list":
        group_filter = str(intent_data.get("group_filter", "")).strip()
        if group_filter:
            return f"Подтверждаю: показать активные задачи только в группе '{group_filter}'. Верно?"
        return "Подтверждаю: показать все активные задачи по группам. Верно?"
    if intent == "plan":
        plan_day = str(intent_data.get("plan_day", "")).strip() or "today"
        return f"Подтверждаю: показать план задач на '{plan_day}'. Верно?"
    if intent == "groups":
        return "Подтверждаю: показать список групп. Верно?"
    if intent == "done":
        query = str(intent_data.get("done_query", "")).strip() or source_text
        return f"Подтверждаю: отметить как выполненную задачу по запросу '{query}'. Верно?"
    if intent == "remind":
        minutes = intent_data.get("minutes", 0)
        reminder_text = str(intent_data.get("reminder_text", "")).strip()
        return f"Подтверждаю: поставить напоминание через {minutes} мин: '{reminder_text}'. Верно?"
    return "Подтверждаю: ответить как нейросеть на твой запрос. Верно?"


async def _execute_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, intent_data: dict[str, Any], source_text: str) -> None:
    intent = str(intent_data.get("intent", "chat")).strip().lower()
    if intent == "groups":
        await groups(update, context)
        return

    if intent == "list":
        group_filter = str(intent_data.get("group_filter", "")).strip()
        pages = _query_active_tasks(group_filter=group_filter or None)
        if not pages:
            await update.message.reply_text("Активных задач не найдено.")
            return
        by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for page in pages:
            if _is_completed(page):
                continue
            by_group[_extract_group(page)].append(page)
        lines: list[str] = []
        for group_name in sorted(by_group):
            lines.append(f"📁 {group_name}")
            for page in by_group[group_name]:
                lines.append(f"- {_extract_title(page)} (`{page['id']}`)")
            lines.append("")
        await update.message.reply_text("\n".join(lines).strip(), parse_mode="Markdown")
        return

    if intent == "plan":
        plan_day_raw = str(intent_data.get("plan_day", "")).strip() or "today"
        try:
            due_day = _parse_plan_day(plan_day_raw)
        except ValueError:
            await update.message.reply_text("Не понял дату плана. Используй today, tomorrow или YYYY-MM-DD.")
            return
        tasks = _query_tasks_by_due(due_day)
        if not tasks:
            await update.message.reply_text(f"На {due_day} задач со сроком не найдено.")
            return
        lines = [f"План на {due_day}:"]
        for page in tasks:
            lines.append(f"- {_extract_title(page)} ({_extract_group(page)}) `[{page['id']}]`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if intent == "done":
        query = str(intent_data.get("done_query", "")).strip() or source_text
        if "-" in query and len(query) >= 20:
            if _set_done_by_page_id(query):
                await update.message.reply_text("Отметил задачу как выполненную.")
            else:
                await update.message.reply_text("Не удалось найти задачу по ID.")
            return
        result = _set_done_by_title_fragment(query)
        if result == "not_found":
            await update.message.reply_text("Совпадений не найдено.")
        elif result == "many":
            await update.message.reply_text("Нашел несколько задач. Уточни запрос или используй ID из /list.")
        else:
            await update.message.reply_text(f"Готово. ID: {result}")
        return

    if intent == "remind":
        minutes = intent_data.get("minutes", 0)
        reminder_text = str(intent_data.get("reminder_text", "")).strip()
        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            minutes_int = 0
        if minutes_int > 0 and reminder_text:
            context.job_queue.run_once(
                _send_reminder,
                when=timedelta(minutes=minutes_int),
                data={"chat_id": update.effective_chat.id, "text": reminder_text},
            )
            await update.message.reply_text(f"Ок, напомню через {minutes_int} мин: {reminder_text}")
            return
        await update.message.reply_text("Не понял параметры напоминания. Укажи время и текст.")
        return

    if intent in {"add", "quick"}:
        task_text = str(intent_data.get("task_text", "")).strip() or source_text
        group_name = str(intent_data.get("group", "")).strip()
        due_date = str(intent_data.get("due_date", "")).strip() or None
        priority = str(intent_data.get("priority", "")).strip() or None
        explicit_group = group_name if (intent == "add" and group_name) else None
        await _create_task_with_guardrails(
            update, context, task_text, explicit_group=explicit_group, due_date=due_date, priority=priority
        )
        return

    await ask_ai_text(update, context, str(intent_data.get("chat_prompt", source_text)).strip() or source_text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    ai_mode = f"Ollama ({OLLAMA_MODEL})" if OLLAMA_MODEL else "не настроен"
    await update.message.reply_text(
        "Привет! Я твой личный Telegram-ассистент для задач и быстрых AI-ответов.\n\n"
        "Что я умею:\n"
        "/add <группа> | <задача>\n"
        "/quick <задача без группы>\n"
        "/list [группа]\n"
        "/groups\n"
        "/done <page_id_или_часть_названия>\n"
        "/remind <минуты> | <текст>\n"
        "/plan [today|tomorrow|YYYY-MM-DD]\n"
        "/ask <вопрос нейросети>\n\n"
        "Можно также писать обычным текстом - отвечу как нейросеть.\n\n"
        "Базовые группы по умолчанию: Работа, Учеба, Дом, Здоровье, Финансы, Покупки, Личное, Inbox.\n"
        "Если укажешь срок/приоритет, постараюсь сохранить их в задаче.\n\n"
        "Для полной работы нужны API/настройки в .env:\n"
        "- NOTION_TOKEN\n"
        "- NOTION_DATABASE_ID\n"
        "- OLLAMA_BASE_URL и OLLAMA_MODEL (бесплатный локальный AI)\n\n"
        f"Текущий AI режим: {ai_mode}\n\n"
        "Пример: /add Работа | Подготовить отчет",
        reply_markup=MENU_KEYBOARD,
    )


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    raw = " ".join(context.args).strip()
    if "|" not in raw:
        await update.message.reply_text("Формат: /add <группа> | <задача>")
        return
    group_name, task_text = [part.strip() for part in raw.split("|", 1)]
    if not group_name or not task_text:
        await update.message.reply_text("И группа, и текст задачи должны быть заполнены.")
        return
    await _create_task_with_guardrails(update, context, task_text, explicit_group=group_name)


async def quick_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    task_text = " ".join(context.args).strip()
    if not task_text:
        await update.message.reply_text("Формат: /quick <задача без группы>")
        return
    await _create_task_with_guardrails(update, context, task_text, explicit_group=None)


def _query_active_tasks(group_filter: str | None = None) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = [{"property": COMPLETED_PROP, "checkbox": {"equals": False}}]
    if group_filter:
        filters.append({"property": GROUP_PROP, "select": {"equals": group_filter}})
    result = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={"and": filters},
        sorts=[{"property": GROUP_PROP, "direction": "ascending"}],
        page_size=100,
    )
    return result.get("results", [])


def _extract_due_date(page: dict[str, Any]) -> str:
    date_obj = page.get("properties", {}).get(DUE_PROP, {}).get("date")
    if not date_obj:
        return ""
    return date_obj.get("start", "") or ""


def _query_tasks_by_due(due_day: str) -> list[dict[str, Any]]:
    if not _property_exists(DUE_PROP):
        return []
    result = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={
            "and": [
                {"property": COMPLETED_PROP, "checkbox": {"equals": False}},
                {"property": DUE_PROP, "date": {"equals": due_day}},
            ]
        },
        page_size=100,
    )
    return result.get("results", [])


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    group_filter = " ".join(context.args).strip() or None
    pages = _query_active_tasks(group_filter=group_filter)
    if not pages:
        await update.message.reply_text("Активных задач не найдено.")
        return
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        if _is_completed(page):
            continue
        by_group[_extract_group(page)].append(page)

    lines: list[str] = []
    for group_name in sorted(by_group):
        lines.append(f"📁 {group_name}")
        for page in by_group[group_name]:
            lines.append(f"- {_extract_title(page)} (`{page['id']}`)")
        lines.append("")
    await update.message.reply_text("\n".join(lines).strip(), parse_mode="Markdown")


async def groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    pages = _query_active_tasks()
    group_names = sorted({_extract_group(page) for page in pages if not _is_completed(page)}.union(set(BASIC_GROUPS)))
    if not group_names:
        await update.message.reply_text("Группы пока пустые.")
        return
    await update.message.reply_text("Группы:\n" + "\n".join(f"- {g}" for g in group_names))


def _set_done_by_page_id(page_id: str) -> bool:
    try:
        notion.pages.update(
            page_id=page_id,
            properties={COMPLETED_PROP: {"checkbox": True}, STATUS_PROP: {"status": {"name": "Done"}}},
        )
        return True
    except Exception:
        return False


def _set_done_by_title_fragment(fragment: str) -> str:
    result = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={
            "and": [
                {"property": COMPLETED_PROP, "checkbox": {"equals": False}},
                {"property": TITLE_PROP, "title": {"contains": fragment}},
            ]
        },
        page_size=10,
    )
    pages = result.get("results", [])
    if not pages:
        return "not_found"
    if len(pages) > 1:
        return "many"
    page = pages[0]
    notion.pages.update(
        page_id=page["id"],
        properties={COMPLETED_PROP: {"checkbox": True}, STATUS_PROP: {"status": {"name": "Done"}}},
    )
    return page["id"]


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Формат: /done <page_id_или_часть_названия>")
        return
    if "-" in query and len(query) >= 20:
        if _set_done_by_page_id(query):
            await update.message.reply_text("Отметил задачу как выполненную.")
        else:
            await update.message.reply_text("Не удалось найти задачу по ID.")
        return

    result = _set_done_by_title_fragment(query)
    if result == "not_found":
        await update.message.reply_text("Совпадений не найдено.")
    elif result == "many":
        await update.message.reply_text("Нашел несколько задач. Уточни запрос или используй ID из /list.")
    else:
        await update.message.reply_text(f"Готово. ID: {result}")


async def hide_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    await update.message.reply_text("Кнопки скрыты. Вернуть: /start", reply_markup=ReplyKeyboardRemove())


async def ask_ai_text(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    if _looks_like_internet_required(prompt):
        await update.message.reply_text(
            "К сожалению, я не могу надежно ответить на этот запрос: у меня нет доступа к интернету в текущем режиме."
        )
        return
    chat_memory = context.user_data.get("chat_memory", [])
    if not isinstance(chat_memory, list):
        chat_memory = []
    try:
        final_prompt = _build_chat_memory_prompt(chat_memory, prompt)
        answer = _ask_ollama(final_prompt)
    except Exception as exc:
        logger.exception("Ollama request failed: %s", exc)
        await update.message.reply_text(
            "Не удалось получить ответ от локальной модели.\n"
            "Проверь, что Ollama запущен и модель установлена.\n"
            "Пример: ollama run llama3.1:8b"
        )
        return
    if not answer:
        await update.message.reply_text("Получил пустой ответ от модели. Попробуй переформулировать.")
        return
    chat_memory.append({"role": "user", "content": prompt})
    chat_memory.append({"role": "assistant", "content": answer})
    context.user_data["chat_memory"] = chat_memory[-CHAT_MEMORY_LIMIT:]
    await update.message.reply_text(answer)


async def ask_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Формат: /ask <вопрос>")
        return
    await ask_ai_text(update, context, prompt)


async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text == "/add":
        await update.message.reply_text("Шаблон: /add <группа> | <задача>")
    elif text == "/quick":
        await update.message.reply_text("Шаблон: /quick <задача без группы>")
    elif text == "/done":
        await update.message.reply_text("Шаблон: /done <page_id_или_часть_названия>")
    elif text == "/remind":
        await update.message.reply_text("Шаблон: /remind <минуты> | <текст>")
    elif text == "/plan":
        await update.message.reply_text("Шаблон: /plan [today|tomorrow|YYYY-MM-DD]")
    elif text == "/ask":
        await update.message.reply_text("Шаблон: /ask <вопрос>")
    elif text == "Скрыть кнопки":
        await hide_buttons(update, context)


async def chat_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    text = (update.message.text or "").strip()
    if not text:
        return
    if text in {"/add", "/quick", "/done", "/remind", "/plan", "/ask", "Скрыть кнопки"}:
        await menu_help(update, context)
        return

    pending_confirmation = context.user_data.get("pending_confirmation")
    if pending_confirmation:
        if _is_affirmative(text):
            await _execute_intent(
                update,
                context,
                pending_confirmation.get("intent_data", {"intent": "chat"}),
                pending_confirmation.get("source_text", ""),
            )
            context.user_data.pop("pending_confirmation", None)
            return
        if _is_negative(text):
            context.user_data.pop("pending_confirmation", None)
            await update.message.reply_text("Ок, отменил. Напиши заново, как нужно сделать.")
            return
        await update.message.reply_text("Ответь 'да' или 'нет', чтобы подтвердить действие.")
        return

    pending_clarification = context.user_data.get("pending_clarification")
    if pending_clarification:
        base_text = str(pending_clarification.get("source_text", "")).strip()
        combined_text = f"{base_text}\nУточнение пользователя: {text}".strip()
        try:
            clarified_intent = _detect_intent(combined_text)
        except Exception:
            clarified_intent = {"intent": "chat", "chat_prompt": combined_text}
        confirmation_text = _build_confirmation_text(clarified_intent, base_text or text)
        context.user_data["pending_confirmation"] = {
            "intent_data": clarified_intent,
            "source_text": base_text or text,
        }
        context.user_data.pop("pending_clarification", None)
        await update.message.reply_text(confirmation_text)
        return

    try:
        intent_data = _detect_intent(text)
    except Exception:
        intent_data = {"intent": "chat", "chat_prompt": text}

    intent = str(intent_data.get("intent", "chat")).strip().lower()
    try:
        confidence = float(intent_data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    needs_clarification = bool(intent_data.get("needs_clarification", False))
    clarification_question = str(intent_data.get("clarification_question", "")).strip()

    if needs_clarification or confidence < INTENT_CONFIDENCE_THRESHOLD:
        question = clarification_question or (
            "Уточни, пожалуйста, что нужно сделать: добавить задачу, показать список, отметить выполненной, "
            "поставить напоминание, показать план на дату или просто ответить на вопрос?"
        )
        context.user_data["pending_clarification"] = {"source_text": text}
        await update.message.reply_text(question)
        return

    await _execute_intent(update, context, intent_data, text)


async def _send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    await context.bot.send_message(chat_id=data.get("chat_id"), text=f"⏰ Напоминание: {data.get('text', '(пусто)')}")


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    raw = " ".join(context.args).strip()
    if "|" not in raw:
        await update.message.reply_text("Формат: /remind <минуты> | <текст>")
        return
    minutes_raw, reminder_text = [part.strip() for part in raw.split("|", 1)]
    if not minutes_raw.isdigit() or int(minutes_raw) <= 0:
        await update.message.reply_text("Минуты должны быть положительным числом.")
        return
    minutes = int(minutes_raw)
    context.job_queue.run_once(
        _send_reminder, when=timedelta(minutes=minutes), data={"chat_id": update.effective_chat.id, "text": reminder_text}
    )
    await update.message.reply_text(f"Ок, напомню через {minutes} мин: {reminder_text}")


def _parse_plan_day(text: str) -> str:
    raw = text.strip().lower()
    today = date.today()
    if raw in {"", "today", "сегодня"}:
        return today.isoformat()
    if raw in {"tomorrow", "завтра"}:
        return (today + timedelta(days=1)).isoformat()
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _remember_chat_id(context, update)
    day_arg = " ".join(context.args).strip()
    try:
        due_day = _parse_plan_day(day_arg)
    except ValueError:
        await update.message.reply_text("Формат: /plan [today|tomorrow|YYYY-MM-DD]")
        return
    tasks = _query_tasks_by_due(due_day)
    if not tasks:
        await update.message.reply_text(f"На {due_day} задач со сроком не найдено.")
        return
    lines = [f"План на {due_day}:"]
    for page in tasks:
        group_name = _extract_group(page)
        lines.append(f"- {_extract_title(page)} ({group_name}) `[{page['id']}]`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _remember_chat_id(context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
    if not update.effective_chat:
        return
    chats = context.application.bot_data.setdefault("subscribed_chats", set())
    chats.add(update.effective_chat.id)


def _parse_hhmm(raw: str) -> time:
    try:
        hour_str, minute_str = raw.split(":", 1)
        return time(hour=int(hour_str), minute=int(minute_str))
    except Exception:
        return time(hour=9, minute=0)


async def _morning_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    chats = context.application.bot_data.get("subscribed_chats", set())
    text = "Доброе утро! Ты уже проснулся? Какие планы на день и что самое важное сегодня?"
    for chat_id in chats:
        await context.bot.send_message(chat_id=chat_id, text=text)


async def _evening_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    chats = context.application.bot_data.get("subscribed_chats", set())
    text = "Как прошел день? Что сделал сегодня и какие планы на завтра?"
    for chat_id in chats:
        await context.bot.send_message(chat_id=chat_id, text=text)


async def _post_init(app: Any) -> None:
    app.bot_data.setdefault("subscribed_chats", set())
    morning_time = _parse_hhmm(MORNING_CHECKIN_TIME)
    evening_time = _parse_hhmm(EVENING_CHECKIN_TIME)
    app.job_queue.run_daily(_morning_checkin, time=morning_time, name="morning_checkin")
    app.job_queue.run_daily(_evening_checkin, time=evening_time, name="evening_checkin")


def validate_env() -> None:
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "NOTION_TOKEN": NOTION_TOKEN,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def main() -> None:
    validate_env()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("quick", quick_task))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("groups", groups))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("ask", ask_ai))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_fallback))
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
