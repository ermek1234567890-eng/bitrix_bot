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
