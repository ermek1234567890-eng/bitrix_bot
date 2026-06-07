import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bitrix import Bitrix
from db import get_all_mops, get_managers, is_reminder_sent, mark_reminder_sent, db_get
from ai_advisor import get_recommendation

log = logging.getLogger(__name__)

WINDOW_START_MIN = 8
WINDOW_END_MIN = 13

# Менеджеры, чьи задачи всегда дублируются в группу
GROUP_MANAGER_IDS = {
    68894,  # Салима
    68542,  # Ернур
    67442,  # Самал
    49464,  # Заир
}


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
    stage_name: str = "",
) -> str:
    deal_title = deal.get("TITLE", "Неизвестная сделка")
    display_stage = stage_name or deal.get("STAGE_ID", "Неизвестно")

    try:
        modify = deal.get("DATE_MODIFY", "")
        dt = datetime.fromisoformat(modify.replace("Z", "+00:00"))
        days_in_stage = (datetime.now(dt.tzinfo) - dt).days
        stage_str = f"{display_stage} ({days_in_stage} дн. в этапе)"
    except Exception:
        stage_str = display_stage

    if call:
        call_text = (call.get("DESCRIPTION") or call.get("SUBJECT") or "")[:400]
        try:
            call_dt = datetime.fromisoformat(call.get("START_TIME", "").replace("Z", "+00:00"))
            call_ago = f"{(datetime.now(call_dt.tzinfo) - call_dt).days} дн. назад"
        except Exception:
            call_ago = ""
        call_block = f"\n\n📞 <b>Последний звонок</b> ({call_ago}):\n{call_text}"
    else:
        call_block = "\n\n📞 <b>Последний звонок:</b> записей звонков нет"

    deal_url = f"{portal_url}/crm/deal/details/{deal_id}/"

    return (
        f"🔔 <b>Звонок через 10 минут</b>\n\n"
        f"🏠 <b>Сделка:</b> {deal_title}\n"
        f"📍 <b>Этап:</b> {stage_str}"
        f"{call_block}\n\n"
        f"🤖 <b>Рекомендация:</b>\n{recommendation}\n\n"
        f'🔗 <a href="{deal_url}">Открыть сделку</a>'
    )


async def process_task(
    bx: Bitrix, mgr: dict, task: dict, bot,
    group_chat_id: int = None, personal_tg_id: int = None,
) -> None:
    task_id = int(task.get("id") or task.get("ID") or 0)
    if not task_id:
        return
    if is_reminder_sent(task_id):
        return

    deal_id = extract_deal_id(task)
    if not deal_id:
        return

    bitrix_id = mgr.get("bitrix_id")
    mgr_name = mgr.get("short_name") or mgr.get("name", "")
    try:
        deal, call, visit = await asyncio.gather(
            bx.fetch_deal_detail(deal_id),
            bx.fetch_last_call(deal_id, responsible_id=bitrix_id),
            bx.fetch_last_visit(deal_id),
        )
    except Exception as e:
        log.error("Error fetching data for task %s: %s", task_id, e)
        return

    # Resolve stage name from Bitrix24
    try:
        stage_name = await bx.fetch_stage_name(
            deal.get("STAGE_ID", ""),
            deal.get("CATEGORY_ID", 0),
        )
    except Exception:
        stage_name = deal.get("STAGE_ID", "")

    try:
        recommendation = get_recommendation(deal, call, visit)
    except Exception as e:
        log.error("get_recommendation failed for task %s: %s", task_id, e)
        recommendation = "Рекомендация недоступна. Изучите историю клиента перед звонком."
    portal_url = bx.get_portal_url()
    text = format_reminder_message(deal, call, task, recommendation, portal_url, deal_id, stage_name)

    sent = False

    # 1. Send to МОП personally (if linked to Telegram)
    if personal_tg_id:
        try:
            await bot.send_message(
                chat_id=personal_tg_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent = True
            log.info("Personal reminder sent to %s for task %s", mgr_name, task_id)
        except Exception as e:
            log.error("Failed to send personal reminder to %s: %s", mgr_name, e)

    # 2. Always send to group chat
    if group_chat_id:
        try:
            group_text = f"👤 <b>{mgr_name}</b>\n\n{text}"
            await bot.send_message(
                chat_id=group_chat_id,
                text=group_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent = True
            log.info("Group reminder sent for %s, task %s", mgr_name, task_id)
        except Exception as e:
            log.error("Failed to send group reminder: %s", e)

    if sent:
        mark_reminder_sent(task_id)


async def check_upcoming_tasks(ctx) -> None:
    """Job queue callback — runs every 5 minutes.
    Checks GROUP_MANAGER_IDS for group chat. Sends personally to linked MOPs.
    """
    managers = get_managers()
    if not managers:
        log.warning("No managers in DB — skipping reminder check")
        return

    now = datetime.now(timezone.utc)
    window_start = now + timedelta(minutes=WINDOW_START_MIN)
    window_end = now + timedelta(minutes=WINDOW_END_MIN)

    # Group chat for reminders
    group_raw = db_get("mop_group_chat_id")
    group_chat_id = int(group_raw) if group_raw else None

    # Build lookup: bitrix_id → telegram_id (for personal sending)
    mop_tg = {m["bitrix_id"]: m["telegram_id"] for m in get_all_mops()}

    # Managers to check = GROUP_MANAGER_IDS + any linked MOPs
    check_ids = set(GROUP_MANAGER_IDS)
    check_ids.update(mop_tg.keys())

    # Filter to only relevant managers
    to_check = [m for m in managers if m["bitrix_id"] in check_ids]

    log.info(
        "Checking tasks: %d managers, window %s – %s UTC",
        len(to_check),
        window_start.strftime("%H:%M"),
        window_end.strftime("%H:%M"),
    )

    bx = Bitrix()
    bot = ctx.bot

    for mgr in to_check:
        bid = mgr["bitrix_id"]
        personal_tg_id = mop_tg.get(bid)  # None if not linked
        send_to_group = group_chat_id if bid in GROUP_MANAGER_IDS else None
        try:
            tasks = await bx.fetch_mop_upcoming_tasks(bid, window_start, window_end)
            if tasks:
                log.info("Found %d tasks for %s", len(tasks), mgr.get("short_name", mgr["name"]))
            for task in tasks:
                await process_task(bx, mgr, task, bot, send_to_group, personal_tg_id)
        except Exception as e:
            log.error("Error checking tasks for %s: %s", mgr.get("short_name", mgr["name"]), e)
