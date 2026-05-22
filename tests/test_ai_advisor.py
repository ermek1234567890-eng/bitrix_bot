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

    import ai_advisor
    ai_advisor._client = None  # reset singleton so the mock is picked up
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_response(expected)
        result = ai_advisor.get_recommendation(deal, call, visit)

    assert "Уточни" in result


def test_get_recommendation_without_visit():
    deal = {"TITLE": "ЖК Керуен 1к", "STAGE_ID": "C1:NEW", "DATE_MODIFY": "2026-05-01T00:00:00"}
    call = {"DESCRIPTION": "Хочет скидку", "START_TIME": "2026-05-22T10:00:00"}

    import ai_advisor
    ai_advisor._client = None
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_response("• Работай со скидкой")
        result = ai_advisor.get_recommendation(deal, call, None)

    assert isinstance(result, str)
    assert len(result) > 0


def test_get_recommendation_returns_fallback_on_error():
    deal = {"TITLE": "Тест", "STAGE_ID": "C1:NEW", "DATE_MODIFY": "2026-05-01T00:00:00"}

    import ai_advisor
    ai_advisor._client = None
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = Exception("API error")
        result = ai_advisor.get_recommendation(deal, None, None)

    assert result == "Рекомендация недоступна. Изучите историю клиента перед звонком."
