import json
import logging
import os
import threading
import time
from datetime import date, timedelta

from bitrix import Bitrix, BitrixError

log = logging.getLogger("store")

TASK_STATUS_DONE = 5
ACTIVITY_TYPE_CALL = 2

# What we keep locally. `since` limits history for entities that pile up (leads, calls, tasks);
# deals are kept in full because open deals of any age belong on the funnel.
ENTITIES = {
    "deals": {
        "label": "сделки", "method": "crm.deal.list", "id": "ID", "modified": "DATE_MODIFY",
        "select": ["ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "STAGE_SEMANTIC_ID", "OPPORTUNITY", "CURRENCY_ID",
                   "ASSIGNED_BY_ID", "DATE_CREATE", "CLOSEDATE", "CLOSED", "DATE_MODIFY"],
        "filter": lambda since: {},
    },
    "leads": {
        "label": "лиды", "method": "crm.lead.list", "id": "ID", "modified": "DATE_MODIFY",
        "select": ["ID", "TITLE", "STATUS_ID", "STATUS_SEMANTIC_ID", "SOURCE_ID", "ASSIGNED_BY_ID",
                   "DATE_CREATE", "OPPORTUNITY", "DATE_MODIFY"],
        "filter": lambda since: {">=DATE_CREATE": since},
    },
    "calls": {
        "label": "звонки", "method": "crm.activity.list", "id": "ID", "modified": "LAST_UPDATED",
        "select": ["ID", "RESPONSIBLE_ID", "DIRECTION", "COMPLETED", "CREATED", "LAST_UPDATED"],
        "filter": lambda since: {"TYPE_ID": ACTIVITY_TYPE_CALL, ">=CREATED": since},
    },
    "tasks": {
        "label": "задачи", "method": "tasks.task.list", "id": "id", "modified": "CHANGED_DATE",
        "row_modified": "changedDate", "result_key": "tasks",
        "select": ["ID", "TITLE", "STATUS", "DEADLINE", "RESPONSIBLE_ID", "CLOSED_DATE", "CREATED_DATE",
                   "CHANGED_DATE"],
        "filter": lambda since: {">=CHANGED_DATE": since},
        # Old tasks that are still open matter too, whenever they were last touched.
        "extra_filter": {"!STATUS": TASK_STATUS_DONE},
    },
}


class Store:
    """Local copy of the Bitrix data, saved to a JSON file and kept fresh with incremental syncs."""

    def __init__(self, path, webhook_url, history_days=400, full_sync_every=24 * 3600):
        self.path = path
        self.webhook_url = webhook_url
        self.history_days = history_days
        self.full_sync_every = full_sync_every
        self.lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.progress = ""
        self.last_error = None
        self.data = self._empty()
        self._load()

    @staticmethod
    def _empty():
        return {"ref": {}, "rows": {name: {} for name in ENTITIES}, "cursors": {}, "tasks_error": None,
                "full_sync_at": 0, "synced_at": 0}

    @property
    def ready(self):
        return self.data["synced_at"] > 0

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            for name in ENTITIES:
                loaded.setdefault("rows", {}).setdefault(name, {})
            self.data = {**self._empty(), **loaded}
            log.info("Локальные данные загружены из %s", self.path)
        except (OSError, ValueError):
            log.exception("Не удалось прочитать %s, скачаю всё заново", self.path)

    def _save(self):
        tmp = self.path + ".tmp"
        with self.lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def snapshot(self):
        with self.lock:
            return {
                "ref": self.data["ref"],
                "rows": {name: list(rows.values()) for name, rows in self.data["rows"].items()},
                "tasks_error": self.data["tasks_error"],
                "synced_at": self.data["synced_at"],
            }

    # ---------- syncing ----------

    def sync(self, force_full=False, wait=True):
        """Pull changes from Bitrix. Returns False if another sync is running and wait=False."""
        if not self.sync_lock.acquire(blocking=wait):
            return False
        try:
            full = force_full or time.time() - self.data["full_sync_at"] > self.full_sync_every
            started = time.time()
            self._sync(full)
            self.last_error = None
            log.info("%s синхронизация за %.1f с", "Полная" if full else "Быстрая", time.time() - started)
            return True
        except Exception as e:
            self.last_error = str(e)
            raise
        finally:
            self.progress = ""
            self.sync_lock.release()

    def _sync(self, full):
        bx = Bitrix(self.webhook_url)
        since = f"{(date.today() - timedelta(days=self.history_days)).isoformat()}T00:00:00"

        self.progress = "справочники"
        ref = load_reference(bx)
        with self.lock:
            self.data["ref"] = ref

        tasks_error = self.data["tasks_error"]
        for name, spec in ENTITIES.items():
            try:
                rows = self._fetch(bx, name, spec, since, None if full else self.data["cursors"].get(name))
            except BitrixError as e:
                if name != "tasks":
                    raise
                log.warning("Задачи недоступны: %s", e)
                tasks_error = ("У вебхука нет прав на задачи — добавьте право «Задачи (task)»"
                               if "insufficient_scope" in str(e) else f"Задачи не загрузились: {e}")
                continue
            if name == "tasks":
                tasks_error = None
            modified_key = spec.get("row_modified", spec["modified"])
            with self.lock:
                if full:
                    self.data["rows"][name] = {}
                target = self.data["rows"][name]
                for r in rows:
                    target[str(r[spec["id"]])] = r
                stamps = [r.get(modified_key) for r in target.values() if r.get(modified_key)]
                if stamps:
                    self.data["cursors"][name] = max(stamps)

        with self.lock:
            self.data["tasks_error"] = tasks_error
            now = time.time()
            self.data["synced_at"] = now
            if full:
                self.data["full_sync_at"] = now
        self._save()

    def _fetch(self, bx, name, spec, since, cursor):
        def progress(done, total):
            self.progress = f"{spec['label']}: {done} из {total}"

        flt = spec["filter"](since)
        if cursor:
            flt = {**flt, f">={spec['modified']}": cursor}
        filters = [flt]
        if not cursor and spec.get("extra_filter"):
            filters.append(spec["extra_filter"])

        rows = []
        for f in filters:
            self.progress = spec["label"]
            if spec.get("result_key"):
                rows += bx.list_paged(spec["method"], {"order": {"ID": "ASC"}, "filter": f, "select": spec["select"]},
                                      result_key=spec["result_key"], progress=progress)
            else:
                rows += bx.list_all(spec["method"], f, spec["select"], progress=progress)
        return rows


def load_reference(bx):
    statuses = bx.call("crm.status.list", {"order": {"SORT": "ASC"}}).get("result") or []
    ref = {"stage_names": {}, "stage_sort": {}, "lead_status": {}, "sources": {}, "pipelines": {"0": "Общая"},
           "users": {}}
    for s in statuses:
        entity = s.get("ENTITY_ID", "")
        if entity.startswith("DEAL_STAGE"):
            ref["stage_names"][s["STATUS_ID"]] = s["NAME"]
            ref["stage_sort"][s["STATUS_ID"]] = int(s.get("SORT") or 0)
        elif entity == "STATUS":
            ref["lead_status"][s["STATUS_ID"]] = s["NAME"]
        elif entity == "SOURCE":
            ref["sources"][s["STATUS_ID"]] = s["NAME"]

    try:
        cats = bx.call("crm.category.list", {"entityTypeId": 2}).get("result", {}).get("categories", [])
        for c in cats:
            ref["pipelines"][str(c["id"])] = c["name"]
    except BitrixError as e:
        log.warning("Не удалось получить воронки: %s", e)

    for u in bx.list_paged("user.get", {"FILTER": {}}):
        name = " ".join(p for p in (u.get("NAME"), u.get("LAST_NAME")) if p).strip()
        ref["users"][str(u["ID"])] = name or u.get("EMAIL") or f"#{u['ID']}"
    return ref
