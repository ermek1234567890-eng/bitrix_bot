"""
МОП Task Reminder Bot (@MOP_zadachi_bot)
Sends personal reminders to sales managers before task deadlines.
"""
import asyncio
import logging
import os
from datetime import time as dtime

import pytz
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from db import (
    init_db, init_mop_tables, db_get, db_set,
    get_managers, upsert_mop, get_all_mops,
    cleanup_old_reminders,
)
from task_reminder import check_upcoming_tasks, format_reminder_message

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

TOKEN = os.getenv("MOP_BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

MOP_PICK, MOP_TG_ID = range(2)


def is_admin(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS


def kb(buttons: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in buttons]
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        db_set("mop_group_chat_id", str(chat.id))
        log.info("Group chat_id saved: %s", chat.id)
    await update.message.reply_text(
        "👋 <b>МОП Reminder Bot</b>\n\n"
        "Этот бот отправляет менеджерам напоминания о задачах "
        "за 10 минут до дедлайна + AI-рекомендацию по клиенту.\n\n"
        "Команды:\n"
        "• /mop — привязать менеджера к Telegram\n"
        "• /list — список привязанных МОПов\n"
        "• /test — отправить тестовое напоминание\n"
        "• /help — справка",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Справка</b>\n\n"
        "<b>/mop</b> — привязать менеджера из Bitrix24 к его Telegram ID\n"
        "<b>/list</b> — показать список привязанных МОПов\n\n"
        "<b>Как работает:</b>\n"
        "Бот каждые 5 минут проверяет задачи МОПов в Bitrix24.\n"
        "Если до дедлайна осталось ~10 минут — отправляет напоминание "
        "в личный чат менеджера с AI-рекомендацией по клиенту.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mops = get_all_mops()
    if not mops:
        await update.message.reply_text("Нет привязанных МОПов. Используйте /mop")
        return
    lines = ["👥 <b>Привязанные МОПы:</b>\n"]
    for m in mops:
        lines.append(f"• {m['name']} — tg:<code>{m['telegram_id']}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─── /test command ───────────────────────────────────────────────────────────

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Find the nearest task for any manager and send a test reminder here."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return

    msg = await update.message.reply_text("⏳ Ищу ближайшую задачу в Bitrix24...")

    from datetime import datetime, timedelta, timezone
    from bitrix import Bitrix
    from ai_advisor import get_recommendation

    bx = Bitrix()
    managers = get_managers()
    if not managers:
        await msg.edit_text("⚠️ Нет менеджеров. Загрузите через отчётного бота /managers")
        return

    # Look for tasks with deadline in next 24h for any manager
    now = datetime.now(timezone.utc)
    window_start = now
    window_end = now + timedelta(hours=24)

    found_task = None
    found_mgr = None
    for m in managers:
        try:
            tasks = await bx.fetch_mop_upcoming_tasks(m["bitrix_id"], window_start, window_end)
            if tasks:
                found_task = tasks[0]
                found_mgr = m
                break
        except Exception:
            continue

    if not found_task:
        await msg.edit_text(
            "❌ Не найдено задач с дедлайном в ближайшие 24 часа.\n\n"
            "Для теста создайте задачу в Bitrix24:\n"
            "1. Создайте задачу от имени МОПа\n"
            "2. Привяжите её к сделке\n"
            "3. Поставьте дедлайн через 10-15 минут"
        )
        return

    await msg.edit_text(f"⏳ Найдена задача от {found_mgr['short_name']}. Генерирую рекомендацию...")

    # Build reminder message
    from task_reminder import extract_deal_id
    deal_id = extract_deal_id(found_task)
    if not deal_id:
        await msg.edit_text("❌ У задачи нет привязанной сделки.")
        return

    try:
        deal, call, visit = await asyncio.gather(
            bx.fetch_deal_detail(deal_id),
            bx.fetch_last_call(deal_id, responsible_id=found_mgr["bitrix_id"]),
            bx.fetch_last_visit(deal_id),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка загрузки данных: {e}")
        return

    try:
        recommendation = get_recommendation(deal, call, visit)
    except Exception:
        recommendation = "Рекомендация недоступна."

    portal_url = bx.get_portal_url()
    text = format_reminder_message(deal, call, found_task, recommendation, portal_url, deal_id)

    header = f"🧪 <b>ТЕСТ</b> | Менеджер: {found_mgr['short_name']}\n\n"
    await msg.edit_text(header + text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ─── /mop conversation ──────────────────────────────────────────────────────

async def cmd_mop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END

    managers = get_managers()
    if not managers:
        await update.message.reply_text("⚠️ Нет менеджеров в базе. Загрузите через основного бота /managers")
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
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END

    _, bitrix_id, name = q.data.split(":", 2)
    ctx.user_data["mop_bitrix_id"] = int(bitrix_id)
    ctx.user_data["mop_name"] = name
    await q.edit_message_text(
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


# ─── Scheduled jobs ──────────────────────────────────────────────────────────

async def _daily_cleanup(ctx):
    cleanup_old_reminders(days=7)
    log.info("Old reminders cleaned up")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    init_mop_tables()
    app = Application.builder().token(TOKEN).build()

    mop_conv = ConversationHandler(
        entry_points=[CommandHandler("mop", cmd_mop)],
        states={
            MOP_PICK: [CallbackQueryHandler(mop_pick, pattern="^mop:")],
            MOP_TG_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, mop_tg_id)],
        },
        fallbacks=[CommandHandler("cancel", mop_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(mop_conv)

    almaty = pytz.timezone("Asia/Almaty")
    app.job_queue.run_repeating(
        check_upcoming_tasks,
        interval=300,
        first=60,
        name="check_upcoming_tasks",
    )
    log.info("Task reminder job scheduled every 5 minutes")

    app.job_queue.run_daily(
        _daily_cleanup,
        time=dtime(3, 0, tzinfo=almaty),
        name="cleanup_old_reminders",
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
