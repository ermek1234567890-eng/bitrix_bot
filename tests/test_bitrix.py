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
    # API returns results in DESC order; first item is most recent.
    activities = [
        {"ID": "11", "TYPE_ID": "2", "START_TIME": "2026-05-22T10:00:00", "DESCRIPTION": "Звонок 2"},
        {"ID": "10", "TYPE_ID": "2", "START_TIME": "2026-05-20T10:00:00", "DESCRIPTION": "Звонок 1"},
    ]
    with patch.object(bx, "call", new=AsyncMock(return_value={"result": activities})):
        result = await bx.fetch_last_call(100)
    assert result["ID"] == "11"


async def test_fetch_last_call_returns_none_when_empty(bx):
    with patch.object(bx, "call", new=AsyncMock(return_value={"result": []})):
        result = await bx.fetch_last_call(100)
    assert result is None


async def test_fetch_last_visit_returns_most_recent(bx):
    activities = [
        {"ID": "21", "TYPE_ID": "1", "START_TIME": "2026-05-18T10:00:00", "DESCRIPTION": "Визит 2"},
        {"ID": "20", "TYPE_ID": "1", "START_TIME": "2026-05-15T10:00:00", "DESCRIPTION": "Визит 1"},
    ]
    with patch.object(bx, "call", new=AsyncMock(return_value={"result": activities})):
        result = await bx.fetch_last_visit(100)
    assert result["ID"] == "21"


async def test_fetch_last_visit_returns_none_when_empty(bx):
    with patch.object(bx, "call", new=AsyncMock(return_value={"result": []})):
        result = await bx.fetch_last_visit(100)
    assert result is None


async def test_fetch_deal_detail(bx):
    deal = {"ID": "100", "TITLE": "ЖК Керуен 2к", "STAGE_ID": "C1:1", "DATE_MODIFY": "2026-05-09T10:00:00"}
    with patch.object(bx, "call", new=AsyncMock(return_value={"result": deal})):
        result = await bx.fetch_deal_detail(100)
    assert result["TITLE"] == "ЖК Керуен 2к"
    assert result["STAGE_ID"] == "C1:1"
