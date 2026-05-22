# МОП Reminder Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** За 10 минут до дедлайна задачи (созданной МОПом, привязанной к сделке) отправить МОПу в личный Telegram уведомление с данными клиента и AI-рекомендацией от Claude на основе последнего звонка и визита.

**Architecture:** Polling каждые 5 минут через job_queue — проверяем задачи с дедлайном в окне [+8, +13 мин]. Для каждой задачи: получаем последний звонок и визит по сделке, отправляем в Claude, результат → Telegram МОПа. Защита от дублей — таблица `reminders_sent` в SQLite.

**Tech Stack:** Python, python-telegram-bot[job-queue], httpx, anthropic SDK, SQLite, pytest, pytest-asyncio

---

## File Map

| Файл | Действие | Ответственность |
|------|----------|-----------------|
| `db.py` | Modify | Таблицы mop_telegram, reminders_sent; CRUD-функции |
| `bitrix.py` | Modify | fetch_mop_upcoming_tasks, fetch_last_call, fetch_last_visit, fetch_deal_detail |
| `ai_advisor.py` | Create | get_recommendation() — Claude API |
| `task_reminder.py` | Create | check_upcoming_tasks() — polling loop |
| `bot.py` | Modify | /mop команда (admin), регистрация job |
| `requirements.txt` | Modify | +anthropic, +pytest, +pytest-asyncio |
| `.env.example` | Modify | +ANTHROPIC_API_KEY |
| `pytest.ini` | Create | asyncio_mode = auto |
| `tests/__init__.py` | Create | Пустой |
| `tests/test_db.py` | Create | Тесты для новых db функций |
| `tests/test_bitrix.py` | Create | Тесты для новых bitrix методов (mock HTTP) |
| `tests/test_ai_advisor.py` | Create | Тесты для get_recommendation (mock Claude) |
| `tests/test_task_reminder.py` | Create | Тесты для логики фильтрации задач |

---

### Task 1: DB — таблицы и CRUD для МОПов и напоминаний

**Files:**
- Modify: `db.py`
- Create: `pytest.ini`
- Create: `tests/__init__.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Создать pytest.ini и tests/__init__.py**

Создать `pytest.ini` в корне `bitrix_bot/`:
```ini
[pytest]
asyncio_mode = auto
```

Создать пустой `tests/__init__.py`.

- [ ] **Step 2: Написать тесты для новых DB функций**

Создать `tests/test_db.py`:

```python
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    import db
    db.DB_PATH = str(tmp_path / "test.db")
    db.init_db()
    db.init_mop_tables()
    yield
    db.DB_PATH = "bot.db"


def test_upsert_and_get_mop():
    import db
    db.upsert_mop(101, 999888, "Самал")
    assert db.get_mop_telegram(101) == 999888


def test_get_all_mops():
    import db
    db.upsert_mop(101, 111, "Самал")
    db.upsert_mop(102, 222, "Заир")
    mops = db.get_all_mops()
    assert len(mops) == 2
    names = {m["name"] for m in mops}
    assert names == {"Самал", "Заир"}


def test_upsert_mop_updates_existing():
    import db
    db.upsert_mop(101, 111, "Самал")
    db.upsert_mop(101, 222, "Самал")
    assert db.get_mop_telegram(101) == 222


def test_get_mop_telegram_returns_none_for_unknown():
    import db
    assert db.get_mop_telegram(999) is None


def test_reminder_not_sent_initially():
    import db
    assert not db.is_reminder_sent(42)


def test_mark_and_check_reminder_sent():
    import db
    db.mark_reminder_sent(42)
    assert db.is_reminder_sent(42)


def test_mark_reminder_sent_idempotent():
    import db
    db.mark_reminder_sent(42)
    db.mark_reminder_sent(42)  # не должно упасть
    assert db.is_reminder_sent(42)


def test_cleanup_old_reminders():
    import db
    db.mark_reminder_sent(10)
    db.cleanup_old_reminders(days=0)
    assert not db.is_reminder_sent(10)


def test_cleanup_keeps_recent_reminders():
    import db
    db.mark_reminder_sent(20)
    db.cleanup_old_reminders(days=7)
    assert db.is_reminder_sent(20)
```

- [ ] **Step 3: Запустить тесты — убедиться что падают**

```bash
cd bitrix_bot
pip install pytest pytest-asyncio -q
pytest tests/test_db.py -v
```

Ожидаемо: `AttributeError: module 'db' has no attribute 'init_mop_tables'`

- [ ] **Step 4: Добавить таблицы и функции в db.py**

В `db.py` после функции `init_db()` добавить:

```python
def init_mop_tables():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mop_telegram (
                bitrix_id   INTEGER PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                name        TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders_sent (
                task_id INTEGER PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_sent_at
                ON reminders_sent(sent_at);
        """)


def upsert_mop(bitrix_id: int, telegram_id: int, name: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO mop_telegram (bitrix_id, telegram_id, name)
            VALUES (?, ?, ?)
            ON CONFLICT(bitrix_id) DO UPDATE SET
                telegram_id = excluded.telegram_id,
                name        = excluded.name
        """, (bitrix_id, telegram_id, name))


def get_mop_telegram(bitrix_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT telegram_id FROM mop_telegram WHERE bitrix_id = ?",
            (bitrix_id,)
        ).fetchone()
    return row["telegram_id"] if row else None


def get_all_mops() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT bitrix_id, telegram_id, name FROM mop_telegram"
        ).fetchall()
    return [dict(r) for r in rows]


def is_reminder_sent(task_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders_sent WHERE task_id = ?",
            (task_id,)
        ).fetchone()
    return row is not None


def mark_reminder_sent(task_id: int):
    from datetime import datetime
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminders_sent (task_id, sent_at) VALUES (?, ?)",
            (task_id, datetime.utcnow().isoformat())
        )


def cleanup_old_reminders(days: int = 7):
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM reminders_sent WHERE sent_at < ?",
            (cutoff,)
        )
```

- [ ] **Step 5: Запустить тесты — убедиться что проходят**

```bash
pytest tests/test_db.py -v
```

Ожидаемо: все тесты `PASSED`.

- [ ] **Step 6: Commit**

```bash
git add db.py pytest.ini tests/__init__.py tests/test_db.py
git commit -m "feat: add mop_telegram and reminders_sent DB tables"
```

---

### Task 2: Bitrix — методы для задач, звонков, визитов и деталей сделки

**Files:**
- Modify: `bitrix.py`
- Create: `tests/test_bitrix.py`

- [ ] **Step 1: Написать тесты**

Создать `tests/test_bitrix.py`:

```python
import os
import sys
import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def bx():
    with patch("db.db_get", return_value="https://test.bitrix24.kz/rest/1/token/"):
        from bitrix import Bitrix
        return Bitrix()


async def test_fetch_mop_upcoming_tasks_keeps_only_deal_tasks(bx):
    all_tasks = [
        {"id": "1", "ufCrmTask": ["D_100"]},
        {"id": "2", "ufCrmTask": []},           # нет сделки — отфильтровать
        {"id": "3", "ufCrmTask": ["D_200"]},
        {"id": "4", "ufCrmTask": ["T_999"]},    # не сделка — отфильтровать
    ]
    now = datetime.now(timezone.utc)
    with patch.object(bx, "list_all_tasks", new=AsyncMock(return_value=all_tasks)):
        tasks = await bx.fetch_mop_upcoming_tasks(42, now, now + timedelta(minutes=13))

    ids = [t["id"] for t in tasks]
    assert "1" in ids
    assert "3" in ids
    assert "2" not in ids
    assert "4" not in ids


async def test_fetch_last_call_returns_most_recent(bx):
    activities = [
        {"ID": "10", "TYPE_ID": "2", "START_TIME": "2026-05-20T10:00:00", "DESCRIPTION": "Звонок 1"},
        {"ID": "11", "TYPE_ID": "2", "START_TIME": "2026-05-22T10:00:00", "DESCRIPTION": "Звонок 2"},
    ]
    with patch.object(bx, "list_all", new=AsyncMock(return_value=activities)):
        result = await bx.fetch_last_call(100)
    assert result["ID"] == "11"


async def test_fetch_last_call_returns_none_when_empty(bx):
    with patch.object(bx, "list_all", new=AsyncMock(return_value=[])):
        result = await bx.fetch_last_call(100)
    assert result is None


async def test_fetch_last_visit_returns_most_recent(bx):
    activities = [
        {"ID": "20", "TYPE_ID": "1", "START_TIME": "2026-05-15T10:00:00", "DESCRIPTION": "Визит 1"},
        {"ID": "21", "TYPE_ID": "1", "START_TIME": "2026-05-18T10:00:00", "DESCRIPTION": "Визит 2"},
    ]
    with patch.object(bx, "list_all", new=AsyncMock(return_value=activities)):
        result = await bx.fetch_last_visit(100)
    assert result["ID"] == "21"


async def test_fetch_last_visit_returns_none_when_empty(bx):
    with patch.object(bx, "list_all", new=AsyncMock(return_value=[])):
        result = await bx.fetch_last_visit(100)
    assert result is None


async def test_fetch_deal_detail(bx):
    deal = {"ID": "100", "TITLE": "ЖК Керуен 2к", "STAGE_ID": "C1:1", "DATE_MODIFY": "2026-05-09T10:00:00"}
    with patch.object(bx, "call", new=AsyncMock(return_value={"result": deal})):
        result = await bx.fetch_deal_detail(100)
    assert result["TITLE"] == "ЖК Керуен 2к"
    assert result["STAGE_ID"] == "C1:1"
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
pytest tests/test_bitrix.py -v
```

Ожидаемо: `AttributeError` — методы не существуют.

- [ ] **Step 3: Добавить методы в конец класса Bitrix в bitrix.py**

```python
    async def fetch_mop_upcoming_tasks(
        self, user_id: int, window_start, window_end
    ) -> list:
        """Tasks created by МОП, linked to a deal, with deadline in window."""
        ws = window_start.strftime("%Y-%m-%dT%H:%M:%S")
        we = window_end.strftime("%Y-%m-%dT%H:%M:%S")

        tasks = await self.list_all_tasks({
            "filter": {
                "CREATED_BY": user_id,
                "!REAL_STATUS": 5,
                ">=DEADLINE": ws,
                "<=DEADLINE": we,
            },
            "select": ["ID", "TITLE", "DEADLINE", "UF_CRM_TASK"],
        })

        result = []
        for task in tasks:
            crm = task.get("ufCrmTask") or task.get("UF_CRM_TASK") or []
            if isinstance(crm, str):
                crm = [crm]
            has_deal = any(
                isinstance(r, str) and r.upper().startswith("D_")
                for r in crm
            )
            if has_deal:
                result.append(task)
        return result

    async def fetch_last_call(self, deal_id: int):
        """Last phone call activity for a deal (TYPE_ID=2)."""
        activities = await self.list_all("crm.activity.list", {
            "filter": {"OWNER_TYPE_ID": 2, "OWNER_ID": deal_id, "TYPE_ID": 2},
            "select": ["ID", "TYPE_ID", "START_TIME", "DESCRIPTION", "SUBJECT"],
            "order": {"START_TIME": "DESC"},
        })
        if not activities:
            return None
        return max(activities, key=lambda a: a.get("START_TIME", ""))

    async def fetch_last_visit(self, deal_id: int):
        """Last meeting/visit activity for a deal (TYPE_ID=1)."""
        activities = await self.list_all("crm.activity.list", {
            "filter": {"OWNER_TYPE_ID": 2, "OWNER_ID": deal_id, "TYPE_ID": 1},
            "select": ["ID", "TYPE_ID", "START_TIME", "DESCRIPTION", "SUBJECT"],
            "order": {"START_TIME": "DESC"},
        })
        if not activities:
            return None
        return max(activities, key=lambda a: a.get("START_TIME", ""))

    async def fetch_deal_detail(self, deal_id: int) -> dict:
        """Fetch deal details: title, stage, dates."""
        r = await self.call("crm.deal.get", {"id": deal_id})
        return r.get("result", {})
```

- [ ] **Step 4: Запустить тесты — убедиться что проходят**

```bash
pytest tests/test_bitrix.py -v
```

Ожидаемо: все тесты `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add bitrix.py tests/test_bitrix.py
git commit -m "feat: add Bitrix methods for tasks, calls, visits, deal detail"
```

---

### Task 3: AI Advisor — интеграция с Claude

**Files:**
- Create: `ai_advisor.py`
- Create: `tests/test_ai_advisor.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Обновить зависимости**

В `requirements.txt` добавить строку:
```
anthropic
```

В `.env.example` добавить строку:
```
ANTHROPIC_API_KEY=sk-ant-...
```

Установить:
```bash
pip install anthropic -q
```

- [ ] **Step 2: Написать тесты**

Создать `tests/test_ai_advisor.py`:

```python
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


def _mock_response(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


def test_get_recommendation_with_call_and_visit():
    deal = {"TITLE": "ЖК Керуен 2к", "STAGE_ID": "C1:NEW", "DATE_MODIFY": "2026-05-10T00:00:00"}
    call = {"DESCRIPTION": "Клиент думает, ждёт мужа", "START_TIME": "2026-05-21T10:00:00"}
    visit = {"DESCRIPTION": "Понравилась планировка, смущает цена", "START_TIME": "2026-05-15T10:00:00"}
    expected = "• Уточни позицию мужа\n• Возражение: цена\n• Предложи рассрочку"

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_response(expected)
        import importlib
        import ai_advisor
        importlib.reload(ai_advisor)
        result = ai_advisor.get_recommendation(deal, call, visit)

    assert "Уточни" in result


def test_get_recommendation_without_visit():
    deal = {"TITLE": "ЖК Керуен 1к", "STAGE_ID": "C1:NEW", "DATE_MODIFY": "2026-05-01T00:00:00"}
    call = {"DESCRIPTION": "Хочет скидку", "START_TIME": "2026-05-22T10:00:00"}

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_response("• Работай со скидкой")
        import ai_advisor
        result = ai_advisor.get_recommendation(deal, call, None)

    assert isinstance(result, str)
    assert len(result) > 0


def test_get_recommendation_returns_fallback_on_error():
    deal = {"TITLE": "Тест", "STAGE_ID": "C1:NEW", "DATE_MODIFY": "2026-05-01T00:00:00"}

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = Exception("API error")
        import ai_advisor
        result = ai_advisor.get_recommendation(deal, None, None)

    assert result == "Рекомендация недоступна. Изучите историю клиента перед звонком."
```

- [ ] **Step 3: Запустить тесты — убедиться что падают**

```bash
pytest tests/test_ai_advisor.py -v
```

Ожидаемо: `ModuleNotFoundError: No module named 'ai_advisor'`

- [ ] **Step 4: Создать ai_advisor.py**

```python
import os
from datetime import datetime
import anthropic

SYSTEM_PROMPT = """Ты — эксперт по продажам первичной недвижимости в Алматы.
Твоя задача — помочь менеджеру ЖК Керуен подготовиться к звонку с клиентом.

На основе данных дай рекомендацию строго в формате:
• На что обратить внимание: [1 предложение]
• Ключевое возражение клиента: [1 предложение]
• Как закрыть на следующий шаг: [1 предложение]

Приоритет данных: последний звонок (клиент даёт живую обратную связь).
Визит в ОП — используй как контекст истории отношений с клиентом.
Будь конкретным, без воды. Максимум 4 предложения итого."""


def _days_since(date_str: str) -> int:
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return (datetime.now(dt.tzinfo) - dt).days
    except Exception:
        return 0


def get_recommendation(deal: dict, call, visit) -> str:
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        days_in_stage = _days_since(deal.get("DATE_MODIFY", ""))
        parts = [
            f"Сделка: {deal.get('TITLE', 'Неизвестно')}",
            f"Этап: {deal.get('STAGE_ID', '')}, {days_in_stage} дней в этапе",
        ]

        if call:
            call_days = _days_since(call.get("START_TIME", ""))
            call_text = call.get("DESCRIPTION") or call.get("SUBJECT") or "Нет описания"
            parts.append(
                f"\n[ЗВОНОК — {call_days} дн. назад — ПРИОРИТЕТ]\n{call_text[:1500]}"
            )

        if visit:
            visit_days = _days_since(visit.get("START_TIME", ""))
            visit_text = visit.get("DESCRIPTION") or visit.get("SUBJECT") or "Нет описания"
            parts.append(
                f"\n[ВИЗИТ В ОП — {visit_days} дн. назад — КОНТЕКСТ]\n{visit_text[:800]}"
            )

        if not call and not visit:
            parts.append("\nИстория общения недоступна. Дай рекомендацию по этапу сделки.")

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
        return response.content[0].text.strip()

    except Exception:
        return "Рекомендация недоступна. Изучите историю клиента перед звонком."
```

- [ ] **Step 5: Запустить тесты — убедиться что проходят**

```bash
pytest tests/test_ai_advisor.py -v
```

Ожидаемо: все тесты `PASSED`.

- [ ] **Step 6: Commit**

```bash
git add ai_advisor.py tests/test_ai_advisor.py requirements.txt .env.example
git commit -m "feat: add Claude AI advisor for call recommendations"
```

---

### Task 4: Task Reminder — polling-модуль

**Files:**
- Create: `task_reminder.py`
- Create: `tests/test_task_reminder.py`

- [ ] **Step 1: Написать тесты**

Создать `tests/test_task_reminder.py`:

```python
import os
import sys
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_TOKEN", "test")


def test_extract_deal_id_lowercase_key():
    from task_reminder import extract_deal_id
    assert extract_deal_id({"ufCrmTask": ["D_123"]}) == 123


def test_extract_deal_id_uppercase_key():
    from task_reminder import extract_deal_id
    assert extract_deal_id({"UF_CRM_TASK": ["D_456"]}) == 456


def test_extract_deal_id_no_deal_returns_none():
    from task_reminder import extract_deal_id
    assert extract_deal_id({"ufCrmTask": ["T_789"]}) is None


def test_extract_deal_id_empty_returns_none():
    from task_reminder import extract_deal_id
    assert extract_deal_id({"ufCrmTask": []}) is None


def test_format_reminder_message_contains_key_parts():
    from task_reminder import format_reminder_message
    deal = {
        "TITLE": "ЖК Керуен 2к",
        "STAGE_ID": "Думает",
        "DATE_MODIFY": "2026-05-09T00:00:00+00:00",
    }
    call = {"DESCRIPTION": "Думает, ждёт мужа", "START_TIME": "2026-05-21T10:00:00+00:00"}
    task = {"title": "Позвонить Асель"}
    recommendation = "• Уточни позицию мужа"

    msg = format_reminder_message(deal, call, task, recommendation, "https://test.bitrix24.kz", 100)

    assert "🔔" in msg
    assert "ЖК Керуен 2к" in msg
    assert "Уточни" in msg
    assert "https://test.bitrix24.kz/crm/deal/details/100/" in msg


def test_format_reminder_message_without_call():
    from task_reminder import format_reminder_message
    deal = {"TITLE": "ЖК Керуен 1к", "STAGE_ID": "Новый", "DATE_MODIFY": "2026-05-01T00:00:00+00:00"}
    task = {"title": "Звонок"}
    recommendation = "• Выясни потребность"

    msg = format_reminder_message(deal, None, task, recommendation, "https://test.bitrix24.kz", 200)

    assert "🔔" in msg
    assert "ЖК Керуен 1к" in msg
    assert "Выясни" in msg
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
pytest tests/test_task_reminder.py -v
```

Ожидаемо: `ModuleNotFoundError: No module named 'task_reminder'`

- [ ] **Step 3: Создать task_reminder.py**

```python
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bitrix import Bitrix
from db import get_all_mops, is_reminder_sent, mark_reminder_sent, cleanup_old_reminders
from ai_advisor import get_recommendation

log = logging.getLogger(__name__)

WINDOW_START_MIN = 8
WINDOW_END_MIN = 13


def extract_deal_id(task: dict):
    crm = task.get("ufCrmTask") or task.get("UF_CRM_TASK") or []
    if isinstance(crm, str):
        crm = [crm]
    for ref in crm:
        if isinstance(ref, str) and ref.upper().startswith("D_"):
            try:
                return int(ref.split("_")[1])
            except (ValueError, IndexError):
                pass
    return None


def format_reminder_message(
    deal: dict,
    call,
    task: dict,
    recommendation: str,
    portal_url: str,
    deal_id: int,
) -> str:
    deal_title = deal.get("TITLE", "Неизвестная сделка")
    stage = deal.get("STAGE_ID", "")

    try:
        modify = deal.get("DATE_MODIFY", "")
        dt = datetime.fromisoformat(modify.replace("Z", "+00:00"))
        days_in_stage = (datetime.now(dt.tzinfo) - dt).days
        stage_str = f"{stage} ({days_in_stage} дн. в этапе)"
    except Exception:
        stage_str = stage

    call_block = ""
    if call:
        call_text = (call.get("DESCRIPTION") or call.get("SUBJECT") or "")[:400]
        try:
            call_dt = datetime.fromisoformat(call.get("START_TIME", "").replace("Z", "+00:00"))
            call_ago = f"{(datetime.now(call_dt.tzinfo) - call_dt).days} дн. назад"
        except Exception:
            call_ago = ""
        call_block = f"\n\n📞 <b>Последний звонок</b> ({call_ago}):\n{call_text}"

    deal_url = f"{portal_url}/crm/deal/details/{deal_id}/"

    return (
        f"🔔 <b>Звонок через 10 минут</b>\n\n"
        f"🏠 <b>Сделка:</b> {deal_title}\n"
        f"📍 <b>Этап:</b> {stage_str}"
        f"{call_block}\n\n"
        f"🤖 <b>Рекомендация:</b>\n{recommendation}\n\n"
        f'🔗 <a href="{deal_url}">Открыть сделку</a>'
    )


async def process_task(bx: Bitrix, mop: dict, task: dict, bot) -> None:
    task_id = int(task.get("id") or task.get("ID", 0))
    if is_reminder_sent(task_id):
        return

    deal_id = extract_deal_id(task)
    if not deal_id:
        return

    try:
        deal, call, visit = await asyncio.gather(
            bx.fetch_deal_detail(deal_id),
            bx.fetch_last_call(deal_id),
            bx.fetch_last_visit(deal_id),
        )
    except Exception as e:
        log.error("Error fetching data for task %s: %s", task_id, e)
        return

    recommendation = get_recommendation(deal, call, visit)
    portal_url = bx.get_portal_url()
    text = format_reminder_message(deal, call, task, recommendation, portal_url, deal_id)

    try:
        await bot.send_message(
            chat_id=mop["telegram_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        mark_reminder_sent(task_id)
        log.info("Reminder sent to %s for task %s", mop["name"], task_id)
    except Exception as e:
        log.error("Failed to send reminder to %s: %s", mop["name"], e)


async def check_upcoming_tasks(ctx) -> None:
    """Job queue callback — runs every 5 minutes."""
    mops = get_all_mops()
    if not mops:
        return

    now = datetime.now(timezone.utc)
    window_start = now + timedelta(minutes=WINDOW_START_MIN)
    window_end = now + timedelta(minutes=WINDOW_END_MIN)

    bx = Bitrix()
    bot = ctx.bot

    for mop in mops:
        try:
            tasks = await bx.fetch_mop_upcoming_tasks(mop["bitrix_id"], window_start, window_end)
            for task in tasks:
                await process_task(bx, mop, task, bot)
        except Exception as e:
            log.error("Error checking tasks for %s: %s", mop["name"], e)

    cleanup_old_reminders(days=7)
```

- [ ] **Step 4: Запустить тесты — убедиться что проходят**

```bash
pytest tests/test_task_reminder.py -v
```

Ожидаемо: все тесты `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add task_reminder.py tests/test_task_reminder.py
git commit -m "feat: add task reminder polling module"
```

---

### Task 5: Bot — команда /mop для привязки МОПов к Telegram

**Files:**
- Modify: `bot.py`

- [ ] **Step 1: Обновить импорты из db в bot.py**

Найти блок:
```python
from db import (
    init_db, db_get, db_set,
    get_managers, upsert_manager, toggle_manager,
    get_projects, upsert_project, get_project_enum_id,
    set_plan, get_plan,
    find_managers_by_names,
)
```

Заменить на:
```python
from db import (
    init_db, db_get, db_set,
    get_managers, upsert_manager, toggle_manager,
    get_projects, upsert_project, get_project_enum_id,
    set_plan, get_plan,
    find_managers_by_names,
    upsert_mop, get_all_mops, init_mop_tables,
)
```

- [ ] **Step 2: Добавить состояния для /mop conversation**

Найти строку:
```python
    SETUP_USER_PICK,
) = range(20, 27)
```

После неё добавить:
```python
(
    MOP_PICK, MOP_TG_ID,
) = range(30, 32)
```

- [ ] **Step 3: Добавить обработчики /mop**

Перед функцией `main()` добавить:

```python
# ─── /mop conversation ────────────────────────────────────────────────────────

async def cmd_mop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END

    managers = get_managers()
    if not managers:
        await update.message.reply_text("⚠️ Сначала загрузите менеджеров через /managers")
        return ConversationHandler.END

    linked = {m["bitrix_id"]: m for m in get_all_mops()}
    rows = []
    for m in managers:
        bid = m["bitrix_id"]
        tg = linked[bid]["telegram_id"] if bid in linked else None
        icon = "✅" if tg else "☐"
        label = f"{icon} {m['short_name']}" + (f" (tg:{tg})" if tg else "")
        rows.append([(label, f"mop:{bid}:{m['short_name']}")])
    rows.append([("❌ Отмена", "mop:cancel")])

    await update.message.reply_text(
        "👥 <b>Привязка МОПов к Telegram</b>\nВыберите менеджера:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb(rows),
    )
    return MOP_PICK


async def mop_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "mop:cancel":
        await safe_edit(q, "Отменено.")
        return ConversationHandler.END

    _, bitrix_id, name = q.data.split(":", 2)
    ctx.user_data["mop_bitrix_id"] = int(bitrix_id)
    ctx.user_data["mop_name"] = name
    await safe_edit(
        q,
        f"👤 <b>{name}</b>\n\n"
        f"Введите Telegram ID этого менеджера.\n"
        f"<i>МОП может узнать свой ID у @userinfobot</i>",
        parse_mode=ParseMode.HTML,
    )
    return MOP_TG_ID


async def mop_tg_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        tg_id = int(txt)
    except ValueError:
        await update.message.reply_text("❌ Введите числовой Telegram ID.")
        return MOP_TG_ID

    bitrix_id = ctx.user_data["mop_bitrix_id"]
    name = ctx.user_data["mop_name"]
    upsert_mop(bitrix_id, tg_id, name)
    await update.message.reply_text(
        f"✅ <b>{name}</b> привязан к Telegram ID <code>{tg_id}</code>",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def mop_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END
```

- [ ] **Step 4: Зарегистрировать conversation в main()**

В `main()` после `init_db()` добавить:
```python
    init_mop_tables()
```

После блока `setup_conv = ConversationHandler(...)` добавить:
```python
    mop_conv = ConversationHandler(
        entry_points=[CommandHandler("mop", cmd_mop)],
        states={
            MOP_PICK: [CallbackQueryHandler(mop_pick, pattern="^mop:")],
            MOP_TG_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, mop_tg_id)],
        },
        fallbacks=[CommandHandler("cancel", mop_cancel)],
        allow_reentry=True,
    )
```

После `app.add_handler(setup_conv)` добавить:
```python
    app.add_handler(mop_conv)
```

- [ ] **Step 5: Обновить текст /start**

Найти строку:
```python
        "• /help — справка",
```

Заменить на:
```python
        "• /mop — привязка менеджеров к Telegram\n"
        "• /help — справка",
```

- [ ] **Step 6: Commit**

```bash
git add bot.py
git commit -m "feat: add /mop command for registering МОП Telegram IDs"
```

---

### Task 6: Wire up — подключить polling и переменные окружения

**Files:**
- Modify: `bot.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Добавить импорт в bot.py**

После строки `from reports import ...` добавить:
```python
from task_reminder import check_upcoming_tasks
```

- [ ] **Step 2: Зарегистрировать polling job в main()**

В `main()`, после блока `app.job_queue.run_daily(daily_task_report, ...)` добавить:

```python
    app.job_queue.run_repeating(
        check_upcoming_tasks,
        interval=300,
        first=60,
        name="check_upcoming_tasks",
    )
    log.info("Task reminder job scheduled every 5 minutes")
```

- [ ] **Step 3: Финальный requirements.txt**

Полное содержимое `requirements.txt`:
```
python-telegram-bot[job-queue]
httpx
python-dotenv
openpyxl
pytz
anthropic
pytest
pytest-asyncio
```

- [ ] **Step 4: Финальный .env.example**

Полное содержимое `.env.example`:
```
TELEGRAM_TOKEN=ВАШ_ТОКЕН_ОТ_BOTFATHER
BITRIX_WEBHOOK=https://amanat.bitrix24.kz/rest/55112/ou075j5sr749f3cl/
ADMIN_IDS=123456789
ANTHROPIC_API_KEY=sk-ant-...
```

- [ ] **Step 5: Запустить все тесты**

```bash
pip install -r requirements.txt -q
pytest tests/ -v
```

Ожидаемо: все тесты `PASSED`.

- [ ] **Step 6: Commit финальный**

```bash
git add bot.py requirements.txt .env.example
git commit -m "feat: wire up task reminder polling to bot job queue"
```

---

## Проверка после реализации

1. Добавить `ANTHROPIC_API_KEY` в реальный `.env`
2. Запустить бот: `python bot.py`
3. Выполнить `/mop` — привязать одного МОПа к тестовому Telegram
4. В Bitrix24 создать задачу: автор = МОП, привязана к сделке, дедлайн через 12 минут
5. Дождаться следующего цикла polling (≤5 мин) — должно прийти сообщение в Telegram МОПа

## Открытые вопросы (решаем при подключении к Bitrix24)

- `TYPE_ID` для звонков и визитов в конкретном портале (стандарт: звонок=2, встреча=1, но может отличаться)
- Где CoPilot сохраняет транскрипт — в `DESCRIPTION` активности или в отдельном поле
- Название клиента (контакт) — добавить в сообщение МОПу для удобства
