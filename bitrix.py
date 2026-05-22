import asyncio
import httpx
from db import db_get
import os

BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK", "")
MAX_PAGES   = 40    # не более 40 страниц × 50 записей = 2000 сделок
API_TIMEOUT = 20.0  # секунд на один запрос


class Bitrix:
    def __init__(self):
        wh = db_get("bitrix_webhook") or BITRIX_WEBHOOK
        self.base = wh.rstrip("/") + "/"

    async def call(self, method: str, params: dict = None) -> dict:
        url = f"{self.base}{method}.json"
        for attempt in range(4):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(API_TIMEOUT, connect=10.0)
            ) as client:
                r = await client.post(url, json=params or {})
                if r.status_code == 429:
                    wait = 1.0 * (attempt + 1)   # 1s, 2s, 3s
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
        raise RuntimeError(f"Bitrix24 rate limit: {method}")

    async def list_all(self, method: str, params: dict = None) -> list:
        results = []
        start = 0
        for _ in range(MAX_PAGES):
            p = dict(params or {})
            p["start"] = start
            try:
                data = await asyncio.wait_for(self.call(method, p), timeout=API_TIMEOUT)
            except asyncio.TimeoutError:
                break
            except Exception:
                break
            batch = data.get("result", [])
            if not batch:
                break
            results.extend(batch)
            nxt = data.get("next")
            if nxt is None:
                break
            start = nxt
            await asyncio.sleep(0.2)
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
        op_mgr_field = db_get("op_manager_field")
        if not field:
            return []
        flt = {
            f">={field}": date_from,
            f"<={field}": date_to,
        }
        # Don't filter by manager in API — we filter in Python
        # because some deals use op_manager_field, others use ASSIGNED_BY_ID
        if project_field and project_enum_ids:
            flt[project_field] = project_enum_ids

        extra = ["PREVIOUS_STAGE_ID"]
        if op_mgr_field:
            extra.append(op_mgr_field)
        if project_field:
            extra.append(project_field)
        for f in ["source_field", "traffic_source_field"]:
            fid = db_get(f)
            if fid and fid not in extra:
                extra.append(fid)
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

    # ---- helpers ----

    def get_portal_url(self) -> str:
        """Extract portal base URL from webhook, e.g. https://amanat.bitrix24.kz"""
        try:
            parts = self.base.rstrip("/").split("/")
            return f"{parts[0]}//{parts[2]}"
        except Exception:
            return ""

    async def list_all_tasks(self, params: dict) -> list:
        """Paginate tasks.task.list (returns {tasks:[...]} not a direct array)."""
        results = []
        start = 0
        for _ in range(MAX_PAGES):
            p = dict(params)
            p["start"] = start
            try:
                data = await asyncio.wait_for(self.call("tasks.task.list", p), timeout=API_TIMEOUT)
            except Exception:
                break
            result = data.get("result", {})
            if isinstance(result, dict):
                tasks = result.get("tasks", [])
            elif isinstance(result, list):
                tasks = result
            else:
                break
            if not tasks:
                break
            results.extend(tasks)
            nxt = data.get("next")
            if nxt is None:
                break
            start = nxt
            await asyncio.sleep(0.2)
        return results

    @staticmethod
    def _extract_deal_ids(tasks: list) -> list:
        """Extract unique deal IDs from tasks' ufCrmTask field."""
        seen = set()
        deal_ids = []
        for task in tasks:
            crm = task.get("ufCrmTask") or task.get("UF_CRM_TASK") or []
            if isinstance(crm, str):
                crm = [crm]
            for ref in crm:
                if isinstance(ref, str) and ref.upper().startswith("D_"):
                    try:
                        did = int(ref.split("_")[1])
                        if did not in seen:
                            seen.add(did)
                            deal_ids.append(did)
                    except (ValueError, IndexError):
                        pass
        return deal_ids

    # ---- tasks ----

    async def fetch_task_counts(self, user_id: int, today_str: str) -> dict:
        """
        Returns {"overdue": int, "today": int} for a given user.
        today_str format: "YYYY-MM-DD"
        Overdue  = not completed, deadline < today
        Today    = not completed, deadline = today
        """
        # Status 5 = completed in Bitrix24 tasks
        base_filter = {
            "RESPONSIBLE_ID": user_id,
            "!REAL_STATUS": 5,
        }

        # Overdue: deadline is set and less than today
        overdue_filter = dict(base_filter)
        overdue_filter["<DEADLINE"] = today_str

        # Today: deadline equals today
        today_filter = dict(base_filter)
        today_filter["<=DEADLINE"] = today_str + "T23:59:59"
        today_filter[">=DEADLINE"] = today_str + "T00:00:00"

        try:
            overdue_resp = await asyncio.wait_for(
                self.call("tasks.task.list", {
                    "filter": overdue_filter,
                    "select": ["ID"],
                    "params": {"NAV_PARAMS": {"nPageSize": 1, "bCount": "Y"}},
                }),
                timeout=API_TIMEOUT,
            )
            today_resp = await asyncio.wait_for(
                self.call("tasks.task.list", {
                    "filter": today_filter,
                    "select": ["ID"],
                    "params": {"NAV_PARAMS": {"nPageSize": 1, "bCount": "Y"}},
                }),
                timeout=API_TIMEOUT,
            )
        except Exception:
            return {"overdue": -1, "today": -1}

        def _count(resp):
            r = resp.get("result", {})
            # tasks.task.list returns {"tasks": [...], "total": N}
            if isinstance(r, dict):
                total = resp.get("total") or r.get("total")
                if total is not None:
                    return int(total)
                return len(r.get("tasks", []))
            return 0

        return {
            "overdue": _count(overdue_resp),
            "today": _count(today_resp),
        }

    async def fetch_deal_ids_from_tasks(self, user_id: int, filter_type: str, today_str: str) -> list:
        """
        Get unique deal IDs linked to tasks for a manager.
        filter_type: 'overdue' | 'today'
        """
        base_filter = {"RESPONSIBLE_ID": user_id, "!REAL_STATUS": 5}
        if filter_type == "overdue":
            base_filter["<DEADLINE"] = today_str
        else:
            base_filter[">=DEADLINE"] = today_str + "T00:00:00"
            base_filter["<=DEADLINE"] = today_str + "T23:59:59"

        tasks = await self.list_all_tasks({
            "filter": base_filter,
            "select": ["ID", "UF_CRM_TASK"],
        })
        return self._extract_deal_ids(tasks)

    async def fetch_deals_by_ids(self, deal_ids: list) -> list:
        """Fetch deal ID and TITLE for given deal IDs."""
        if not deal_ids:
            return []
        return await self.list_all("crm.deal.list", {
            "filter": {"=ID": deal_ids},
            "select": ["ID", "TITLE"],
        })

    async def fetch_deals_no_tasks(self, user_id: int) -> list:
        """
        Active deals in the Sales pipeline assigned to manager
        that have no open tasks where this manager is responsible.
        Fast: 2 API calls total.
        """
        sales_pipeline = db_get("sales_pipeline_id") or "0"

        # Fetch deals + tasks in parallel
        active_deals, tasks = await asyncio.gather(
            self.list_all("crm.deal.list", {
                "filter": {
                    "ASSIGNED_BY_ID": user_id,
                    "STAGE_SEMANTIC_ID": "P",
                    "CATEGORY_ID": sales_pipeline,
                },
                "select": ["ID", "TITLE"],
            }),
            self.list_all_tasks({
                "filter": {
                    "RESPONSIBLE_ID": user_id,
                    "!REAL_STATUS": 5,
                },
                "select": ["ID", "UF_CRM_TASK"],
            }),
        )
        covered = set(self._extract_deal_ids(tasks))
        return [d for d in active_deals if int(d.get("ID", 0)) not in covered]

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

    async def fetch_mop_upcoming_tasks(
        self, user_id: int, window_start: "datetime", window_end: "datetime"
    ) -> list:
        """Tasks created by МОП, linked to a deal, with deadline in window."""
        ws = window_start.strftime("%Y-%m-%dT%H:%M:%S")
        we = window_end.strftime("%Y-%m-%dT%H:%M:%S")

        tasks = await self.list_all_tasks({
            "filter": {
                "CREATED_BY": user_id,
                "!REAL_STATUS": 5,
                ">=DEADLINE": ws,
                "<=DEADLINE": we,
            },
            "select": ["ID", "TITLE", "DEADLINE", "UF_CRM_TASK"],
        })

        result = []
        for task in tasks:
            crm = task.get("ufCrmTask") or task.get("UF_CRM_TASK") or []
            if isinstance(crm, str):
                crm = [crm]
            has_deal = any(
                isinstance(r, str) and r.upper().startswith("D_")
                for r in crm
            )
            if has_deal:
                result.append(task)
        return result

    async def fetch_last_call(self, deal_id: int) -> "dict | None":
        """Last phone call activity for a deal (TYPE_ID=2)."""
        activities = await self.list_all("crm.activity.list", {
            "filter": {"OWNER_TYPE_ID": 2, "OWNER_ID": deal_id, "TYPE_ID": 2},
            "select": ["ID", "TYPE_ID", "START_TIME", "DESCRIPTION", "SUBJECT"],
            "order": {"START_TIME": "DESC"},
        })
        if not activities:
            return None
        return max(activities, key=lambda a: a.get("START_TIME", ""))

    async def fetch_last_visit(self, deal_id: int) -> "dict | None":
        """Last meeting/visit activity for a deal (TYPE_ID=1)."""
        activities = await self.list_all("crm.activity.list", {
            "filter": {"OWNER_TYPE_ID": 2, "OWNER_ID": deal_id, "TYPE_ID": 1},
            "select": ["ID", "TYPE_ID", "START_TIME", "DESCRIPTION", "SUBJECT"],
            "order": {"START_TIME": "DESC"},
        })
        if not activities:
            return None
        return max(activities, key=lambda a: a.get("START_TIME", ""))

    async def fetch_deal_detail(self, deal_id: int) -> dict:
        """Fetch deal details: title, stage, dates."""
        r = await self.call("crm.deal.get", {"id": deal_id})
        return r.get("result", {})
