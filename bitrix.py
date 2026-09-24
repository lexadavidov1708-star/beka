import logging
import time
from urllib.parse import quote

import requests

log = logging.getLogger("bitrix")

PAGE_SIZE = 50
BATCH_SIZE = 50


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

    def batch(self, commands):
        """Run up to 50 calls in one request. `commands` maps key -> (method, params)."""
        cmd = {key: f"{method}?{encode_query(params)}" for key, (method, params) in commands.items()}
        data = self.call("batch", {"halt": 1, "cmd": cmd}).get("result", {})
        errors = data.get("result_error") or {}
        if errors:
            key, err = next(iter(errors.items()))
            raise BitrixError(f"{commands[key][0]}: {err.get('error')} — {err.get('error_description', '')}")
        return data.get("result") or {}

    def list_all(self, method, filter=None, select=None, progress=None):
        """Fetch every row of a crm.*.list method."""
        return self.list_paged(method, {"order": {"ID": "ASC"}, "filter": filter or {}, "select": select or ["*"]},
                               progress=progress)

    def list_paged(self, method, params=None, result_key=None, progress=None):
        """Fetch every page: the first call learns `total`, the rest go in batches of 50 pages."""
        params = params or {}

        def rows_of(result):
            if result_key:
                return result.get(result_key, []) if isinstance(result, dict) else []
            return result or []

        first = self.call(method, {**params, "start": 0})
        rows = rows_of(first.get("result"))
        total = int(first.get("total") or 0)
        offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
        if progress:
            progress(len(rows), total)
        for i in range(0, len(offsets), BATCH_SIZE):
            chunk = offsets[i:i + BATCH_SIZE]
            results = self.batch({f"p{o}": (method, {**params, "start": o}) for o in chunk})
            for o in chunk:
                rows.extend(rows_of(results.get(f"p{o}")))
            if progress:
                progress(len(rows), total)
        return rows


def encode_query(params, prefix=None):
    """PHP-style http_build_query, which the batch method expects inside each command."""
    parts = []
    items = params.items() if isinstance(params, dict) else enumerate(params)
    for key, value in items:
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, (dict, list, tuple)):
            parts.append(encode_query(value, name))
        else:
            parts.append(f"{quote(name, safe='')}={quote(str(value), safe='')}")
    return "&".join(p for p in parts if p)
