const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nf = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 0});
let data = null, charts = {}, mgrSort = {key: "won_sum", dir: -1};

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const PALETTE = ["#2f6fed", "#1f9d55", "#d98a00", "#d64545", "#8e59d9", "#1aa3b8", "#c2569b", "#6b7489", "#7fae2b", "#e0703c"];

function iso(d) { return d.toISOString().slice(0, 10); }
function setPreset(p) {
  const t = new Date(); t.setMinutes(t.getMinutes() - t.getTimezoneOffset());
  let f = new Date(t), to = new Date(t);
  if (p === "week") f.setDate(t.getDate() - 6);
  if (p === "month") f.setDate(1);
  if (p === "prev") { f = new Date(t.getFullYear(), t.getMonth() - 1, 1, 12); to = new Date(t.getFullYear(), t.getMonth(), 0, 12); }
  if (p === "quarter") f = new Date(t.getFullYear(), Math.floor(t.getMonth() / 3) * 3, 1, 12);
  $("#from").value = iso(f); $("#to").value = iso(to);
  document.querySelectorAll("[data-preset]").forEach(b => b.classList.toggle("active", b.dataset.preset === p));
}


// ---------- Bitrix24 REST through the BX24 SDK (runs as the user who opened the app) ----------

const PAGE = 50, BATCH = 50;
const HISTORY_DAYS = 400;              // leads, calls and tasks older than this are not kept
const FULL_SYNC_EVERY = 24 * 3600e3;   // full reload once a day to drop deleted records
const AUTO_SYNC_EVERY = 120e3;         // pull changes every 2 minutes while the page is open

function bxError(method, err) {
  const ex = err && err.ex;
  return new Error(`${method}: ${ex ? `${ex.error} — ${ex.error_description || ""}` : String(err)}`);
}

function callMethod(method, params) {
  return new Promise((resolve, reject) =>
    BX24.callMethod(method, params, res => res.error() ? reject(bxError(method, res.error())) : resolve(res)));
}

function callBatch(calls) {
  return new Promise((resolve, reject) => BX24.callBatch(calls, results => {
    for (const [key, res] of Object.entries(results)) {
      if (res.error()) return reject(bxError(calls[key][0], res.error()));
    }
    resolve(results);
  }, true));
}

async function listAll(method, params, resultKey, onProgress) {
  const rowsOf = d => resultKey ? (d && d[resultKey]) || [] : d || [];
  const first = await callMethod(method, {...params, start: 0});
  const rows = rowsOf(first.data());
  const total = Number(first.total() || 0);
  onProgress && onProgress(rows.length, total);
  const offsets = [];
  for (let o = PAGE; o < total; o += PAGE) offsets.push(o);
  for (let i = 0; i < offsets.length; i += BATCH) {
    const calls = {};
    for (const o of offsets.slice(i, i + BATCH)) calls["p" + o] = [method, {...params, start: o}];
    const results = await callBatch(calls);
    for (const key of Object.keys(calls)) rows.push(...rowsOf(results[key].data()));
    onProgress && onProgress(rows.length, total);
  }
  return rows;
}

// ---------- local copy kept in the browser (IndexedDB) ----------

const ENTITIES = {
  deals: {label: "сделки", method: "crm.deal.list", id: "ID", modified: "DATE_MODIFY",
    select: ["ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "STAGE_SEMANTIC_ID", "OPPORTUNITY", "CURRENCY_ID",
             "ASSIGNED_BY_ID", "DATE_CREATE", "CLOSEDATE", "CLOSED", "DATE_MODIFY"],
    filter: since => ({})},
  leads: {label: "лиды", method: "crm.lead.list", id: "ID", modified: "DATE_MODIFY",
    select: ["ID", "TITLE", "STATUS_ID", "STATUS_SEMANTIC_ID", "SOURCE_ID", "ASSIGNED_BY_ID", "DATE_CREATE",
             "OPPORTUNITY", "DATE_MODIFY"],
    filter: since => ({">=DATE_CREATE": since})},
  calls: {label: "звонки", method: "crm.activity.list", id: "ID", modified: "LAST_UPDATED",
    select: ["ID", "RESPONSIBLE_ID", "DIRECTION", "COMPLETED", "CREATED", "LAST_UPDATED"],
    filter: since => ({TYPE_ID: 2, ">=CREATED": since})},
  tasks: {label: "задачи", method: "tasks.task.list", id: "id", modified: "CHANGED_DATE", rowModified: "changedDate",
    resultKey: "tasks",
    select: ["ID", "TITLE", "STATUS", "DEADLINE", "RESPONSIBLE_ID", "CLOSED_DATE", "CREATED_DATE", "CHANGED_DATE"],
    filter: since => ({">=CHANGED_DATE": since}),
    extraFilter: {"!STATUS": 5}},  // old tasks that are still open
};

const emptyStore = () => ({ref: {}, rows: {deals: {}, leads: {}, calls: {}, tasks: {}}, cursors: {},
                           tasksError: null, fullSyncAt: 0, syncedAt: 0});
let store = emptyStore();
let storeKey = "data";

function idb(mode, fn) {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open("beka-dashboard", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("kv");
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction("kv", mode);
      const req = fn(tx.objectStore("kv"));
      tx.oncomplete = () => { open.result.close(); resolve(req && req.result); };
      tx.onerror = () => reject(tx.error);
    };
  });
}
async function loadStore() {
  try { const saved = await idb("readonly", s => s.get(storeKey)); if (saved) store = {...emptyStore(), ...saved}; }
  catch (e) { console.warn("IndexedDB недоступен", e); }
}
async function saveStore() {
  try { await idb("readwrite", s => s.put(store, storeKey)); } catch (e) { console.warn("Не удалось сохранить", e); }
}

async function loadReference() {
  const ref = {stage_names: {}, stage_sort: {}, lead_status: {}, sources: {}, pipelines: {"0": "Общая"}, users: {}};
  const statuses = (await callMethod("crm.status.list", {order: {SORT: "ASC"}})).data() || [];
  for (const s of statuses) {
    const entity = s.ENTITY_ID || "";
    if (entity.startsWith("DEAL_STAGE")) { ref.stage_names[s.STATUS_ID] = s.NAME; ref.stage_sort[s.STATUS_ID] = Number(s.SORT || 0); }
    else if (entity === "STATUS") ref.lead_status[s.STATUS_ID] = s.NAME;
    else if (entity === "SOURCE") ref.sources[s.STATUS_ID] = s.NAME;
  }
  try {
    const cats = ((await callMethod("crm.category.list", {entityTypeId: 2})).data() || {}).categories || [];
    for (const c of cats) ref.pipelines[String(c.id)] = c.name;
  } catch (e) { console.warn("Воронки недоступны", e); }
  for (const u of await listAll("user.get", {FILTER: {}})) {
    ref.users[String(u.ID)] = [u.NAME, u.LAST_NAME].filter(Boolean).join(" ").trim() || u.EMAIL || "#" + u.ID;
  }
  return ref;
}

let syncing = null;
function sync(onProgress) {
  if (!syncing) syncing = doSync(onProgress).finally(() => { syncing = null; });
  return syncing;
}

async function doSync(onProgress) {
  const full = Date.now() - store.fullSyncAt > FULL_SYNC_EVERY;
  const sinceDate = new Date(Date.now() - HISTORY_DAYS * 864e5);
  const since = iso(sinceDate) + "T00:00:00";
  const next = full ? emptyStore() : {...store, rows: {...store.rows}, cursors: {...store.cursors}};

  onProgress && onProgress("справочники");
  next.ref = await loadReference();

  for (const [name, spec] of Object.entries(ENTITIES)) {
    const cursor = full ? null : store.cursors[name];
    const filters = [cursor ? {...spec.filter(since), [">=" + spec.modified]: cursor} : spec.filter(since)];
    if (!cursor && spec.extraFilter) filters.push(spec.extraFilter);
    let rows = [];
    try {
      for (const filter of filters) {
        rows = rows.concat(await listAll(spec.method, {order: {ID: "ASC"}, filter, select: spec.select}, spec.resultKey,
          (done, total) => onProgress && onProgress(`${spec.label}: ${done} из ${total}`)));
      }
    } catch (e) {
      if (name !== "tasks") throw e;
      next.tasksError = /insufficient_scope|ACCESS_DENIED/i.test(e.message)
        ? "Нет прав на задачи — добавьте приложению право «Задачи (task)»" : "Задачи не загрузились: " + e.message;
      continue;
    }
    if (name === "tasks") next.tasksError = null;
    const target = {...(full ? {} : next.rows[name])};
    for (const r of rows) target[String(r[spec.id])] = r;
    next.rows[name] = target;
    const mk = spec.rowModified || spec.modified;
    let max = next.cursors[name] || "";
    for (const r of rows) if (r[mk] && r[mk] > max) max = r[mk];
    if (max) next.cursors[name] = max;
  }
  next.syncedAt = Date.now();
  if (full) next.fullSyncAt = next.syncedAt;
  store = next;
  await saveStore();
}

// ---------- summary for a period (pure, from the local copy) ----------

const dayOf = v => v ? String(v).slice(0, 10) : null;
const num = v => { const n = parseFloat(v); return isNaN(n) ? 0 : n; };
const inPeriod = (v, f, t) => { const d = dayOf(v); return d !== null && d >= f && d <= t; };
const countBy = (arr, keyFn) => arr.reduce((m, x) => { const k = keyFn(x); m[k] = (m[k] || 0) + 1; return m; }, {});
const sortedPairs = m => Object.entries(m).sort((a, b) => b[1] - a[1]);

function summarize(dFrom, dTo) {
  const ref = store.ref, users = ref.users || {};
  const userName = id => users[String(id)] || "#" + id;
  const all = name => Object.values(store.rows[name]);

  const deals = all("deals");
  const dealsNew = deals.filter(d => inPeriod(d.DATE_CREATE, dFrom, dTo));
  const dealsClosed = deals.filter(d => d.CLOSED === "Y" && inPeriod(d.CLOSEDATE, dFrom, dTo));
  const dealsOpen = deals.filter(d => d.CLOSED !== "Y");
  const won = dealsClosed.filter(d => d.STAGE_SEMANTIC_ID === "S");
  const lost = dealsClosed.filter(d => d.STAGE_SEMANTIC_ID === "F");
  const leads = all("leads").filter(l => inPeriod(l.DATE_CREATE, dFrom, dTo));
  const calls = all("calls").filter(c => inPeriod(c.CREATED, dFrom, dTo));
  const tasks = all("tasks");
  const tasksOpen = tasks.filter(t => ![5, 7].includes(Number(t.status)));
  const tasksDone = tasks.filter(t => Number(t.status) === 5 && inPeriod(t.closedDate, dFrom, dTo));
  const now = Date.now();
  const isOverdue = t => t.deadline && new Date(t.deadline).getTime() < now;
  const overdue = tasksOpen.filter(isOverdue);

  const days = [];
  for (let d = new Date(dFrom + "T12:00:00"); iso(d) <= dTo; d.setDate(d.getDate() + 1)) days.push(iso(d));
  const sumBy = (arr, dateKey) => arr.reduce((m, x) => { const k = dayOf(x[dateKey]); m[k] = (m[k] || 0) + num(x.OPPORTUNITY); return m; }, {});
  const revenueByDay = sumBy(won, "CLOSEDATE");
  const createdByDay = countBy(dealsNew, d => dayOf(d.DATE_CREATE));
  const leadsByDay = countBy(leads, l => dayOf(l.DATE_CREATE));

  const funnel = {};
  for (const d of dealsOpen) {
    const cat = String(d.CATEGORY_ID || "0");
    const stages = funnel[cat] = funnel[cat] || {};
    const s = stages[d.STAGE_ID] = stages[d.STAGE_ID] || {count: 0, sum: 0};
    s.count++; s.sum += num(d.OPPORTUNITY);
  }
  const stageSort = ref.stage_sort || {}, stageNames = ref.stage_names || {};
  const funnels = Object.keys(funnel).sort().map(cat => ({
    name: (ref.pipelines || {})[cat] || "Воронка " + cat,
    stages: Object.entries(funnel[cat]).sort((a, b) => (stageSort[a[0]] || 0) - (stageSort[b[0]] || 0))
      .map(([id, v]) => ({stage: stageNames[id] || id, ...v})),
  }));

  const mgr = {};
  const m = id => mgr[String(id)] = mgr[String(id)] || {deals_new: 0, won: 0, won_sum: 0, lost: 0, leads: 0, calls: 0,
    calls_out: 0, tasks_open: 0, tasks_overdue: 0, tasks_done: 0, pipeline_sum: 0};
  dealsNew.forEach(d => m(d.ASSIGNED_BY_ID).deals_new++);
  won.forEach(d => { const x = m(d.ASSIGNED_BY_ID); x.won++; x.won_sum += num(d.OPPORTUNITY); });
  lost.forEach(d => m(d.ASSIGNED_BY_ID).lost++);
  dealsOpen.forEach(d => m(d.ASSIGNED_BY_ID).pipeline_sum += num(d.OPPORTUNITY));
  leads.forEach(l => m(l.ASSIGNED_BY_ID).leads++);
  calls.forEach(c => { const x = m(c.RESPONSIBLE_ID); x.calls++; if (String(c.DIRECTION) === "2") x.calls_out++; });
  tasksOpen.forEach(t => { const x = m(t.responsibleId); x.tasks_open++; if (isOverdue(t)) x.tasks_overdue++; });
  tasksDone.forEach(t => m(t.responsibleId).tasks_done++);
  const managers = Object.entries(mgr).map(([id, x]) => {
    const closed = x.won + x.lost;
    return {id, name: userName(id), ...x, conversion: closed ? Math.round(1000 * x.won / closed) / 10 : null};
  }).sort((a, b) => b.won_sum - a.won_sum || b.deals_new - a.deals_new);

  const wonSum = won.reduce((s, d) => s + num(d.OPPORTUNITY), 0);
  const closedTotal = won.length + lost.length;
  const leadStatus = ref.lead_status || {}, sources = ref.sources || {};
  return {
    period: {from: dFrom, to: dTo},
    generated_at: new Date(store.syncedAt).toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit"}),
    currency: (deals.find(d => d.CURRENCY_ID) || {}).CURRENCY_ID || "",
    kpi: {
      deals_new: dealsNew.length, won: won.length, won_sum: wonSum, lost: lost.length,
      conversion: closedTotal ? Math.round(1000 * won.length / closedTotal) / 10 : null,
      avg_check: won.length ? wonSum / won.length : 0,
      pipeline_count: dealsOpen.length, pipeline_sum: dealsOpen.reduce((s, d) => s + num(d.OPPORTUNITY), 0),
      leads: leads.length,
      leads_converted: leads.filter(l => l.STATUS_SEMANTIC_ID === "S").length,
      leads_junk: leads.filter(l => l.STATUS_SEMANTIC_ID === "F").length,
      calls: calls.length, tasks_open: tasksOpen.length, tasks_overdue: overdue.length, tasks_done: tasksDone.length,
    },
    timeline: {
      days,
      revenue: days.map(d => revenueByDay[d] || 0),
      deals_created: days.map(d => createdByDay[d] || 0),
      leads: days.map(d => leadsByDay[d] || 0),
    },
    funnels,
    leads_by_status: sortedPairs(countBy(leads, l => leadStatus[l.STATUS_ID] || l.STATUS_ID || "—")),
    leads_by_source: sortedPairs(countBy(leads, l => sources[l.SOURCE_ID] || l.SOURCE_ID || "Не указан")),
    managers,
    top_won: [...won].sort((a, b) => num(b.OPPORTUNITY) - num(a.OPPORTUNITY)).slice(0, 10).map(d => ({
      id: d.ID, title: d.TITLE || "Сделка #" + d.ID, sum: num(d.OPPORTUNITY), manager: userName(d.ASSIGNED_BY_ID),
      date: dayOf(d.CLOSEDATE)})),
    tasks_error: store.tasksError,
    overdue_tasks: [...overdue].sort((a, b) => String(a.deadline).localeCompare(String(b.deadline))).slice(0, 30)
      .map(t => ({id: t.id, title: t.title, deadline: dayOf(t.deadline), responsible: userName(t.responsibleId)})),
  };
}

// ---------- page flow ----------

function show() {
  if (!store.syncedAt) return;
  data = summarize($("#from").value, $("#to").value);
  render();
  $("#status").hidden = true; $("#content").hidden = false;
  if (window.BX24 && BX24.fitWindow) BX24.fitWindow();
}

async function refresh() {
  const first = !store.syncedAt;
  if (!first) $("#meta").textContent = "Обновляю…";
  try {
    await sync(p => {
      if (first) { $("#status").hidden = false; $("#status").className = "";
        $("#status").textContent = `Первая загрузка из Битрикса — это один раз, дальше всё будет открываться сразу. Сейчас: ${p}`; }
    });
    show();
  } catch (e) {
    console.error(e);
    if (first) { $("#status").hidden = false; $("#status").className = "error"; $("#status").textContent = "Не удалось загрузить: " + e.message; }
    else $("#meta").textContent = "Не удалось обновить: " + e.message;
  }
}

const money = v => nf.format(v) + (data.currency ? " " + data.currency : "");
function kpi(label, value, sub, cls) {
  return `<div class="card kpi"><div class="muted">${esc(label)}</div><div class="v ${cls || ""}">${value}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
}

function chart(id, cfg) {
  if (typeof Chart === "undefined") {
    $(id).parentElement.innerHTML = `<div class="muted">Графики не загрузились</div>`;
    return;
  }
  if (charts[id]) charts[id].destroy();
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--border");
  charts[id] = new Chart($(id), {...cfg, options: {responsive: true, maintainAspectRatio: false, ...(cfg.options || {})}});
}

function render() {
  const k = data.kpi;
  $("#meta").textContent = `${data.period.from} — ${data.period.to} · данные на ${data.generated_at}`;

  $("#kpi-sales").innerHTML =
    kpi("Выручка", money(k.won_sum), `${k.won} выиграно`, "good") +
    kpi("Новые сделки", nf.format(k.deals_new)) +
    kpi("Конверсия", k.conversion == null ? "—" : k.conversion + "%", `${k.won} выиграно / ${k.lost} проиграно`) +
    kpi("Средний чек", money(k.avg_check)) +
    kpi("В работе сейчас", money(k.pipeline_sum), `${k.pipeline_count} открытых сделок`) +
    kpi("Звонки", nf.format(k.calls));

  $("#kpi-leads").innerHTML =
    kpi("Новые лиды", nf.format(k.leads)) +
    kpi("Сконвертировано", nf.format(k.leads_converted), k.leads ? Math.round(100 * k.leads_converted / k.leads) + "% от лидов" : "", "good") +
    kpi("Некачественные", nf.format(k.leads_junk), "", "bad");

  $("#kpi-tasks").innerHTML =
    kpi("Открытые задачи", nf.format(k.tasks_open)) +
    kpi("Просрочено", nf.format(k.tasks_overdue), "", k.tasks_overdue ? "bad" : "") +
    kpi("Выполнено за период", nf.format(k.tasks_done), "", "good");

  const labels = data.timeline.days.map(d => d.slice(8, 10) + "." + d.slice(5, 7));
  chart("#c-revenue", {type: "bar", data: {labels, datasets: [{label: "Выручка", data: data.timeline.revenue, backgroundColor: css("--good")}]},
    options: {plugins: {legend: {display: false}}}});
  chart("#c-flow", {type: "line", data: {labels, datasets: [
    {label: "Сделки", data: data.timeline.deals_created, borderColor: css("--accent"), backgroundColor: css("--accent"), tension: .3},
    {label: "Лиды", data: data.timeline.leads, borderColor: css("--warn"), backgroundColor: css("--warn"), tension: .3}]}});

  const pie = (id, rows) => chart(id, {type: "doughnut",
    data: {labels: rows.map(r => r[0]), datasets: [{data: rows.map(r => r[1]), backgroundColor: PALETTE, borderColor: css("--card")}]},
    options: {plugins: {legend: {position: "right"}}}});
  pie("#c-sources", data.leads_by_source);
  pie("#c-lstatus", data.leads_by_status);

  $("#funnels").innerHTML = data.funnels.length ? data.funnels.map(f => {
    const max = Math.max(...f.stages.map(s => s.count), 1);
    return `<div class="muted" style="margin:8px 0 4px">${esc(f.name)}</div>` + f.stages.map(s =>
      `<div class="stage-row"><span>${esc(s.stage)}</span><div class="bar" style="width:${Math.max(2, 100 * s.count / max)}%"></div><span>${s.count}</span><span>${nf.format(s.sum)}</span></div>`).join("");
  }).join("") : `<div class="muted">Нет открытых сделок</div>`;

  $("#t-top").innerHTML = `<tr><th>Сделка</th><th>Менеджер</th><th>Дата</th><th class="num">Сумма</th></tr>` +
    (data.top_won.map(d => `<tr><td><a href="#" data-path="/crm/deal/details/${esc(d.id)}/">${esc(d.title)}</a></td>
      <td>${esc(d.manager)}</td><td>${esc(d.date)}</td><td class="num">${nf.format(d.sum)}</td></tr>`).join("")
     || `<tr><td colspan="4" class="muted">Нет выигранных сделок за период</td></tr>`);

  renderManagers();

  $("#tasks-error").hidden = !data.tasks_error;
  $("#tasks-error").textContent = data.tasks_error ? "⚠️ " + data.tasks_error : "";
  $("#t-overdue").innerHTML = `<tr><th>Задача</th><th>Ответственный</th><th>Дедлайн</th></tr>` +
    (data.overdue_tasks.map(t => `<tr><td><a href="#" data-path="/company/personal/user/0/tasks/task/view/${esc(t.id)}/">${esc(t.title)}</a></td>
      <td>${esc(t.responsible)}</td><td class="bad">${esc(t.deadline)}</td></tr>`).join("")
     || `<tr><td colspan="3" class="muted">Просроченных задач нет 👍</td></tr>`);
}

const MGR_COLS = [
  ["name", "Менеджер"], ["won_sum", "Выручка"], ["won", "Выиграно"], ["lost", "Проиграно"], ["conversion", "Конв., %"],
  ["deals_new", "Новые сделки"], ["pipeline_sum", "В работе"], ["leads", "Лиды"], ["calls", "Звонки"],
  ["tasks_open", "Задачи"], ["tasks_overdue", "Просрочено"], ["tasks_done", "Выполнено"],
];
function renderManagers() {
  const rows = [...data.managers].sort((a, b) => {
    const x = a[mgrSort.key] ?? -1, y = b[mgrSort.key] ?? -1;
    return (x > y ? 1 : x < y ? -1 : 0) * mgrSort.dir;
  });
  $("#t-managers").innerHTML = "<tr>" + MGR_COLS.map(([k, l]) =>
      `<th data-k="${k}" class="${k === "name" ? "" : "num"}">${l}${mgrSort.key === k ? (mgrSort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("") + "</tr>" +
    (rows.map(m => "<tr>" + MGR_COLS.map(([k]) => {
      let v = m[k];
      if (k === "name") return `<td>${esc(v)}</td>`;
      if (k === "conversion") v = v == null ? "—" : v;
      else v = nf.format(v);
      const cls = k === "tasks_overdue" && m[k] ? "num bad" : "num";
      return `<td class="${cls}">${v}</td>`;
    }).join("") + "</tr>").join("") || `<tr><td class="muted">Нет данных</td></tr>`);
  document.querySelectorAll("#t-managers th").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    mgrSort = {key: k, dir: mgrSort.key === k ? -mgrSort.dir : (k === "name" ? 1 : -1)};
    renderManagers();
  });
}

document.querySelectorAll("[data-preset]").forEach(b => b.onclick = () => { setPreset(b.dataset.preset); show(); });
$("#apply").onclick = () => { document.querySelectorAll("[data-preset]").forEach(b => b.classList.remove("active")); show(); };
$("#refresh").onclick = () => refresh();
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-path]");
  if (!a) return;
  e.preventDefault();
  BX24.openPath(a.dataset.path);
});
setPreset("month");

if (!window.BX24) {
  $("#status").className = "error";
  $("#status").textContent = "Это приложение открывается только внутри Битрикс24.";
} else {
  BX24.init(async () => {
    storeKey = "data:" + BX24.getDomain();
    await loadStore();
    show();
    await refresh();
    setInterval(refresh, AUTO_SYNC_EVERY);
  });
}
