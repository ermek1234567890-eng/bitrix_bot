import logging
import os
from datetime import datetime
import anthropic

log = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    return _client


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
        client = _get_client()

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
        log.exception("AI advisor error for deal %s", deal.get("TITLE", "unknown"))
        return "Рекомендация недоступна. Изучите историю клиента перед звонком."
