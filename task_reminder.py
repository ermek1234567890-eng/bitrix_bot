import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bitrix import Bitrix
from db import get_all_mops, is_reminder_sent, mark_reminder_sent
from ai_advisor import get_recommendation

log = logging.getLogger(__name__)

WINDOW_START_MIN = 8
WINDOW_END_MIN = 13


def extract_deal_id(task: dict):
    """Return the first deal ID linked to the task, or None."""
    ids = Bitrix._extract_deal_ids([task])
    return ids[0] if ids else None


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
    task_id = int(task.get("id") or task.get("ID") or 0)
    if not task_id:
        return
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

    try:
        recommendation = get_recommendation(deal, call, visit)
    except Exception as e:
        log.error("get_recommendation failed for task %s: %s", task_id, e)
        recommendation = "Рекомендация недоступна. Изучите историю клиента перед звонком."
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
