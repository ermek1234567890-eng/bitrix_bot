import sqlite3
import os
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("DB_PATH", "bot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS managers (
                bitrix_id  INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                short_name TEXT,
                is_active  INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS projects (
                name      TEXT PRIMARY KEY,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS plans (
                year    INTEGER,
                month   INTEGER,
                project TEXT DEFAULT '',
                metric  TEXT,
                value   REAL DEFAULT 0,
                PRIMARY KEY (year, month, project, metric)
            );
        """)


# ---------- mop / reminders ----------

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
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminders_sent (task_id, sent_at) VALUES (?, ?)",
            (task_id, datetime.now(timezone.utc).isoformat())
        )


def cleanup_old_reminders(days: int = 7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM reminders_sent WHERE sent_at < ?",
            (cutoff,)
        )


# ---------- settings ----------

def db_get(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def db_set(key: str, value):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value) if value is not None else ""))


def db_all_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ---------- managers ----------

def get_managers() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM managers WHERE is_active=1 ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_manager(bitrix_id: int, name: str, short_name: str = None):
    sn = short_name or name.split()[0]
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO managers (bitrix_id, name, short_name, is_active)
            VALUES (?,?,?,1)
            ON CONFLICT(bitrix_id) DO UPDATE SET
                name=excluded.name,
                short_name=excluded.short_name
        """, (bitrix_id, name, sn))


def toggle_manager(bitrix_id: int, active: int):
    with get_conn() as conn:
        conn.execute("UPDATE managers SET is_active=? WHERE bitrix_id=?", (active, bitrix_id))


def find_managers_by_names(short_names: list) -> dict:
    """
    Find managers by short_name containing any of the given names.
    Returns dict: {search_name: manager_row or None}
    """
    result = {}
    with get_conn() as conn:
        for name in short_names:
            row = conn.execute(
                "SELECT bitrix_id, name, short_name FROM managers WHERE short_name LIKE ? AND is_active=1 LIMIT 1",
                (f"%{name}%",)
            ).fetchone()
            result[name] = dict(row) if row else None
    return result


# ---------- projects ----------

def get_projects() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, enum_id FROM projects WHERE is_active=1 ORDER BY name"
        ).fetchall()
    return [{"name": r["name"], "enum_id": r["enum_id"]} for r in rows]


def get_project_enum_id(name: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT enum_id FROM projects WHERE name=?", (name,)
        ).fetchone()
    return row["enum_id"] if row and row["enum_id"] else None


def upsert_project(name: str, enum_id: str = None):
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN enum_id TEXT")
        except Exception:
            pass
        conn.execute(
            "INSERT OR IGNORE INTO projects (name, is_active) VALUES (?,1)", (name,)
        )
        if enum_id:
            conn.execute("UPDATE projects SET enum_id=? WHERE name=?", (enum_id, name))


# ---------- plans ----------

def get_plan(year: int, month: int, project: str, metric: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM plans WHERE year=? AND month=? AND project=? AND metric=?",
            (year, month, project, metric)
        ).fetchone()
    return float(row["value"]) if row else 0.0


def set_plan(year: int, month: int, project: str, metric: str, value: float):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO plans VALUES (?,?,?,?,?)
            ON CONFLICT(year,month,project,metric) DO UPDATE SET value=excluded.value
        """, (year, month, project, metric, value))


def get_plans_dict(year: int, month: int, project: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT metric, value FROM plans WHERE year=? AND month=? AND project=?",
            (year, month, project)
        ).fetchall()
    return {r["metric"]: float(r["value"]) for r in rows}
