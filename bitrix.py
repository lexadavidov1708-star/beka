import logging
import time

import requests

log = logging.getLogger("bitrix")

PAGE_SIZE = 50


class BitrixError(Exception):
    pass


class Bitrix:
    """Minimal Bitrix24 REST client working through an incoming webhook URL."""

    def __init__(self, webhook_url, timeout=30):
        self.base = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, method, params=None):
        for attempt in range(5):
            resp = self.session.post(self.base + method + ".json", json=params or {}, timeout=self.timeout)
            try:
                data = resp.json()
            except ValueError:
                raise BitrixError(f"{method}: HTTP {resp.status_code}, не JSON-ответ")
            if data.get("error") == "QUERY_LIMIT_EXCEEDED":
                time.sleep(1 + attempt)
                continue
            if "error" in data:
                raise BitrixError(f"{method}: {data['error']} — {data.get('error_description', '')}")
            return data
        raise BitrixError(f"{method}: превышен лимит запросов")

    def list_all(self, method, filter=None, select=None):
        """Fetch every row of a crm.*.list method using the fast ID-keyset pagination."""
        rows = []
        last_id = 0
        while True:
            flt = dict(filter or {})
            flt[">ID"] = last_id
            data = self.call(method, {
                "order": {"ID": "ASC"},
                "filter": flt,
                "select": select or ["*"],
                "start": -1,
            })
            page = data.get("result") or []
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            last_id = int(page[-1]["ID"])

    def list_paged(self, method, params=None, result_key=None):
        """Fetch every row of a method with classic `start`/`next` pagination."""
        rows = []
        start = 0
        while True:
            data = self.call(method, {**(params or {}), "start": start})
            page = data.get("result") or []
            if result_key:
                page = page.get(result_key, []) if isinstance(page, dict) else []
            rows.extend(page)
            if "next" not in data:
                return rows
            start = data["next"]
