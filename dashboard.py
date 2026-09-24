import hmac
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

from store import Store

load_dotenv()
BITRIX_WEBHOOK_URL = os.environ.get("BITRIX_WEBHOOK_URL", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "120"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "400"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("beka_dashboard")

app = Flask(__name__)

store = Store(os.path.join(DATA_DIR, "bitrix_data.json"), BITRIX_WEBHOOK_URL, HISTORY_DAYS)

TASK_STATUS_DONE = 5
TASK_STATUS_DECLINED = 7


# ---------- auth ----------

@app.before_request
def require_password():
    if not DASHBOARD_PASSWORD:
        return Response("Задайте DASHBOARD_PASSWORD в переменных окружения.", 503)
    auth = request.authorization
    if auth and auth.password and hmac.compare_digest(auth.password.encode("utf-8"),
                                                      DASHBOARD_PASSWORD.encode("utf-8")):
        return None
    return Response("Нужен пароль", 401, {"WWW-Authenticate": 'Basic realm="Beka Dashboard"'})


# ---------- helpers ----------

def day_of(value):
    return value[:10] if value else None


def money(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def days_between(date_from, date_to):
    days = []
    d = date_from
    while d <= date_to:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def in_period(value, d_from, d_to):
    day = day_of(value)
    return day is not None and d_from <= day <= d_to


# ---------- summary (built from the local copy, no Bitrix calls) ----------

def build_summary(date_from, date_to):
    snap = store.snapshot()
    ref, rows = snap["ref"], snap["rows"]
    stage_names, stage_sort = ref.get("stage_names", {}), ref.get("stage_sort", {})
    lead_status, sources = ref.get("lead_status", {}), ref.get("sources", {})
    pipelines, users = ref.get("pipelines", {}), ref.get("users", {})
    d_from, d_to = date_from.isoformat(), date_to.isoformat()

    deals = rows["deals"]
    deals_new = [d for d in deals if in_period(d.get("DATE_CREATE"), d_from, d_to)]
    deals_closed = [d for d in deals if d.get("CLOSED") == "Y" and in_period(d.get("CLOSEDATE"), d_from, d_to)]
    deals_open = [d for d in deals if d.get("CLOSED") != "Y"]
    leads = [l for l in rows["leads"] if in_period(l.get("DATE_CREATE"), d_from, d_to)]
    calls = [c for c in rows["calls"] if in_period(c.get("CREATED"), d_from, d_to)]
    tasks_open = [t for t in rows["tasks"]
                  if int(t.get("status") or 0) not in (TASK_STATUS_DONE, TASK_STATUS_DECLINED)]
    tasks_done = [t for t in rows["tasks"]
                  if int(t.get("status") or 0) == TASK_STATUS_DONE and in_period(t.get("closedDate"), d_from, d_to)]
    tasks_error = snap["tasks_error"]

    days = days_between(date_from, date_to)
    now = datetime.now(timezone.utc)
    user_name = lambda uid: users.get(str(uid), f"#{uid}")

    # --- deals ---
    won = [d for d in deals_closed if d.get("STAGE_SEMANTIC_ID") == "S"]
    lost = [d for d in deals_closed if d.get("STAGE_SEMANTIC_ID") == "F"]
    won_sum = sum(money(d["OPPORTUNITY"]) for d in won)
    currency = next((d.get("CURRENCY_ID") for d in deals_new + deals_closed + deals_open if d.get("CURRENCY_ID")), "")

    revenue_by_day = defaultdict(float)
    for d in won:
        revenue_by_day[day_of(d.get("CLOSEDATE"))] += money(d["OPPORTUNITY"])
    created_by_day = defaultdict(int)
    for d in deals_new:
        created_by_day[day_of(d.get("DATE_CREATE"))] += 1

    funnel = defaultdict(lambda: {"count": 0, "sum": 0.0})
    for d in deals_open:
        f = funnel[(str(d.get("CATEGORY_ID") or "0"), d["STAGE_ID"])]
        f["count"] += 1
        f["sum"] += money(d["OPPORTUNITY"])
    funnels = defaultdict(list)
    for (cat, stage), v in sorted(funnel.items(), key=lambda kv: (kv[0][0], stage_sort.get(kv[0][1], 0))):
        funnels[cat].append({"stage": stage_names.get(stage, stage), **v})

    top_won = sorted(won, key=lambda d: money(d["OPPORTUNITY"]), reverse=True)[:10]

    # --- leads ---
    lead_by_status = defaultdict(int)
    lead_by_source = defaultdict(int)
    lead_by_day = defaultdict(int)
    for l in leads:
        lead_by_status[lead_status.get(l.get("STATUS_ID"), l.get("STATUS_ID") or "—")] += 1
        lead_by_source[sources.get(l.get("SOURCE_ID"), l.get("SOURCE_ID") or "Не указан")] += 1
        lead_by_day[day_of(l.get("DATE_CREATE"))] += 1
    leads_converted = sum(1 for l in leads if l.get("STATUS_SEMANTIC_ID") == "S")
    leads_junk = sum(1 for l in leads if l.get("STATUS_SEMANTIC_ID") == "F")

    # --- tasks ---
    def is_overdue(t):
        dl = t.get("deadline")
        if not dl:
            return False
        try:
            return datetime.fromisoformat(dl) < now
        except ValueError:
            return False

    overdue = [t for t in tasks_open if is_overdue(t)]

    # --- managers ---
    mgr = defaultdict(lambda: {"deals_new": 0, "won": 0, "won_sum": 0.0, "lost": 0, "leads": 0,
                               "calls": 0, "calls_out": 0, "tasks_open": 0, "tasks_overdue": 0,
                               "tasks_done": 0, "pipeline_sum": 0.0})
    for d in deals_new:
        mgr[str(d["ASSIGNED_BY_ID"])]["deals_new"] += 1
    for d in won:
        m = mgr[str(d["ASSIGNED_BY_ID"])]
        m["won"] += 1
        m["won_sum"] += money(d["OPPORTUNITY"])
    for d in lost:
        mgr[str(d["ASSIGNED_BY_ID"])]["lost"] += 1
    for d in deals_open:
        mgr[str(d["ASSIGNED_BY_ID"])]["pipeline_sum"] += money(d["OPPORTUNITY"])
    for l in leads:
        mgr[str(l["ASSIGNED_BY_ID"])]["leads"] += 1
    for c in calls:
        m = mgr[str(c["RESPONSIBLE_ID"])]
        m["calls"] += 1
        if str(c.get("DIRECTION")) == "2":
            m["calls_out"] += 1
    for t in tasks_open:
        m = mgr[str(t.get("responsibleId"))]
        m["tasks_open"] += 1
        if is_overdue(t):
            m["tasks_overdue"] += 1
    for t in tasks_done:
        mgr[str(t.get("responsibleId"))]["tasks_done"] += 1

    managers = []
    for uid, m in mgr.items():
        closed = m["won"] + m["lost"]
        managers.append({"id": uid, "name": user_name(uid), **m,
                         "conversion": round(100 * m["won"] / closed, 1) if closed else None})
    managers.sort(key=lambda m: (m["won_sum"], m["deals_new"]), reverse=True)

    closed_total = len(won) + len(lost)
    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "generated_at": datetime.fromtimestamp(snap["synced_at"]).strftime("%d.%m.%Y %H:%M"),
        "currency": currency,
        "portal": BITRIX_WEBHOOK_URL.split("/rest/")[0],
        "kpi": {
            "deals_new": len(deals_new),
            "won": len(won),
            "won_sum": won_sum,
            "lost": len(lost),
            "conversion": round(100 * len(won) / closed_total, 1) if closed_total else None,
            "avg_check": won_sum / len(won) if won else 0,
            "pipeline_count": len(deals_open),
            "pipeline_sum": sum(money(d["OPPORTUNITY"]) for d in deals_open),
            "leads": len(leads),
            "leads_converted": leads_converted,
            "leads_junk": leads_junk,
            "calls": len(calls),
            "tasks_open": len(tasks_open),
            "tasks_overdue": len(overdue),
            "tasks_done": len(tasks_done),
        },
        "timeline": {
            "days": days,
            "revenue": [revenue_by_day.get(d, 0) for d in days],
            "deals_created": [created_by_day.get(d, 0) for d in days],
            "leads": [lead_by_day.get(d, 0) for d in days],
        },
        "funnels": [{"name": pipelines.get(cat, f"Воронка {cat}"), "stages": stages}
                    for cat, stages in funnels.items()],
        "leads_by_status": sorted(lead_by_status.items(), key=lambda kv: -kv[1]),
        "leads_by_source": sorted(lead_by_source.items(), key=lambda kv: -kv[1]),
        "managers": managers,
        "top_won": [{"id": d["ID"], "title": d.get("TITLE") or f"Сделка #{d['ID']}",
                     "sum": money(d["OPPORTUNITY"]), "manager": user_name(d["ASSIGNED_BY_ID"]),
                     "date": day_of(d.get("CLOSEDATE"))} for d in top_won],
        "tasks_error": tasks_error,
        "overdue_tasks": [{"id": t["id"], "title": t.get("title"), "deadline": day_of(t.get("deadline")),
                           "responsible": user_name(t.get("responsibleId"))}
                          for t in sorted(overdue, key=lambda t: t.get("deadline") or "")[:30]],
    }


def sync_loop():
    """Keep the local copy fresh: a full download once a day, only changes in between."""
    while True:
        try:
            store.sync()
        except Exception:
            log.exception("Синхронизация с Битриксом не удалась")
        time.sleep(SYNC_INTERVAL)


if BITRIX_WEBHOOK_URL:
    threading.Thread(target=sync_loop, daemon=True).start()


# ---------- routes ----------

def parse_period():
    today = date.today()
    try:
        date_from = date.fromisoformat(request.args.get("from", ""))
    except ValueError:
        date_from = today.replace(day=1)
    try:
        date_to = date.fromisoformat(request.args.get("to", ""))
    except ValueError:
        date_to = today
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/summary")
def api_summary():
    if not BITRIX_WEBHOOK_URL:
        return jsonify({"error": "Не задан BITRIX_WEBHOOK_URL"}), 500
    date_from, date_to = parse_period()
    if request.args.get("refresh") == "1" and store.ready:
        try:
            store.sync(wait=False)
        except Exception as e:
            log.exception("Ошибка Битрикса")
            return jsonify({"error": str(e)}), 502
    if not store.ready:
        if store.last_error:
            return jsonify({"error": store.last_error}), 502
        return jsonify({"loading": True, "progress": store.progress})
    return jsonify(build_summary(date_from, date_to))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
