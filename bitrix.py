import asyncio
import httpx
from db import db_get
import os

BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK", "")


class Bitrix:
    def __init__(self):
        wh = db_get("bitrix_webhook") or BITRIX_WEBHOOK
        self.base = wh.rstrip("/") + "/"

    async def call(self, method: str, params: dict = None) -> dict:
        url = f"{self.base}{method}.json"
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, json=params or {})
            r.raise_for_status()
            return r.json()

    async def list_all(self, method: str, params: dict = None) -> list:
        results = []
        start = 0
        while True:
            p = dict(params or {})
            p["start"] = start
            data = await self.call(method, p)
            batch = data.get("result", [])
            if not batch:
                break
            results.extend(batch)
            nxt = data.get("next")
            if nxt is None:
                break
            start = nxt
            await asyncio.sleep(0.25)
        return results

    # ---- discovery ----

    async def get_deal_fields(self) -> dict:
        r = await self.call("crm.deal.fields")
        return r.get("result", {})

    async def get_pipelines(self) -> list:
        r = await self.call("crm.category.list", {"entityTypeId": 2})
        res = r.get("result", {})
        if isinstance(res, dict):
            cats = res.get("categories", [])
        else:
            cats = res or []
        # Add default pipeline (id=0) manually
        default = {"ID": "0", "NAME": "Отдел продаж (основная)"}
        return [default] + [c for c in cats if str(c.get("ID")) != "0"]

    async def get_pipeline_stages(self, pipeline_id) -> list:
        r = await self.call("crm.dealcategory.stages.list", {"id": pipeline_id})
        return r.get("result", [])

    async def get_users(self, position: str = None, positions: list = None) -> list:
        all_users = await self.list_all("user.get", {
            "filter": {"ACTIVE": True, "USER_TYPE": "employee"}
        })
        if positions:
            all_users = [u for u in all_users if u.get("WORK_POSITION", "").strip() in positions]
        elif position:
            all_users = [u for u in all_users if u.get("WORK_POSITION", "").strip() == position]
        return all_users

    # ---- deals ----

    async def get_deals(self, flt: dict, select: list = None) -> list:
        base_select = ["ID", "ASSIGNED_BY_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID"]
        if select:
            for f in select:
                if f not in base_select:
                    base_select.append(f)
        return await self.list_all("crm.deal.list", {"filter": flt, "select": base_select})

    # ---- report data ----

    async def fetch_meeting_deals(
        self, date_from: str, date_to: str,
        manager_ids: list = None, project_field: str = None, project_enum_ids: list = None
    ) -> list:
        field = db_get("meeting_date_field")
        if not field:
            return []
        flt = {
            f">={field}": date_from,
            f"<={field}": date_to,
        }
        if manager_ids:
            flt["ASSIGNED_BY_ID"] = manager_ids
        if project_field and project_enum_ids:
            flt[project_field] = project_enum_ids  # Bitrix24 accepts list for IN filter

        extra = []
        if project_field:
            extra.append(project_field)
        source_field = db_get("source_field")
        if source_field:
            extra.append(source_field)
        return await self.get_deals(flt, extra)

    async def fetch_dfb_deals(
        self, date_from: str, date_to: str,
        manager_ids: list = None, project_field: str = None, project_enum_ids: list = None
    ) -> list:
        field = db_get("booking_date_field")
        cs_pipeline = db_get("cs_pipeline_id")
        op_mgr_field = db_get("op_manager_field")  # "Менеджер ОП" field in CS pipeline
        if not field:
            return []
        flt = {
            f">={field}": date_from,
            f"<={field}": date_to,
        }
        if cs_pipeline:
            flt["CATEGORY_ID"] = cs_pipeline
        # Use "Менеджер ОП" field to filter by original sales manager
        if manager_ids and op_mgr_field:
            flt[op_mgr_field] = manager_ids
        elif manager_ids:
            flt["ASSIGNED_BY_ID"] = manager_ids
        if project_field and project_enum_ids:
            flt[project_field] = project_enum_ids
        return await self.get_deals(flt, [op_mgr_field] if op_mgr_field else [])

    async def fetch_paid_deals(
        self, date_from: str, date_to: str,
        manager_ids: list = None
    ) -> list:
        paid_stage = db_get("paid_stage_id")
        paid_date_field = db_get("paid_date_field") or db_get("booking_date_field")
        if not paid_stage or not paid_date_field:
            return []
        flt = {
            "STAGE_ID": paid_stage,
            f">={paid_date_field}": f"{date_from}T00:00:00",
            f"<={paid_date_field}": f"{date_to}T23:59:59",
        }
        if manager_ids:
            flt["ASSIGNED_BY_ID"] = manager_ids
        return await self.get_deals(flt, [])
