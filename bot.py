"""
Bitrix24 Report Bot
Команды: /start /report /setplan /setup /managers /help
"""
import asyncio
import logging
import os
from datetime import date, timedelta, time as dtime
import calendar
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
    init_db, db_get, db_set,
    get_managers, upsert_manager, toggle_manager,
    get_projects, upsert_project, get_project_enum_id,
    set_plan, get_plan,
    find_managers_by_names,
    upsert_mop, get_all_mops, init_mop_tables,
)
from bitrix import Bitrix
from reports import compute_metrics, format_regular_report, format_plan_fact_report, METRICS
from excel_report import generate_regular_excel, generate_planfact_excel
from telegram import InputFile

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# ─── Conversation states ───────────────────────────────────────────────────────
(
    RPT_TYPE, RPT_DATE_PRESET, RPT_DATE_FROM, RPT_DATE_TO,
    RPT_MANAGERS, RPT_PROJECT, RPT_CONFIRM,
) = range(7)

(
    PLAN_MONTH, PLAN_PROJECT, PLAN_METRIC, PLAN_VALUE,
) = range(10, 14)

(
    SETUP_MENU, SETUP_FIELD_PICK, SETUP_FIELD_VALUE,
    SETUP_PIPELINE_PICK, SETUP_STAGE_PICK, SETUP_STAGE_TYPE,
    SETUP_USER_PICK,
) = range(20, 27)

(
    MOP_PICK, MOP_TG_ID,
) = range(30, 32)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_admin(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS


def kb(buttons: list) -> InlineKeyboardMarkup:
    """Build inline keyboard from list of (text, callback_data) rows (each row = list)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in buttons]
    )


def _month_name(m: int) -> str:
    names = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    return names[m]


async def safe_edit(query, text, reply_markup=None, parse_mode=ParseMode.HTML):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_admin(uid):
        chat_id = str(update.effective_chat.id)
        db_set("task_chat_id", chat_id)
        log.info("task_chat_id saved: %s", chat_id)
    await update.message.reply_text(
        "👋 <b>Bitrix24 Report Bot</b>\n\n"
        "Команды:\n"
        "• /report — сформировать отчёт\n"
        "• /tasks — отчёт по задачам\n"
        "• /setplan — ввести плановые цифры\n"
        "• /managers — управление менеджерами\n"
        "• /setup — настройка полей Bitrix24\n"
        "• /mop — привязка менеджеров к Telegram\n"
        "• /help — справка",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Справка</b>\n\n"
        "<b>/report</b> — выбрать тип отчёта, период, менеджеров и проект\n"
        "<b>/setplan</b> — ввести плановые цифры на месяц\n"
        "<b>/managers</b> — синхронизировать менеджеров из Bitrix24\n"
        "<b>/setup</b> — настроить поля и воронки (для администратора)\n\n"
        "<b>Первый запуск:</b>\n"
        "1. /setup → Поля — укажите поля Bitrix24\n"
        "2. /setup → Воронки — укажите воронки продаж\n"
        "3. /managers — загрузите менеджеров\n"
        "4. /report — готово!",
        parse_mode=ParseMode.HTML,
    )

# ─── /report conversation ─────────────────────────────────────────────────────

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["sel_managers"] = []
    await update.message.reply_text(
        "📊 <b>Новый отчёт</b>\nВыберите тип:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([
            [("Регулярный менеджмент", "rpt:regular"),
             ("План vs Факт", "rpt:planfact")],
            [("❌ Отмена", "rpt:cancel")],
        ]),
    )
    return RPT_TYPE


async def rpt_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "rpt:cancel":
        await safe_edit(q, "Отменено.")
        return ConversationHandler.END

    ctx.user_data["rpt_type"] = q.data.split(":")[1]
    today = date.today()
    cur_week_start = today - timedelta(days=today.weekday())
    prev_week_start = cur_week_start - timedelta(days=7)
    prev_week_end = cur_week_start - timedelta(days=1)
    first_cur = date(today.year, today.month, 1)
    last_cur = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    prev_month = (first_cur - timedelta(days=1))
    first_prev = date(prev_month.year, prev_month.month, 1)
    last_prev = prev_month

    ctx.user_data["presets"] = {
        "week": (prev_week_start.isoformat(), prev_week_end.isoformat()),
        "month": (first_cur.isoformat(), last_cur.isoformat()),
        "prev_month": (first_prev.isoformat(), last_prev.isoformat()),
    }

    await safe_edit(
        q,
        "📅 Выберите период:",
        kb([
            [("Прошлая неделя", "preset:week"), ("Текущий месяц", "preset:month")],
            [("Прошлый месяц", "preset:prev_month"), ("Свой диапазон", "preset:custom")],
            [("⬅️ Назад", "rpt:back_type")],
        ]),
    )
    return RPT_DATE_PRESET


async def rpt_date_preset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "rpt:back_type":
        return await cmd_report_from_query(q, ctx)

    key = q.data.split(":")[1]
    if key == "custom":
        await safe_edit(q, "📅 Введите <b>начальную дату</b> в формате ДД.ММ.ГГГГ:", parse_mode=ParseMode.HTML)
        return RPT_DATE_FROM

    df, dt = ctx.user_data["presets"][key]
    ctx.user_data["date_from"] = df
    ctx.user_data["date_to"] = dt
    return await _show_manager_selector(q, ctx)


async def rpt_date_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        d = _parse_date(txt)
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введите дату как ДД.ММ.ГГГГ")
        return RPT_DATE_FROM
    ctx.user_data["date_from"] = d.isoformat()
    await update.message.reply_text("📅 Введите <b>конечную дату</b> в формате ДД.ММ.ГГГГ:", parse_mode=ParseMode.HTML)
    return RPT_DATE_TO


async def rpt_date_to(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        d = _parse_date(txt)
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введите дату как ДД.ММ.ГГГГ")
        return RPT_DATE_TO
    ctx.user_data["date_to"] = d.isoformat()
    msg = await update.message.reply_text("⏳ Загружаю менеджеров...")
    return await _show_manager_selector_msg(msg, ctx)


def _parse_date(txt: str) -> date:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return date(*[int(x) for x in __import__("re").split(r"[.\-/]", txt)][::-1]
                        if False else [])
        except Exception:
            pass
    # try all formats
    import datetime as dt_mod
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt_mod.datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {txt}")


async def _show_manager_selector(q, ctx):
    managers = get_managers()
    if not managers:
        await safe_edit(q, "⚠️ Нет менеджеров. Используйте /managers для синхронизации.")
        return ConversationHandler.END
    ctx.user_data["all_managers"] = managers
    ctx.user_data["sel_managers"] = []
    return await _render_manager_selector(q, ctx, edit=True)


async def _show_manager_selector_msg(msg, ctx):
    managers = get_managers()
    if not managers:
        await msg.edit_text("⚠️ Нет менеджеров. Используйте /managers для синхронизации.")
        return ConversationHandler.END
    ctx.user_data["all_managers"] = managers
    ctx.user_data["sel_managers"] = []
    return await _render_manager_selector(msg, ctx, edit=True)


async def _render_manager_selector(q_or_msg, ctx, edit=True):
    managers = ctx.user_data.get("all_managers", [])
    selected = set(ctx.user_data.get("sel_managers", []))
    sel_names = ", ".join(
        m["short_name"] for m in managers if m["bitrix_id"] in selected
    ) or "все"
    rows = [
        [("➡️ Далее →", "mgr:done")],
        [("☑️ Все менеджеры", "mgr:all")],
    ]
    for m in managers:
        mid = m["bitrix_id"]
        check = "✅" if mid in selected else "☐"
        rows.append([(f"{check} {m['short_name']}", f"mgr:{mid}")])
    markup = kb(rows)
    text = f"👥 <b>Выберите менеджеров</b>\nВыбрано: <i>{sel_names}</i>\n\n➡️ Далее — вверху списка"
    if edit:
        try:
            await q_or_msg.edit_message_text(text, reply_markup=markup)
        except Exception:
            await q_or_msg.reply_text(text, reply_markup=markup)
    else:
        await q_or_msg.reply_text(text, reply_markup=markup)
    return RPT_MANAGERS


async def rpt_managers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sel = ctx.user_data.get("sel_managers", [])
    managers = ctx.user_data.get("all_managers", [])
    valid_ids = {m["bitrix_id"] for m in managers}

    if q.data == "mgr:all":
        all_ids = [m["bitrix_id"] for m in managers]
        ctx.user_data["sel_managers"] = all_ids
        await q.answer("Выбраны все менеджеры")
        return await _render_manager_selector(q, ctx, edit=True)
    elif q.data == "mgr:done":
        return await _show_project_selector(q, ctx)
    else:
        mid = int(q.data.split(":")[1])
        if mid in sel:
            sel.remove(mid)
        else:
            sel.append(mid)
        ctx.user_data["sel_managers"] = sel
        return await _render_manager_selector(q, ctx, edit=True)


async def _show_project_selector(q, ctx):
    projects = get_projects()
    if not projects:
        ctx.user_data["sel_projects"] = []
        return await _generate_report(q, ctx)
    ctx.user_data["all_projects"] = projects
    ctx.user_data["sel_projects"] = []
    return await _render_project_selector(q, ctx)


async def _render_project_selector(q, ctx):
    projects = ctx.user_data.get("all_projects", [])
    selected = set(ctx.user_data.get("sel_projects", []))
    sel_names = ", ".join(selected) if selected else "все"
    rows = [
        [("➡️ Далее →", "proj:done")],
        [("☑️ Все проекты", "proj:ALL")],
    ]
    for p in projects:
        check = "✅" if p["name"] in selected else "☐"
        rows.append([(f"{check} {p['name']}", f"proj:{p['name']}")])
    await safe_edit(q, f"🗂 <b>Выберите проекты</b>\nВыбрано: <i>{sel_names}</i>", kb(rows), parse_mode=ParseMode.HTML)
    return RPT_PROJECT


async def rpt_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split(":", 1)[1]
    sel = ctx.user_data.get("sel_projects", [])

    if val == "done":
        return await _generate_report(q, ctx)
    elif val == "ALL":
        ctx.user_data["sel_projects"] = []
        return await _render_project_selector(q, ctx)
    else:
        if val in sel:
            sel.remove(val)
        else:
            sel.append(val)
        ctx.user_data["sel_projects"] = sel
        return await _render_project_selector(q, ctx)


async def _generate_report(q, ctx):
    date_from = ctx.user_data["date_from"]
    date_to = ctx.user_data["date_to"]
    manager_ids = ctx.user_data.get("sel_managers", [])
    sel_projects = ctx.user_data.get("sel_projects", [])
    rpt_type = ctx.user_data.get("rpt_type", "regular")

    # Build list of enum IDs for selected projects
    project_enum_ids = [get_project_enum_id(p) for p in sel_projects if get_project_enum_id(p)]
    proj_label = ", ".join(sel_projects) if sel_projects else "Все проекты"

    await safe_edit(q, "⏳ Загружаю данные из Bitrix24...")

    try:
        data, err = await asyncio.wait_for(
            compute_metrics(date_from, date_to, manager_ids, project_enum_ids or None),
            timeout=90.0
        )
    except asyncio.TimeoutError:
        await safe_edit(q, "⏱ Bitrix24 не отвечает. Попробуйте ещё раз.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception as e:
        log.exception("compute_metrics error")
        await safe_edit(q, f"❌ Ошибка загрузки данных:\n<code>{e}</code>", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    try:
        if err:
            await safe_edit(q, err, parse_mode=ParseMode.HTML)
            return ConversationHandler.END

        managers = ctx.user_data.get("all_managers", get_managers())
        manager_map = {}
        for m in managers:
            manager_map[m["bitrix_id"]] = m.get("short_name") or m["name"].split()[0]

        from datetime import date as dclass
        df = dclass.fromisoformat(date_from)

        if rpt_type == "regular":
            # Short text summary
            total = data.get("total", {})
            meets = total.get("meetings", 0)
            dfb   = total.get("dfb", 0)
            cr3   = f"{round(dfb/meets*100)}%" if meets else "0%"
            summary = (
                f"📊 <b>Регулярный менеджмент</b>\n"
                f"🗂 {proj_label} | 📅 {date_from} — {date_to}\n\n"
                f"Встречи: <b>{meets}</b> | ДФБ: <b>{dfb}</b> | CR3: <b>{cr3}</b>\n\n"
                f"📎 Полный отчёт в файле Excel ниже"
            )
            await safe_edit(q, summary, parse_mode=ParseMode.HTML)
            xlsx = generate_regular_excel(data, manager_map, date_from, date_to, proj_label)
            fname = f"report_{date_from}_{date_to}.xlsx"
            await q.message.reply_document(document=InputFile(xlsx, filename=fname))

        else:
            summary = (
                f"📈 <b>План vs Факт</b>\n"
                f"🗂 {proj_label} | 📅 {date_from} — {date_to}\n\n"
                f"📎 Отчёт в файле Excel ниже"
            )
            await safe_edit(q, summary, parse_mode=ParseMode.HTML)
            xlsx = generate_planfact_excel(data, date_from, date_to, df.year, df.month, proj_label)
            fname = f"plan_fact_{date_from}_{date_to}.xlsx"
            await q.message.reply_document(document=InputFile(xlsx, filename=fname))

    except Exception as e:
        log.exception("Report generation error")
        await safe_edit(q, f"❌ Ошибка:\n<code>{e}</code>", parse_mode=ParseMode.HTML)

    return ConversationHandler.END


async def cmd_report_from_query(q, ctx):
    ctx.user_data.clear()
    ctx.user_data["sel_managers"] = []
    await safe_edit(
        q,
        "📊 <b>Новый отчёт</b>\nВыберите тип:",
        kb([
            [("Регулярный менеджмент", "rpt:regular"),
             ("План vs Факт", "rpt:planfact")],
            [("❌ Отмена", "rpt:cancel")],
        ]),
    )
    return RPT_TYPE


async def rpt_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ─── Task report ──────────────────────────────────────────────────────────────

TASK_MANAGERS = ["Самал", "Заир", "Ернур", "Салима"]
_TASK_NAME_W  = max(len(n) for n in TASK_MANAGERS)   # 6 (Салима)
_FIG          = " "   # figure space — ширина одной цифры


def _rpad(n, width=3) -> str:
    """Right-align number to `width` digit-widths using figure spaces."""
    s = str(n)
    return _FIG * (width - len(s)) + s


def _npad(name) -> str:
    """Pad name to fixed width with regular spaces."""
    return name + " " * (_TASK_NAME_W - len(name))


async def build_task_report():
    """
    Fetch counts for all managers in parallel.
    Returns (text, markup) where each manager is ONE clickable button-row.
    """
    bx = Bitrix()
    today = date.today().isoformat()
    managers = find_managers_by_names(TASK_MANAGERS)

    async def get_manager_data(label):
        mgr = managers.get(label)
        if not mgr:
            return label, None
        bid = mgr["bitrix_id"]
        try:
            counts = await bx.fetch_task_counts(bid, today)
        except Exception:
            counts = {"overdue": -1, "today": -1}
        try:
            no_task_deals = await bx.fetch_deals_no_tasks(bid)
        except Exception:
            no_task_deals = []
        return label, {
            "overdue":  counts.get("overdue", -1),
            "today":    counts.get("today", -1),
            "no_tasks": len(no_task_deals) if isinstance(no_task_deals, list) else 0,
            "bitrix_id": bid,
        }

    # Sequential — retry logic in call() handles any 429s
    results = []
    for label in TASK_MANAGERS:
        results.append(await get_manager_data(label))

    date_str = date.today().strftime("%d.%m.%Y")
    # Only header — all data is in the clickable button rows below
    text = f"📋 <b>Отчёт по задачам — {date_str}</b>"

    keyboard_rows = []
    for label, data in results:
        if data is None:
            keyboard_rows.append([
                InlineKeyboardButton(f"👤 {label} — нет в базе", callback_data="tsk:none")
            ])
            continue

        ovd  = data["overdue"]
        tdy  = data["today"]
        ntsk = data["no_tasks"]
        bid  = data["bitrix_id"]
        ovd_s = str(ovd)  if ovd  >= 0 else "?"
        tdy_s = str(tdy)  if tdy  >= 0 else "?"

        btn = f"👤 {_npad(label)}  📍{_rpad(ovd_s)}  📅{_rpad(tdy_s)}  ⬛{_rpad(ntsk)}"
        keyboard_rows.append([
            InlineKeyboardButton(btn, callback_data=f"tsk:{bid}:{ovd_s}:{tdy_s}:{ntsk}")
        ])

    markup = InlineKeyboardMarkup(keyboard_rows)
    return text, markup


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Загружаю задачи из Bitrix24...")
    try:
        text, markup = await asyncio.wait_for(build_task_report(), timeout=90.0)
        ctx.bot_data["last_task_report"] = (text, markup)   # кэш
        await msg.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Bitrix24 не отвечает. Попробуйте позже.")
    except Exception as e:
        log.exception("cmd_tasks error")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def task_drilldown_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Handles all tsk: callbacks.

    Callback data shapes:
      tsk:back                    → regenerate main report
      tsk:none                    → manager not found, ignore
      tsk:{bid}:{ovd}:{tdy}:{ntsk} → show sub-menu for that manager
      tsk:{bid}:o/t/n             → fetch & show deal list
    """
    q = update.callback_query
    await q.answer()

    parts = q.data.split(":")

    # ── Ignore ─────────────────────────────────────────────────────────────────
    if parts[1] == "none":
        return

    # ── Back to summary ────────────────────────────────────────────────────────
    if parts[1] == "back":
        cached = ctx.bot_data.get("last_task_report")
        if cached:
            # Мгновенно — берём из кэша
            text, markup = cached
            await safe_edit(q, text, markup, parse_mode=ParseMode.HTML)
        else:
            # Кэш пуст (перезапуск бота) — грузим заново
            await safe_edit(q, "⏳ Обновляю отчёт...")
            try:
                text, markup = await asyncio.wait_for(build_task_report(), timeout=90.0)
                ctx.bot_data["last_task_report"] = (text, markup)
                await safe_edit(q, text, markup, parse_mode=ParseMode.HTML)
            except Exception as e:
                await safe_edit(q, f"❌ Ошибка: {e}")
        return

    try:
        bid = int(parts[1])
    except (ValueError, IndexError):
        return

    managers  = find_managers_by_names(TASK_MANAGERS)
    mgr_label = next(
        (l for l, m in managers.items() if m and m["bitrix_id"] == bid),
        f"ID:{bid}"
    )

    # ── Manager row clicked → sub-menu (5 parts: tsk:bid:ovd:tdy:ntsk) ────────
    if len(parts) == 5:
        ovd_s, tdy_s, ntsk_s = parts[2], parts[3], parts[4]
        submenu = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📍  Просроченные — {ovd_s}",  callback_data=f"tsk:{bid}:o")],
            [InlineKeyboardButton(f"📅  На сегодня — {tdy_s}",    callback_data=f"tsk:{bid}:t")],
            [InlineKeyboardButton(f"⬛  Без задач — {ntsk_s}",    callback_data=f"tsk:{bid}:n")],
            [InlineKeyboardButton("⬅️  Назад к отчёту",           callback_data="tsk:back")],
        ])
        await safe_edit(
            q,
            f"👤 <b>{mgr_label}</b> — выберите категорию:",
            submenu,
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Category chosen → deal list (3 parts: tsk:bid:type) ───────────────────
    if len(parts) != 3:
        return

    filter_type = parts[2]   # o / t / n
    type_labels = {"o": "просроченные", "t": "на сегодня", "n": "без задач"}
    type_label  = type_labels.get(filter_type, "")

    await safe_edit(
        q,
        f"⏳ Загружаю: <b>{mgr_label} — {type_label}</b>...",
        parse_mode=ParseMode.HTML,
    )

    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️  Назад к отчёту", callback_data="tsk:back")
    ]])

    try:
        bx     = Bitrix()
        today  = date.today().isoformat()
        portal = bx.get_portal_url()

        if filter_type in ("o", "t"):
            ft       = "overdue" if filter_type == "o" else "today"
            deal_ids = await asyncio.wait_for(
                bx.fetch_deal_ids_from_tasks(bid, ft, today), timeout=30.0
            )
            deals = await asyncio.wait_for(
                bx.fetch_deals_by_ids(deal_ids), timeout=30.0
            ) if deal_ids else []
        else:
            deals = await asyncio.wait_for(
                bx.fetch_deals_no_tasks(bid), timeout=60.0
            )

        if not deals:
            await safe_edit(
                q,
                f"✅ <b>{mgr_label} — {type_label}</b>\n\nСделок нет!",
                back_kb, parse_mode=ParseMode.HTML,
            )
            return

        MAX_DEALS = 40
        header = f"📋 <b>{mgr_label} — {type_label}</b> ({len(deals)} сд.)\n\n"
        lines  = []
        for d in deals[:MAX_DEALS]:
            did   = d.get("ID") or d.get("id") or ""
            title = (d.get("TITLE") or d.get("title") or f"Сделка #{did}").strip()
            url   = f"{portal}/crm/deal/details/{did}/"
            lines.append(f'• <a href="{url}">{title}</a>')

        text = header + "\n".join(lines)
        if len(deals) > MAX_DEALS:
            text += f"\n\n<i>...и ещё {len(deals) - MAX_DEALS} сделок</i>"

        await safe_edit(q, text, back_kb, parse_mode=ParseMode.HTML)

    except asyncio.TimeoutError:
        await safe_edit(q, "⏱ Timeout. Попробуйте ещё раз.", back_kb)
    except Exception as e:
        log.exception("task_drilldown_callback error")
        await safe_edit(q, f"❌ Ошибка: {e}", back_kb)


async def daily_task_report(ctx: ContextTypes.DEFAULT_TYPE):
    """Scheduled job — sends task report to admin chat."""
    chat_id = db_get("task_chat_id")
    if not chat_id:
        log.warning("daily_task_report: task_chat_id not set, skipping")
        return
    try:
        text, markup = await asyncio.wait_for(build_task_report(), timeout=90.0)
        ctx.bot_data["last_task_report"] = (text, markup)   # кэш
        await ctx.bot.send_message(
            chat_id=int(chat_id), text=text,
            reply_markup=markup, parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception("daily_task_report error")
        try:
            await ctx.bot.send_message(
                chat_id=int(chat_id),
                text=f"❌ Ошибка отчёта по задачам: {e}",
            )
        except Exception:
            pass


# ─── /managers ────────────────────────────────────────────────────────────────

async def cmd_managers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Загружаю менеджеров отдела продаж из Bitrix24...")
    try:
        bx = Bitrix()
        users = await bx.get_users(positions=["Менеджер отдела продаж", "Менеджер ОП"])
        count = 0
        for u in users:
            if not u.get("ID"):
                continue
            name = f"{u.get('LAST_NAME', '')} {u.get('NAME', '')}".strip()
            if not name:
                name = u.get("LOGIN", f"User_{u['ID']}")
            short = u.get("NAME", name.split()[0] if name else str(u["ID"]))
            upsert_manager(int(u["ID"]), name, short)
            count += 1
        await msg.edit_text(
            f"✅ Загружено <b>{count}</b> менеджеров отдела продаж.\n"
            f"Используйте /report для отчётов.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

# ─── /setplan conversation ─────────────────────────────────────────────────────

async def cmd_setplan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    ctx.user_data.clear()
    today = date.today()
    rows = []
    for delta in range(3):
        m = today.month - delta
        y = today.year
        if m <= 0:
            m += 12
            y -= 1
        rows.append([(f"{_month_name(m)} {y}", f"planm:{y}:{m}")])
    rows.append([("❌ Отмена", "planm:cancel")])
    await update.message.reply_text("📅 Выберите месяц для планов:", reply_markup=kb(rows))
    return PLAN_MONTH


async def plan_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "planm:cancel":
        await safe_edit(q, "Отменено.")
        return ConversationHandler.END
    _, y, m = q.data.split(":")
    ctx.user_data["plan_year"] = int(y)
    ctx.user_data["plan_month"] = int(m)

    projects = get_projects()
    rows = [[("Общий (без проекта)", "planp:__")]]
    for p in projects:
        rows.append([(p, f"planp:{p}")])
    rows.append([("❌ Отмена", "planp:cancel")])
    await safe_edit(q, "🗂 Выберите проект:", kb(rows))
    return PLAN_PROJECT


async def plan_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "planp:cancel":
        await safe_edit(q, "Отменено.")
        return ConversationHandler.END
    val = q.data.split(":", 1)[1]
    ctx.user_data["plan_project"] = "" if val == "__" else val
    ctx.user_data["plan_idx"] = 0
    ctx.user_data["plan_skippable"] = [k for k, _ in METRICS if k != "cr3"]
    return await _ask_plan_metric(q, ctx)


async def _ask_plan_metric(q, ctx):
    idx = ctx.user_data.get("plan_idx", 0)
    keys = ctx.user_data["plan_skippable"]
    if idx >= len(keys):
        y = ctx.user_data["plan_year"]
        m = ctx.user_data["plan_month"]
        proj = ctx.user_data["plan_project"]
        await safe_edit(q, f"✅ Планы на <b>{_month_name(m)} {y}</b> ({proj or 'общий'}) сохранены!", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    key = keys[idx]
    label = next(l for k, l in METRICS if k == key)
    y = ctx.user_data["plan_year"]
    m = ctx.user_data["plan_month"]
    proj = ctx.user_data["plan_project"]
    cur = get_plan(y, m, proj, key)
    await safe_edit(
        q,
        f"📝 <b>{label}</b>\nТекущий план: <code>{int(cur) if cur else '—'}</code>\n\nВведите новое значение (или 0 чтобы пропустить):",
        parse_mode=ParseMode.HTML,
    )
    ctx.user_data["plan_cur_metric"] = key
    return PLAN_VALUE


async def plan_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        val = float(txt)
    except ValueError:
        await update.message.reply_text("❌ Введите число.")
        return PLAN_VALUE

    y = ctx.user_data["plan_year"]
    m = ctx.user_data["plan_month"]
    proj = ctx.user_data["plan_project"]
    metric = ctx.user_data["plan_cur_metric"]

    if val > 0:
        set_plan(y, m, proj, metric, val)

    ctx.user_data["plan_idx"] += 1
    keys = ctx.user_data["plan_skippable"]
    idx = ctx.user_data["plan_idx"]

    if idx >= len(keys):
        await update.message.reply_text(f"✅ Планы сохранены!")
        return ConversationHandler.END

    key = keys[idx]
    label = next(l for k, l in METRICS if k == key)
    cur = get_plan(y, m, proj, key)
    ctx.user_data["plan_cur_metric"] = key
    await update.message.reply_text(
        f"📝 <b>{label}</b>\nТекущий план: <code>{int(cur) if cur else '—'}</code>\n\nВведите значение:",
        parse_mode=ParseMode.HTML,
    )
    return PLAN_VALUE


# ─── /setup conversation ──────────────────────────────────────────────────────

SETUP_FIELDS = [
    ("meeting_date_field",  "Дата встречи проведена (фиксация)"),
    ("booking_date_field",  "Дата (Бронь) — ОП (ДФБ)"),
    ("project_field",       "Поле «Проект — встреча»"),
    ("source_field",        "Поле источника (ТМ/Агенты/Пешеходы)"),
    ("agent_stage_ids",     "ID стадий «Встреча от Агентов» (через запятую)"),
    ("ped_stage_ids",       "ID стадий «Пешеходы» (через запятую)"),
    ("cs_pipeline_id",      "ID воронки «Клиентский сервис»"),
    ("sales_pipeline_id",   "ID воронки «Отдел продаж»"),
    ("paid_stage_id",       "ID стадии «Оплатившие»"),
]


async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    return await _show_setup_menu(update.message, ctx)


async def _show_setup_menu(msg_or_q, ctx, edit=False):
    rows = [
        [("📋 Поля Bitrix24", "setup:fields"), ("🔍 Найти поля авто", "setup:discover")],
        [("🗂 Воронки", "setup:pipelines"), ("📊 Стадии", "setup:stages")],
        [("⚙️ Текущие настройки", "setup:current"), ("❌ Закрыть", "setup:close")],
    ]
    text = "⚙️ <b>Настройка Bitrix24</b>\n\nВыберите раздел:"
    if edit:
        await safe_edit(msg_or_q, text, kb(rows), parse_mode=ParseMode.HTML)
    else:
        await msg_or_q.reply_text(text, reply_markup=kb(rows), parse_mode=ParseMode.HTML)
    return SETUP_MENU


async def setup_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]

    if action == "close":
        await safe_edit(q, "✅ Настройки закрыты.")
        return ConversationHandler.END

    elif action == "current":
        lines = ["⚙️ <b>Текущие настройки</b>\n"]
        for key, label in SETUP_FIELDS:
            val = db_get(key) or "—"
            lines.append(f"<b>{label}</b>:\n<code>{val}</code>\n")
        rows = [[("⬅️ Назад", "setup:back")]]
        await safe_edit(q, "\n".join(lines), kb(rows), parse_mode=ParseMode.HTML)
        return SETUP_MENU

    elif action == "back":
        return await _show_setup_menu(q, ctx, edit=True)

    elif action == "fields":
        rows = []
        for i, (key, label) in enumerate(SETUP_FIELDS):
            rows.append([(f"✏️ {label}", f"setfield:{i}")])
        rows.append([("⬅️ Назад", "setup:back")])
        await safe_edit(q, "📋 <b>Выберите поле для настройки:</b>", kb(rows), parse_mode=ParseMode.HTML)
        return SETUP_FIELD_PICK

    elif action == "discover":
        await safe_edit(q, "⏳ Загружаю поля из Bitrix24...")
        try:
            bx = Bitrix()
            fields = await bx.get_deal_fields()
            # Show date and list fields
            interesting = []
            for fname, fmeta in fields.items():
                if not fname.startswith("UF_"):
                    continue
                ftype = fmeta.get("type", "")
                flabel = fmeta.get("listLabel") or fmeta.get("title") or fname
                if ftype in ("date", "datetime", "enumeration", "crm_status"):
                    interesting.append((fname, flabel, ftype))
            if not interesting:
                await safe_edit(q, "Пользовательских полей не найдено.", kb([[("⬅️ Назад", "setup:back")]]))
                return SETUP_MENU
            lines = ["🔍 <b>Найденные пользовательские поля:</b>\n"]
            for fname, flabel, ftype in interesting[:40]:
                lines.append(f"<code>{fname}</code> — {flabel} [{ftype}]")
            lines.append("\nСкопируйте нужный ID поля и введите его через /setup → Поля")
            rows = [[("⬅️ Назад", "setup:back")]]
            await safe_edit(q, "\n".join(lines), kb(rows), parse_mode=ParseMode.HTML)
        except Exception as e:
            await safe_edit(q, f"❌ Ошибка: {e}", kb([[("⬅️ Назад", "setup:back")]]))
        return SETUP_MENU

    elif action == "pipelines":
        await safe_edit(q, "⏳ Загружаю воронки...")
        try:
            bx = Bitrix()
            pipelines = await bx.get_pipelines()
            lines = ["🗂 <b>Воронки (пайплайны):</b>\n"]
            for p in pipelines:
                lines.append(f"ID: <code>{p.get('ID')}</code> — {p.get('NAME', '')}")
            lines.append("\nИспользуйте /setup → Поля для ввода ID воронок")
            rows = [[("⬅️ Назад", "setup:back")]]
            await safe_edit(q, "\n".join(lines), kb(rows), parse_mode=ParseMode.HTML)
        except Exception as e:
            await safe_edit(q, f"❌ Ошибка: {e}", kb([[("⬅️ Назад", "setup:back")]]))
        return SETUP_MENU

    elif action == "stages":
        sales_pl = db_get("sales_pipeline_id") or "0"
        await safe_edit(q, f"⏳ Загружаю стадии воронки {sales_pl}...")
        try:
            bx = Bitrix()
            stages = await bx.get_pipeline_stages(sales_pl)
            lines = [f"📊 <b>Стадии воронки {sales_pl}:</b>\n"]
            for s in stages:
                sem = s.get("SEMANTICS", "")
                sem_label = {"": "В работе", "S": "Выиграна", "F": "Проиграна"}.get(sem, sem)
                lines.append(f"<code>{s.get('STATUS_ID')}</code> — {s.get('NAME')} [{sem_label}]")
            lines.append("\nСкопируйте ID стадий и введите через /setup → Поля")
            rows = [[("⬅️ Назад", "setup:back")]]
            await safe_edit(q, "\n".join(lines), kb(rows), parse_mode=ParseMode.HTML)
        except Exception as e:
            await safe_edit(q, f"❌ Ошибка: {e}", kb([[("⬅️ Назад", "setup:back")]]))
        return SETUP_MENU

    return SETUP_MENU


async def setup_field_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "setup:back":
        return await _show_setup_menu(q, ctx, edit=True)
    idx = int(q.data.split(":")[1])
    ctx.user_data["setup_field_idx"] = idx
    key, label = SETUP_FIELDS[idx]
    cur = db_get(key) or "—"
    await safe_edit(
        q,
        f"✏️ <b>{label}</b>\nТекущее значение: <code>{cur}</code>\n\nВведите новое значение:",
        parse_mode=ParseMode.HTML,
    )
    return SETUP_FIELD_VALUE


async def setup_field_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    idx = ctx.user_data.get("setup_field_idx", 0)
    key, label = SETUP_FIELDS[idx]
    db_set(key, val)
    await update.message.reply_text(
        f"✅ <b>{label}</b> сохранено: <code>{val}</code>",
        parse_mode=ParseMode.HTML,
    )
    return await _show_setup_menu(update.message, ctx)


async def setup_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Настройка отменена.")
    return ConversationHandler.END

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

# ─── Application builder ──────────────────────────────────────────────────────

def main():
    init_db()
    init_mop_tables()
    from init_data import seed
    seed()
    app = Application.builder().token(TOKEN).build()

    # /report conversation
    rpt_conv = ConversationHandler(
        entry_points=[CommandHandler("report", cmd_report)],
        states={
            RPT_TYPE: [CallbackQueryHandler(rpt_type, pattern="^rpt:")],
            RPT_DATE_PRESET: [CallbackQueryHandler(rpt_date_preset, pattern="^(preset:|rpt:back)")],
            RPT_DATE_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, rpt_date_from)],
            RPT_DATE_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, rpt_date_to)],
            RPT_MANAGERS: [CallbackQueryHandler(rpt_managers, pattern="^mgr:")],
            RPT_PROJECT: [CallbackQueryHandler(rpt_project, pattern="^proj:")],
        },
        fallbacks=[CommandHandler("cancel", rpt_cancel)],
        allow_reentry=True,
    )

    # /setplan conversation
    plan_conv = ConversationHandler(
        entry_points=[CommandHandler("setplan", cmd_setplan)],
        states={
            PLAN_MONTH: [CallbackQueryHandler(plan_month, pattern="^planm:")],
            PLAN_PROJECT: [CallbackQueryHandler(plan_project, pattern="^planp:")],
            PLAN_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_value)],
        },
        fallbacks=[CommandHandler("cancel", rpt_cancel)],
        allow_reentry=True,
    )

    # /setup conversation
    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", cmd_setup)],
        states={
            SETUP_MENU: [CallbackQueryHandler(setup_menu, pattern="^setup:")],
            SETUP_FIELD_PICK: [
                CallbackQueryHandler(setup_field_pick, pattern="^setfield:"),
                CallbackQueryHandler(setup_menu, pattern="^setup:"),
            ],
            SETUP_FIELD_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_field_value)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
        allow_reentry=True,
    )

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
    app.add_handler(CommandHandler("managers", cmd_managers))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CallbackQueryHandler(task_drilldown_callback, pattern="^tsk:"))
    app.add_handler(rpt_conv)
    app.add_handler(plan_conv)
    app.add_handler(setup_conv)
    app.add_handler(mop_conv)

    # Daily task report at 10:30 Almaty (UTC+5) = 05:30 UTC
    almaty = pytz.timezone("Asia/Almaty")
    app.job_queue.run_daily(
        daily_task_report,
        time=dtime(10, 30, tzinfo=almaty),
        name="daily_task_report",
    )
    log.info("Bot started, daily task report scheduled at 10:30 Almaty")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
