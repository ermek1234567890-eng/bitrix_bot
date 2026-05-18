from bitrix import Bitrix
from db import db_get, get_managers, get_plan

METRICS = [
    ("meetings", "Встречи"),
    ("closed",   "Закрытые встречи"),
    ("agents",   "Агенты"),
    ("peds",     "Пешеходы/Рек."),
    ("tm",       "ТМ"),
    ("dfb",      "Кол-во ДФБ"),
    ("paid",     "Оплатившие"),
    ("cr3",      "CR3"),
]


def _empty_row() -> dict:
    return {k: 0 for k, _ in METRICS}


def _cr3(dfb: int, meetings: int) -> str:
    if meetings == 0:
        return "0%"
    return f"{round(dfb / meetings * 100)}%"


async def compute_metrics(
    date_from: str,
    date_to: str,
    manager_ids: list,           # list of int bitrix IDs; [] = all
    project_enum_ids: list = None,  # list of project enum IDs; None = all
) -> tuple:
    """
    Returns (data_dict, error_str).
    data_dict = {
        manager_bitrix_id: {metric_key: value, ...},
        "total": {...}
    }
    """
    meeting_date_field = db_get("meeting_date_field")
    if not meeting_date_field:
        return None, "❌ Не настроено поле <b>Дата встречи проведена</b>.\nИспользуйте /setup → Поля"

    booking_date_field = db_get("booking_date_field")
    project_field = db_get("project_field")
    source_field = db_get("source_field")

    agent_stage_ids = set(
        s.strip() for s in (db_get("agent_stage_ids") or "").split(",") if s.strip()
    )
    ped_stage_ids = set(
        s.strip() for s in (db_get("ped_stage_ids") or "").split(",") if s.strip()
    )

    bx = Bitrix()

    # Resolve manager_ids to pass to API (None = no filter)
    api_mgr_ids = manager_ids if manager_ids else None

    # Fetch meeting deals (filtered by project if selected)
    meeting_deals = await bx.fetch_meeting_deals(
        date_from, date_to, api_mgr_ids, project_field, project_enum_ids
    )

    # Fetch DFB deals — by booking date + project, independent of meeting date
    dfb_deals = await bx.fetch_dfb_deals(
        date_from, date_to, api_mgr_ids, project_field, project_enum_ids
    )

    # Fetch paid deals
    paid_deals = await bx.fetch_paid_deals(date_from, date_to, api_mgr_ids)

    # Determine which manager IDs to track
    if manager_ids:
        tracked = {mid: _empty_row() for mid in manager_ids}
    else:
        tracked = {}

    def get_row(mid: int) -> dict:
        if mid not in tracked:
            tracked[mid] = _empty_row()
        return tracked[mid]

    # Process meeting deals
    for deal in meeting_deals:
        mid = int(deal.get("ASSIGNED_BY_ID", 0))
        if manager_ids and mid not in tracked:
            continue
        row = get_row(mid)
        row["meetings"] += 1

        stage_id = str(deal.get("STAGE_ID", ""))
        semantic = deal.get("STAGE_SEMANTIC_ID", "")

        # Закрытые встречи = стадии группы "провалена"
        if semantic == "F":
            row["closed"] += 1

        # Classify source via "Аналитика встречи - фиксация" field (enumeration IDs)
        is_agent = False
        is_ped = False
        if source_field:
            src_val = str(deal.get(source_field, ""))
            # src_val is enum item ID or list of IDs
            src_ids = set(src_val.replace(";", ",").split(",")) if src_val else set()
            agent_ids = set(
                s.strip() for s in (db_get("source_agent_ids") or "").split(",") if s.strip()
            )
            ped_ids = set(
                s.strip() for s in (db_get("source_ped_ids") or "").split(",") if s.strip()
            )
            is_agent = bool(src_ids & agent_ids)
            is_ped = bool(src_ids & ped_ids)

        if is_agent:
            row["agents"] += 1
        elif is_ped:
            row["peds"] += 1
        else:
            row["tm"] += 1

    # Process DFB deals — use "Менеджер ОП" field if available
    op_mgr_field = db_get("op_manager_field")
    for deal in dfb_deals:
        if op_mgr_field and deal.get(op_mgr_field):
            mid = int(deal.get(op_mgr_field, 0))
        else:
            mid = int(deal.get("ASSIGNED_BY_ID", 0))
        if manager_ids and mid not in tracked:
            continue
        get_row(mid)["dfb"] += 1

    # Process paid deals
    for deal in paid_deals:
        mid = int(deal.get("ASSIGNED_BY_ID", 0))
        if manager_ids and mid not in tracked:
            continue
        get_row(mid)["paid"] += 1

    # Compute CR3 per manager
    for row in tracked.values():
        row["cr3_num"] = row["dfb"]
        row["cr3_den"] = row["meetings"]

    # Compute totals
    total = _empty_row()
    for row in tracked.values():
        for key, _ in METRICS:
            if key != "cr3":
                total[key] += row[key]
    total["cr3_num"] = total["dfb"]
    total["cr3_den"] = total["meetings"]

    tracked["total"] = total
    return tracked, None


# ---- formatting ----

def _col(text: str, width: int) -> str:
    t = str(text)
    if len(t) > width:
        t = t[:width]
    return t.ljust(width)


def format_regular_report(
    data: dict,
    manager_map: dict,  # {bitrix_id: short_name}
    date_from: str,
    date_to: str,
    project: str = "Все проекты",
) -> str:
    mgr_ids = [mid for mid in data if mid != "total"]
    mgr_ids_sorted = sorted(mgr_ids)

    # Column widths
    metric_w = 20
    mgr_w = 7
    total_w = 7

    header_parts = [_col("Метрика", metric_w)]
    for mid in mgr_ids_sorted:
        name = manager_map.get(mid, str(mid))
        header_parts.append(_col(name, mgr_w))
    header_parts.append(_col("Итого", total_w))

    sep = "─" * (metric_w + (mgr_w + 1) * len(mgr_ids_sorted) + total_w + 1)

    lines = [
        f"📊 <b>Регулярный менеджмент</b>",
        f"🗂 Проект: <b>{project}</b>",
        f"📅 {date_from} — {date_to}",
        "",
        f"<pre>{' '.join(header_parts)}",
        sep,
    ]

    total = data["total"]
    for key, label in METRICS:
        row_parts = [_col(label, metric_w)]
        for mid in mgr_ids_sorted:
            row = data[mid]
            if key == "cr3":
                val = _cr3(row.get("cr3_num", row.get("dfb", 0)), row.get("cr3_den", row.get("meetings", 0)))
            else:
                val = str(row.get(key, 0))
            row_parts.append(_col(val, mgr_w))
        # Total column
        if key == "cr3":
            tot_val = _cr3(total.get("cr3_num", total.get("dfb", 0)), total.get("cr3_den", total.get("meetings", 0)))
        else:
            tot_val = str(total.get(key, 0))
        row_parts.append(_col(tot_val, total_w))
        lines.append(" ".join(row_parts))

    lines.append(sep)
    lines.append("</pre>")
    return "\n".join(lines)


def format_plan_fact_report(
    data: dict,
    date_from: str,
    date_to: str,
    year: int,
    month: int,
    project: str = "",
) -> str:
    total = data.get("total", {})

    plan_w = 6
    fact_w = 6
    pct_w = 8
    metric_w = 22

    header = f"{_col('Метрика', metric_w)}{_col('План', plan_w)}{_col('Факт', fact_w)}{_col('Выполн.', pct_w)}"
    sep = "─" * (metric_w + plan_w + fact_w + pct_w)

    lines = [
        f"📈 <b>План vs Факт</b>",
        f"🗂 Проект: <b>{project or 'Все'}</b>",
        f"📅 {date_from} — {date_to}",
        "",
        f"<pre>{header}",
        sep,
    ]

    for key, label in METRICS:
        plan_val = get_plan(year, month, project, key)
        if key == "cr3":
            fact_val_str = _cr3(
                total.get("cr3_num", total.get("dfb", 0)),
                total.get("cr3_den", total.get("meetings", 0))
            )
            plan_str = f"{int(plan_val)}%" if plan_val else "—"
            pct_str = ""
        else:
            fact_num = total.get(key, 0)
            fact_val_str = str(fact_num)
            plan_str = str(int(plan_val)) if plan_val else "—"
            if plan_val > 0:
                pct_str = f"{round(fact_num / plan_val * 100)}%"
            else:
                pct_str = "—"

        row = (
            f"{_col(label, metric_w)}"
            f"{_col(plan_str, plan_w)}"
            f"{_col(fact_val_str, fact_w)}"
            f"{_col(pct_str, pct_w)}"
        )
        lines.append(row)

    lines.append(sep)
    lines.append("</pre>")
    return "\n".join(lines)
