/* ==========================================================================
   FACTORY GUARDIAN — 中央管理平台主控台前端
   --------------------------------------------------------------------------
   純前端、零建置：以原生 ES modules 語法寫成單一檔案，由 FastAPI 直接服務。

   結構：
     api      —— fetch 包裝，統一處理 401 與錯誤訊息
     state    —— 目前使用者、meta、輪詢器
     router   —— hash 路由（#/fleet、#/sites/TC-01…）
     views    —— 每個頁面一個 render 函式，回傳 DOM
     ui       —— 共用的小元件（徽章、表格、對話框、toast）

   權限：後端是唯一的權限來源；前端只依 permissions 隱藏按鈕，
   避免使用者按下註定 403 的東西。隱藏不是安全機制。
   ========================================================================== */
"use strict";

// ==================================================================== 工具
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const fmt = {
  num: (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "--" : Number(v).toFixed(d)),
  int: (v) => (v === null || v === undefined ? "--" : Math.round(Number(v)).toLocaleString()),
  pct: (v, d = 1) => (v === null || v === undefined ? "--" : `${Number(v).toFixed(d)}%`),
  // 時間一律轉成本地時間顯示；後端存 UTC。
  time: (iso) => {
    if (!iso) return "--";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "--" : d.toLocaleTimeString("zh-TW", { hour12: false });
  },
  datetime: (iso) => {
    if (!iso) return "--";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "--"
      : d.toLocaleString("zh-TW", { hour12: false, month: "2-digit", day: "2-digit",
                                    hour: "2-digit", minute: "2-digit" });
  },
  // 停留時間用「多久以前」表示，值班時比絕對時間好判斷。
  dur: (min) => {
    if (min === null || min === undefined) return "--";
    const m = Number(min);
    if (m < 1) return "<1 分";
    if (m < 60) return `${Math.round(m)} 分`;
    if (m < 1440) return `${Math.floor(m / 60)} 時 ${Math.round(m % 60)} 分`;
    return `${Math.floor(m / 1440)} 天`;
  },
};

// ==================================================================== API
const api = {
  async call(path, { method = "GET", body = null } = {}) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (body !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(`/api/v1${path}`, opts);
    if (res.status === 401) {
      state.user = null;
      renderLogin("登入已逾期，請重新登入。");
      throw new Error("unauthorized");
    }
    let data = null;
    try { data = await res.json(); } catch { /* 204 之類沒有 body */ }
    if (!res.ok) {
      const msg = data?.detail ?? `請求失敗（HTTP ${res.status}）`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  },
  get: (p) => api.call(p),
  post: (p, body) => api.call(p, { method: "POST", body: body ?? {} }),
  patch: (p, body) => api.call(p, { method: "PATCH", body }),
  del: (p) => api.call(p, { method: "DELETE" }),
};

// ==================================================================== 狀態
const state = {
  user: null,
  meta: null,
  poller: null,
  route: null,
  navCounts: { alarms: 0, workOrders: 0, approvals: 0 },
};

const can = (perm) => Boolean(state.user?.permissions?.includes(perm));

// ==================================================================== UI 元件
const ui = {
  toast(message, kind = "info") {
    let wrap = $(".toast-wrap");
    if (!wrap) {
      wrap = el("div", { class: "toast-wrap" });
      document.body.append(wrap);
    }
    const node = el("div", { class: `toast ${kind}` }, message);
    wrap.append(node);
    setTimeout(() => node.remove(), 5200);
  },

  severityBadge(sev) {
    const label = { critical: "CRITICAL", warning: "WARNING", info: "INFO" }[sev] ?? sev;
    return el("span", { class: `badge ${sev}` }, label);
  },

  stateBadge(value) {
    const map = {
      open: ["critical", "未處理"], acknowledged: ["warning", "已確認"],
      resolved: ["ok", "已結案"], closed: ["idle", "已關閉"],
      in_progress: ["warning", "進行中"], blocked: ["critical", "受阻"],
      completed: ["ok", "已完成"], cancelled: ["idle", "已取消"],
    };
    const [cls, label] = map[value] ?? ["idle", value];
    return el("span", { class: `badge ${cls}` }, label);
  },

  priorityBadge(p) {
    const map = { urgent: ["critical", "最高"], high: ["warning", "高"],
                  normal: ["idle", "一般"], low: ["idle", "低"] };
    const [cls, label] = map[p] ?? ["idle", p];
    return el("span", { class: `badge ${cls}` }, label);
  },

  table(columns, rows, { onRowClick = null, empty = "沒有資料" } = {}) {
    if (!rows.length) return el("div", { class: "empty" }, empty);
    const thead = el("thead", {}, el("tr", {}, columns.map((c) => el("th", {}, c.label))));
    const tbody = el("tbody", {}, rows.map((row) => {
      const tr = el("tr", { class: onRowClick ? "clickable" : "" },
        columns.map((c) => {
          const value = c.render ? c.render(row) : row[c.key];
          return el("td", { class: c.cls ?? "" },
            value instanceof Node ? value : String(value ?? "--"));
        }));
      if (onRowClick) tr.addEventListener("click", () => onRowClick(row));
      return tr;
    }));
    return el("div", { class: "tbl-wrap" }, el("table", {}, thead, tbody));
  },

  modal(title, content, actions = []) {
    const back = el("div", { class: "modal-back" });
    // 三條關閉路徑（按鈕、點背景、Esc）都要走同一個 close()，
    // 否則從按鈕關掉的對話框會把 keydown listener 永遠留在 document 上。
    const onEsc = (e) => { if (e.key === "Escape") close(); };
    const close = () => {
      document.removeEventListener("keydown", onEsc);
      back.remove();
    };
    const foot = el("div", { class: "modal-foot" },
      actions.map((a) => el("button", {
        class: `btn ${a.kind ?? ""}`,
        onclick: async () => {
          if (a.onClick) {
            const keep = await a.onClick();
            if (keep === false) return;
          }
          close();
        },
      }, a.label)));
    const box = el("div", { class: "modal" },
      el("div", { class: "card-h" }, el("b", {}, title)),
      el("div", { class: "card-b" }, content),
      foot);
    back.append(box);
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    document.addEventListener("keydown", onEsc);
    document.body.append(back);
    const firstInput = box.querySelector("input,textarea,select");
    if (firstInput) firstInput.focus();
    return back;
  },

  stat(label, value, sub, kind = "") {
    return el("div", { class: `stat ${kind}` },
      el("span", { class: "k" }, label),
      el("span", { class: "v" }, value),
      sub ? el("span", { class: "s" }, sub) : null);
  },

  /** 折線圖。用 SVG 手繪，避免引入圖表庫（現場網路不一定連得出去）。 */
  sparkline(series, { height = 120, width = 600 } = {}) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "spark");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("preserveAspectRatio", "none");
    const all = series.flatMap((s) => s.values).filter((v) => Number.isFinite(v));
    if (!all.length) return el("div", { class: "empty" }, "沒有足夠的資料點");
    const min = Math.min(...all), max = Math.max(...all);
    const span = max - min || 1;
    const pad = 6;
    // 背景水平格線
    for (let i = 0; i <= 2; i++) {
      const y = pad + ((height - pad * 2) * i) / 2;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("class", "gr");
      line.setAttribute("x1", 0); line.setAttribute("x2", width);
      line.setAttribute("y1", y); line.setAttribute("y2", y);
      svg.append(line);
    }
    for (const s of series) {
      const pts = s.values.map((v, i) => {
        const x = (width * i) / Math.max(1, s.values.length - 1);
        const y = height - pad - ((v - min) / span) * (height - pad * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
      const poly = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
      poly.setAttribute("class", `ln ${s.cls ?? ""}`);
      poly.setAttribute("points", pts);
      svg.append(poly);
    }
    return svg;
  },

  kv(pairs) {
    return el("dl", { class: "kv" },
      pairs.flatMap(([k, v, cjk]) => [
        el("dt", {}, k),
        el("dd", { class: cjk ? "cjk" : "" }, v instanceof Node ? v : String(v ?? "--")),
      ]));
  },
};

// ==================================================================== 登入
function renderLogin(errorMessage = "") {
  stopPolling();
  document.body.innerHTML = "";
  const error = el("div", { class: "err", hidden: !errorMessage }, errorMessage);
  const username = el("input", { type: "text", id: "lu", autocomplete: "username", required: true });
  const password = el("input", { type: "password", id: "lp", autocomplete: "current-password", required: true });
  const submit = el("button", { class: "btn primary", type: "submit", style: "width:100%" }, "登入");

  const form = el("form", { class: "card" },
    el("div", { class: "card-b" },
      error,
      el("label", { class: "field" }, el("span", {}, "帳號"), username),
      el("label", { class: "field" }, el("span", {}, "密碼"), password),
      submit));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    submit.textContent = "登入中…";
    try {
      const res = await api.call("/auth/login", {
        method: "POST",
        body: { username: username.value.trim(), password: password.value },
      });
      state.user = res.user;
      await boot();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
      submit.textContent = "登入";
      password.select();
    }
  });

  document.body.append(el("div", { class: "login-wrap" },
    el("div", { class: "login" },
      el("div", { class: "head" },
        el("div", { class: "mark" }),
        el("h1", {}, "Factory Guardian"),
        el("p", {}, "中央管理平台")),
      form,
      el("div", { class: "hint" },
        "示範帳號（依角色權限不同）：", el("br"),
        el("code", {}, "admin / admin12345"), " 系統管理員", el("br"),
        el("code", {}, "engineer / engineer123"), " 設備工程師（可核准）", el("br"),
        el("code", {}, "operator / operator123"), " 現場操作員", el("br"),
        el("code", {}, "viewer / viewer12345"), " 檢視者（唯讀）"))));
  username.focus();
}

// ==================================================================== 外殼
function renderShell() {
  document.body.innerHTML = "";
  const nav = el("nav", { class: "nav", id: "nav" });
  const main = el("main", { class: "main", id: "main" });
  const ctx = el("div", { class: "ctx", id: "topCtx" });

  const logout = el("button", { class: "btn sm", onclick: async () => {
    try { await api.post("/auth/logout"); } catch { /* 已登出也無妨 */ }
    state.user = null;
    renderLogin("已登出。");
  } }, "登出");

  document.body.append(el("div", { class: "app" },
    el("div", { class: "brand" },
      el("i", { class: "mark" }),
      el("b", {}, "GUARDIAN"),
      el("span", {}, "CMP")),
    el("header", { class: "top" },
      ctx,
      el("div", { class: "spacer" }),
      el("span", { class: "badge idle" }, `${state.user.display_name}・${state.user.role_label}`),
      logout),
    nav, main));
  renderNav();
}

const NAV_ITEMS = [
  { group: "監控" },
  { href: "#/fleet", label: "廠區總覽", perm: "fleet:read" },
  { href: "#/sites", label: "站點清單", perm: "site:read" },
  { group: "維運" },
  { href: "#/alarms", label: "告警管理", perm: "alarm:read", count: "alarms" },
  { href: "#/work-orders", label: "工單管理", perm: "workorder:read", count: "workOrders" },
  { href: "#/handovers", label: "班別交接", perm: "site:read" },
  { group: "分析" },
  { href: "#/analytics", label: "維運指標", perm: "analytics:read" },
  { href: "#/audit", label: "操作稽核", perm: "audit:read" },
  { group: "管理" },
  { href: "#/admin", label: "系統管理", perm: "user:manage" },
];

function renderNav() {
  const nav = $("#nav");
  if (!nav) return;
  nav.innerHTML = "";
  for (const item of NAV_ITEMS) {
    if (item.group) {
      // 群組標題只在底下至少有一個可見項目時才畫出來。
      const idx = NAV_ITEMS.indexOf(item);
      const following = NAV_ITEMS.slice(idx + 1);
      const until = following.findIndex((i) => i.group);
      const scope = until === -1 ? following : following.slice(0, until);
      if (!scope.some((i) => !i.perm || can(i.perm))) continue;
      nav.append(el("div", { class: "group" }, item.group));
      continue;
    }
    if (item.perm && !can(item.perm)) continue;
    const count = item.count ? state.navCounts[item.count] : 0;
    const link = el("a", { href: item.href },
      el("span", {}, item.label),
      count ? el("span", { class: `count ${item.count === "alarms" ? "hot" : ""}` }, count) : null);
    if (location.hash === item.href || location.hash.startsWith(item.href + "/")) {
      link.setAttribute("aria-current", "page");
    }
    nav.append(link);
  }
}

function setContext(text) {
  const ctx = $("#topCtx");
  if (ctx) ctx.innerHTML = text;
}

// ==================================================================== 輪詢
//
// 每次切頁都會 stopPolling()。但 paint() 是 async 的：切頁當下可能有一個
// fetch 還在飛，它 resolve 時原本的 DOM 已經被 route() 清掉，接著就會對
// null 取 .innerHTML 而炸掉。所以每個輪詢器帶一個世代編號，回來時先確認
// 自己還是當前世代，不是就安靜結束。
let pollGeneration = 0;

function startPolling(fn, intervalMs = 4000) {
  stopPolling();
  const generation = pollGeneration;
  const guarded = async () => {
    if (generation !== pollGeneration) return;
    try {
      await fn();
    } catch (err) {
      if (err?.message !== "unauthorized" && generation === pollGeneration) {
        console.error("poll failed:", err);
      }
    }
  };
  guarded();
  state.poller = setInterval(guarded, intervalMs);
}

function stopPolling() {
  pollGeneration += 1;
  if (state.poller) { clearInterval(state.poller); state.poller = null; }
}

// ==================================================================== 頁面：廠區總覽
async function viewFleet(main) {
  main.innerHTML = "";
  const head = el("div", { class: "page-head" },
    el("div", {}, el("h1", {}, "廠區總覽"),
      el("p", {}, "所有站點的即時營運狀態。點任一站點進入主控台。")),
    el("div", { class: "spacer" }),
    el("span", { class: "note", id: "fleetTime" }));
  const stats = el("div", { class: "grid g4", id: "fleetStats" });
  const cards = el("div", { class: "grid g2", id: "fleetCards", style: "margin-top:12px" });
  main.append(head, stats, cards);

  const paint = async () => {
    let data;
    try { data = await api.get("/fleet/overview"); } catch { return; }
    const t = data.totals;
    state.navCounts.alarms = t.alarms_total;
    state.navCounts.workOrders = t.open_work_orders;
    renderNav();
    setContext(`全廠區　<b>${t.sites_running}/${t.site_count}</b> 站點運行中`);
    $("#fleetTime").textContent = `更新於 ${fmt.time(data.generated_at)}`;

    stats.innerHTML = "";
    stats.append(
      ui.stat("站點運行", `${t.sites_running}/${t.site_count}`,
        `${t.simulated_sites} 個模擬站點`, t.sites_running < t.site_count ? "warn" : "ok"),
      ui.stat("未處理告警", String(t.alarms_total),
        `其中 ${t.alarms_critical} 件 CRITICAL`,
        t.alarms_critical > 0 ? "crit" : t.alarms_total > 0 ? "warn" : "ok"),
      ui.stat("待核准方案", String(t.pending_approvals),
        t.pending_approvals ? "需要工程師裁決" : "無等待中項目",
        t.pending_approvals > 0 ? "warn" : ""),
      ui.stat("未結工單", String(t.open_work_orders),
        `平均健康度 ${fmt.pct(t.avg_factory_health)}`,
        t.open_work_orders > 0 ? "warn" : "ok"));

    cards.innerHTML = "";
    for (const site of data.sites) {
      const hot = site.alarms.critical > 0 ? "has-critical"
        : site.alarms.warning > 0 ? "has-warning" : "";
      const card = el("button", { class: `site-card ${hot}`, type: "button",
        onclick: () => { location.hash = `#/sites/${site.site_id}`; } },
        el("div", { class: "sc-h" },
          el("i", { class: `dot ${site.status}` }),
          el("b", {}, site.name),
          el("span", { class: "sid" }, site.site_id),
          el("div", { class: "spacer" }),
          site.simulated ? el("span", { class: "badge sim" }, "SIM") : null,
          site.pending_approval ? el("span", { class: "badge warning" }, "待核准") : null),
        el("div", { class: "sc-b" },
          el("div", {}, el("span", { class: "k" }, "健康度"),
            el("span", { class: "v" }, fmt.num(site.factory_health, 1))),
          el("div", {}, el("span", { class: "k" }, "產出"),
            el("span", { class: "v" }, fmt.num(site.production_pct, 0) + "%")),
          el("div", {}, el("span", { class: "k" }, "告警"),
            el("span", { class: "v", style: site.alarms.total ? "color:var(--critical)" : "" },
              String(site.alarms.total))),
          el("div", {}, el("span", { class: "k" }, "停機機台"),
            el("span", { class: "v" }, `${site.machines_down}/${site.machine_count}`))),
        el("div", { class: "sc-f" },
          el("span", { class: "badge idle" }, site.region || "未分區"),
          el("span", { class: "badge idle" }, site.autopilot ? "AUTOPILOT ON" : "AUTOPILOT OFF"),
          site.last_error ? el("span", { class: "badge critical" }, "錯誤") : null,
          el("span", { class: "note", style: "margin-left:auto" },
            site.last_poll_at ? `更新 ${fmt.time(site.last_poll_at)}` : "尚未啟動")));
      cards.append(card);
    }
    if (!data.sites.length) {
      cards.append(el("div", { class: "card" },
        el("div", { class: "empty" }, "尚未註冊任何站點。請至「系統管理」新增。")));
    }
  };
  startPolling(paint, 4000);
}

// ==================================================================== 頁面：站點清單
async function viewSites(main) {
  main.innerHTML = "";
  main.append(el("div", { class: "page-head" },
    el("div", {}, el("h1", {}, "站點清單"),
      el("p", {}, "所有已註冊站點與其資料來源設定。"))));
  const card = el("div", { class: "card" });
  main.append(card);

  const paint = async () => {
    const [{ sites }, overview] = await Promise.all([
      api.get("/sites"), api.get("/fleet/overview"),
    ]);
    const live = Object.fromEntries(overview.sites.map((s) => [s.site_id, s]));
    card.innerHTML = "";
    card.append(ui.table([
      { label: "狀態", render: (r) => el("i", { class: `dot ${live[r.site_id]?.status ?? "stopped"}` }) },
      { label: "站點", cls: "cjk", render: (r) => r.name },
      { label: "代號", cls: "mono", key: "site_id" },
      { label: "區域", cls: "cjk", render: (r) => r.region || "--" },
      { label: "資料來源", render: (r) => el("span", { class: `badge ${r.adapter_kind === "simulated" ? "sim" : "idle"}` }, r.adapter_kind) },
      { label: "健康度", cls: "num", render: (r) => fmt.num(live[r.site_id]?.factory_health, 1) },
      { label: "告警", cls: "num", render: (r) => String(live[r.site_id]?.alarms?.total ?? 0) },
      { label: "啟用", render: (r) => el("span", { class: `badge ${r.enabled ? "ok" : "idle"}` }, r.enabled ? "啟用" : "停用") },
    ], sites, {
      onRowClick: (r) => { location.hash = `#/sites/${r.site_id}`; },
      empty: "尚未註冊任何站點。",
    }));
  };
  startPolling(paint, 6000);
}

// ==================================================================== 頁面：站點主控台
async function viewSiteDetail(main, siteId) {
  main.innerHTML = "";
  const head = el("div", { class: "page-head" },
    el("div", {}, el("h1", { id: "sdName" }, siteId),
      el("p", { id: "sdSub" }, "載入中…")),
    el("div", { class: "spacer" }),
    el("div", { class: "btn-row", id: "sdActions" }));
  const approvalSlot = el("div", { id: "sdApproval" });
  const stats = el("div", { class: "grid g4", id: "sdStats" });
  const body = el("div", { class: "grid g2", style: "margin-top:12px" },
    el("div", { class: "card" },
      el("div", { class: "card-h" }, el("b", {}, "機台狀態")),
      el("div", { class: "card-b flush", id: "sdMachines" })),
    el("div", { class: "card" },
      el("div", { class: "card-h" }, el("b", {}, "Agent 活動"),
        el("div", { class: "spacer" }),
        el("span", { class: "note", id: "sdBusy" })),
      el("div", { class: "timeline", id: "sdTimeline" })));
  const trend = el("div", { class: "card", style: "margin-top:12px" },
    el("div", { class: "card-h" }, el("b", {}, "KPI 走勢"),
      el("div", { class: "spacer" }),
      el("span", { class: "note" }, "綠＝健康度　藍＝產出%")),
    el("div", { class: "card-b", id: "sdTrend" }));
  main.append(head, approvalSlot, stats, body, trend);

  let lastSeq = 0;
  const timelineRows = [];

  const paint = async () => {
    let detail;
    try {
      detail = await api.get(`/sites/${encodeURIComponent(siteId)}`);
    } catch (err) {
      main.innerHTML = "";
      main.append(el("div", { class: "card" }, el("div", { class: "empty" }, err.message)));
      stopPolling();
      return;
    }
    setContext(`<b>${esc(detail.name)}</b>　${esc(detail.site_id)}`);
    $("#sdName").textContent = detail.name;
    $("#sdSub").textContent =
      `${detail.region || "未分區"}　tick ${detail.tick}　` +
      `${detail.simulated ? "模擬資料源" : detail.adapter_kind}　` +
      `${detail.autopilot ? "AUTOPILOT 開啟" : "AUTOPILOT 關閉"}`;

    // ---- 操作按鈕（依權限顯示）
    const actions = $("#sdActions");
    actions.innerHTML = "";
    if (can("site:control")) {
      actions.append(
        el("button", { class: "btn sm", onclick: async () => {
          try {
            await api.post(`/sites/${siteId}/control`, { autopilot: !detail.autopilot });
            ui.toast(`AUTOPILOT 已${detail.autopilot ? "關閉" : "開啟"}`, "ok");
            paint();
          } catch (e) { ui.toast(e.message, "err"); }
        } }, detail.autopilot ? "關閉 AUTOPILOT" : "開啟 AUTOPILOT"),
        el("button", { class: "btn sm", disabled: detail.busy, onclick: async () => {
          try { await api.post(`/sites/${siteId}/run-loop`); ui.toast("已觸發一次閉環處理", "ok"); }
          catch (e) { ui.toast(e.message, "err"); }
        } }, "手動執行閉環"),
        detail.status === "stopped"
          ? el("button", { class: "btn sm go", onclick: async () => {
              try { await api.post(`/sites/${siteId}/start`); ui.toast("站點已啟動", "ok"); paint(); }
              catch (e) { ui.toast(e.message, "err"); }
            } }, "啟動站點")
          : el("button", { class: "btn sm", onclick: async () => {
              try { await api.post(`/sites/${siteId}/stop`); ui.toast("站點已停止", "ok"); paint(); }
              catch (e) { ui.toast(e.message, "err"); }
            } }, "停止站點"));
      if (detail.simulated) actions.append(injectButton(siteId));
    }

    // ---- 待核准橫幅
    const slot = $("#sdApproval");
    slot.innerHTML = "";
    const pending = detail.pending_approval_detail;
    if (pending) {
      slot.append(renderApproval(siteId, pending, paint));
    } else if (!detail.simulated) {
      slot.append(el("div", { class: "warnbar" },
        "此站點使用真實資料源，控制動作將直接下行至現場設備。"));
    }

    // ---- KPI
    const s = detail;
    stats.innerHTML = "";
    stats.append(
      ui.stat("廠區健康度", fmt.num(s.factory_health, 1),
        "EQUIPMENT HEALTH", s.factory_health < 70 ? "crit" : s.factory_health < 90 ? "warn" : "ok"),
      ui.stat("產線產出", fmt.pct(s.production_pct, 0), "相對名目產能",
        s.production_pct < 70 ? "crit" : s.production_pct < 95 ? "warn" : "ok"),
      ui.stat("最大訂單延遲", `${fmt.num(s.max_delay_min, 0)} 分`, "已排程訂單",
        s.max_delay_min > 30 ? "crit" : s.max_delay_min > 0 ? "warn" : "ok"),
      ui.stat("未處理告警", String(s.alarms.total),
        `危險區曝露 ${fmt.num(s.hazard_exposure_min, 0)} 分`,
        s.alarms.critical ? "crit" : s.alarms.total ? "warn" : "ok"));

    $("#sdBusy").textContent = s.busy ? "閉環執行中…" : "";

    // ---- 機台
    const machines = Object.values(detail.snapshot?.machines ?? {});
    $("#sdMachines").innerHTML = "";
    $("#sdMachines").append(ui.table([
      { label: "機台", cls: "mono", key: "machine_id" },
      { label: "名稱", cls: "cjk", key: "name" },
      { label: "狀態", render: (m) => el("span", {
          class: `badge ${m.state === "running" ? "ok" : m.state === "maintenance" ? "warning"
                  : m.online ? "info" : "critical"}` }, m.state) },
      { label: "健康度", cls: "num", render: (m) => fmt.num(m.health, 1) },
      { label: "產速", cls: "num", render: (m) => fmt.num(m.production_rate_uph, 0) },
      { label: "訊號", render: (m) => el("span", { class: `badge ${
          m.worst_band === "critical" ? "critical" : m.worst_band === "warning" ? "warning" : "ok"}` },
          m.worst_band) },
    ], machines, { empty: "沒有機台資料" }));

    // ---- 走勢
    const history = detail.history ?? [];
    $("#sdTrend").innerHTML = "";
    if (history.length > 1) {
      $("#sdTrend").append(ui.sparkline([
        { values: history.map((h) => h.factory_health), cls: "health" },
        { values: history.map((h) => h.production_pct), cls: "prod" },
      ]));
    } else {
      $("#sdTrend").append(el("div", { class: "empty" }, "累積中，稍候即可看到走勢"));
    }
  };

  // 事件流獨立輪詢，頻率高一些，讓 Agent 活動看起來是即時的。
  const pumpEvents = async () => {
    let data;
    try { data = await api.get(`/sites/${encodeURIComponent(siteId)}/events?since=${lastSeq}`); }
    catch { return; }
    const tl = $("#sdTimeline");
    if (!tl) return;
    for (const ev of data.events) {
      lastSeq = ev.seq;
      timelineRows.unshift(ev);
      const cls = ev.stage === "error" ? "err"
        : ["verify", "loop_done"].includes(ev.stage) ? "ok" : "";
      const row = el("div", { class: "row" },
        el("span", { class: "t" }, fmt.time(ev.ts)),
        el("span", { class: `st ${cls}` }, ev.stage),
        el("span", { class: "msg" }, summarizeEvent(ev)));
      tl.prepend(row);
    }
    while (tl.children.length > 120) tl.lastChild.remove();
    if (!tl.children.length) tl.append(el("div", { class: "empty" }, "尚無事件"));
  };

  startPolling(async () => { await paint(); await pumpEvents(); }, 3000);
}

/** 把後端事件 payload 轉成一句人看得懂的話。
 *
 *  payload 的形狀直接對應 orchestrator 各階段送出的物件（多半是巢狀的，
 *  例如 impact 事件的內容在 payload.impact）。這裡逐一對應，不做猜測。 */
function summarizeEvent(ev) {
  const p = ev.payload ?? {};
  switch (ev.stage) {
    case "detect": {
      const e = p.event ?? {};
      return `偵測到異常：${e.machine_id ?? ""} ${(e.triggers ?? []).join("、")}`
        + (e.severity ? `（${e.severity}）` : "");
    }
    case "confirm": {
      const top = p.diagnosis?.candidates?.[0];
      return top
        ? `確認中：研判 ${top.label ?? top.fault_id}（信心 ${Math.round((top.confidence ?? 0) * 100)}%）`
        : `確認中（已等待 ${p.waited_ticks ?? 0}/${p.max_confirm_ticks ?? 0} tick）`;
    }
    case "diagnose": {
      const top = p.diagnosis?.candidates?.[0] ?? p.candidates?.[0];
      return top
        ? `研判根因：${top.label ?? top.fault_id}（信心 ${Math.round((top.confidence ?? 0) * 100)}%）`
        : "完成診斷";
    }
    case "impact": {
      const im = p.impact ?? {};
      return `影響評估：產出損失 ${fmt.num(im.production_loss_pct, 1)}%`
        + `，影響 ${im.affected_orders?.length ?? 0} 張訂單`
        + `，最大延遲 ${fmt.num(im.total_delay_min, 0)} 分`;
    }
    case "plan": {
      const n = p.plans?.length ?? 0;
      return `產生 ${n} 個候選方案：${(p.plans ?? []).map((x) => x.plan_id).join("、")}`;
    }
    case "rank": {
      const r = p.ranking ?? {};
      const best = r.recommended_plan_id;
      const top = (r.ranking ?? []).find((x) => x.plan_id === best);
      return `選定方案 ${best ?? "--"}${top?.title ? `：${top.title}` : ""}`;
    }
    case "safety": {
      const reviews = p.reviews ?? [];
      const blocked = reviews.filter((r) => r.blocked || r.verdict === "BLOCK");
      if (blocked.length) {
        return `安全裁決：否決 ${blocked.map((r) => r.plan_id).join("、")}`
          + `（${blocked[0].findings?.[0]?.message ?? blocked[0].verdict}）`;
      }
      return `安全裁決：${reviews.length} 個方案通過檢查`;
    }
    case "policy": {
      const d = p.decision ?? {};
      const risk = d.risk ? `風險 ${d.risk}` : "";
      const need = d.requires_approval ? "需人工核准" : d.allowed ? "可自動執行" : "不允許";
      return `治理判定 ${p.plan_id ?? ""}：${need}${risk ? `（${risk}）` : ""}`;
    }
    case "approve": {
      const a = p.approval ?? {};
      return `${a.approver ?? "系統"} ${a.approved ? "核准" : "退回"} ${a.plan_id ?? ""}`
        + (a.reason ? `：${a.reason}` : "");
    }
    case "approval_required": return `等待人工核准：${p.plan?.plan_id ?? ""}`;
    case "approval_submitted":
      return `${p.approver} ${p.approved ? "核准" : "退回"}了 ${p.plan_id}`;
    case "work_order": {
      const w = p.work_order ?? {};
      return `開立工單 ${w.work_order_id ?? ""}：${w.problem ?? ""}`
        + (w.estimated_repair_min ? `（預估 ${fmt.num(w.estimated_repair_min, 0)} 分）` : "");
    }
    case "execute": {
      const effects = p.effects ?? [];
      return `執行 ${p.plan_id ?? ""}：${effects.map((e) => e.kind ?? e.action ?? "").filter(Boolean).join("、") || `${effects.length} 個動作`}`;
    }
    case "verify": {
      const r = p.report ?? {};
      const passed = r.passed;
      const health = r.after?.factory_health;
      return passed
        ? `驗證通過${health !== undefined ? `，健康度回到 ${fmt.num(health, 1)}` : ""}`
        : "驗證未通過，重新規劃";
    }
    case "replan": return `第 ${p.attempt ?? "?"} 次重新規劃：${p.explanation ?? ""}`;
    case "escalate": return `升級人工處理：${p.reason ?? ""}`;
    case "loop_done": return `閉環完成（${p.result?.verified ? "已驗證" : "未通過"}）`;
    case "inject": return `注入情境：${p.title ?? p.scenario_id}（操作者 ${p.actor ?? "-"}）`;
    case "autopilot": return `AUTOPILOT ${p.enabled ? "開啟" : "關閉"}`;
    case "error": return `錯誤：${p.message ?? ""}`;
    case "idle": return p.message ?? "無異常";
    case "tick": return `推進至 tick ${p.snapshot?.tick ?? ""}`;
    default: return p.message ?? ev.stage;
  }
}

function renderApproval(siteId, pending, refresh) {
  const plan = pending.plan ?? {};
  const policy = pending.policy ?? {};
  const actions = (plan.actions ?? []).map((a) => a.kind ?? a).join("、");
  const box = el("div", { class: "approval" },
    el("div", { class: "ah" },
      el("i", { class: "dot handling" }),
      el("b", {}, "等待人工核准"),
      el("span", { class: "badge warning" }, plan.plan_id ?? "")),
    el("p", {}, plan.title ?? plan.summary ?? "Agent 提出一個需要核准的處置方案"),
    el("div", { class: "why" },
      `建議動作：${actions || "（無）"}`, el("br"),
      `治理判定：${policy.verdict ?? policy.decision ?? "需要核准"}`,
      policy.reason ? el("span", {}, ` — ${policy.reason}`) : null));

  if (can("approval:decide")) {
    const reason = el("input", { type: "text", placeholder: "裁決理由（選填）",
                                 style: "max-width:280px" });
    box.append(el("div", { class: "btn-row" },
      reason,
      el("button", { class: "btn go", onclick: async () => {
        try {
          await api.post(`/sites/${siteId}/approval`,
            { plan_id: plan.plan_id, approved: true, reason: reason.value });
          ui.toast("已核准，Agent 開始執行", "ok");
          refresh();
        } catch (e) { ui.toast(e.message, "err"); }
      } }, "核准執行"),
      el("button", { class: "btn primary", onclick: async () => {
        try {
          await api.post(`/sites/${siteId}/approval`,
            { plan_id: plan.plan_id, approved: false, reason: reason.value });
          ui.toast("已退回該方案", "ok");
          refresh();
        } catch (e) { ui.toast(e.message, "err"); }
      } }, "退回")));
  } else {
    box.append(el("div", { class: "note" },
      "你的角色沒有核准權限，請通知設備工程師或系統管理員處理。"));
  }
  return box;
}

function injectButton(siteId) {
  return el("button", { class: "btn sm", onclick: () => {
    const select = el("select", {},
      (state.meta?.scenarios ?? []).map((s) =>
        el("option", { value: s.scenario_id }, `${s.scenario_id}　${s.title}`)));
    ui.modal("注入測試情境", el("div", {},
      el("div", { class: "warnbar" },
        "此操作只適用於模擬站點，用於演練與驗收。操作會記入稽核。"),
      el("label", { class: "field" }, el("span", {}, "情境"), select)),
      [{ label: "取消" },
       { label: "注入", kind: "primary", onClick: async () => {
          try {
            await api.post(`/sites/${siteId}/inject`, { scenario_id: select.value });
            ui.toast("情境已注入，觀察 Agent 反應", "ok");
          } catch (e) { ui.toast(e.message, "err"); return false; }
        } }]);
  } }, "注入測試情境");
}

// ==================================================================== 頁面：告警
async function viewAlarms(main) {
  main.innerHTML = "";
  const filters = {
    site_id: "", state: "", severity: "", active_only: "true",
  };
  const siteSelect = el("select", { onchange: (e) => { filters.site_id = e.target.value; paint(); } },
    el("option", { value: "" }, "全部站點"));
  const stateSelect = el("select", { onchange: (e) => {
      filters.state = e.target.value;
      filters.active_only = e.target.value ? "false" : "true";
      paint();
    } },
    el("option", { value: "" }, "未結案（open + 已確認）"),
    el("option", { value: "open" }, "未處理"),
    el("option", { value: "acknowledged" }, "已確認"),
    el("option", { value: "resolved" }, "已結案"),
    el("option", { value: "closed" }, "已關閉"));
  const sevSelect = el("select", { onchange: (e) => { filters.severity = e.target.value; paint(); } },
    el("option", { value: "" }, "全部嚴重度"),
    el("option", { value: "critical" }, "CRITICAL"),
    el("option", { value: "warning" }, "WARNING"),
    el("option", { value: "info" }, "INFO"));

  main.append(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, "告警管理"),
        el("p", {}, "確認、指派與結案。超過 15 分鐘未確認的告警會標記為 STALE。"))),
    el("div", { class: "filters" }, siteSelect, stateSelect, sevSelect),
    el("div", { class: "card", id: "alarmCard" }));

  api.get("/sites").then(({ sites }) => {
    for (const s of sites) siteSelect.append(el("option", { value: s.site_id }, `${s.site_id}　${s.name}`));
  }).catch(() => {});

  const paint = async () => {
    const qs = new URLSearchParams({ limit: "200" });
    if (filters.site_id) qs.set("site_id", filters.site_id);
    if (filters.state) qs.set("state", filters.state);
    if (filters.severity) qs.set("severity", filters.severity);
    if (filters.active_only === "true") qs.set("active_only", "true");
    const data = await api.get(`/alarms?${qs}`);
    state.navCounts.alarms = data.alarms.filter((a) =>
      ["open", "acknowledged"].includes(a.state)).length;
    renderNav();

    const card = $("#alarmCard");
    card.innerHTML = "";
    card.append(ui.table([
      { label: "嚴重度", render: (a) => ui.severityBadge(a.severity) },
      { label: "狀態", render: (a) => el("span", {},
          ui.stateBadge(a.state),
          a.stale ? el("span", { class: "badge critical", style: "margin-left:4px" }, "STALE") : null) },
      { label: "站點", cls: "mono", key: "site_id" },
      { label: "機台", cls: "mono", render: (a) => a.machine_id || "--" },
      { label: "告警內容", cls: "cjk", key: "title" },
      { label: "次數", cls: "num", key: "occurrence_count" },
      { label: "持續", cls: "num", render: (a) => fmt.dur(a.age_min) },
      { label: "確認者", cls: "mono", render: (a) => a.acked_by || "--" },
      { label: "發生時間", cls: "mono", render: (a) => fmt.datetime(a.raised_at) },
    ], data.alarms, {
      onRowClick: (a) => openAlarm(a, paint),
      empty: "目前沒有符合條件的告警。",
    }));
    card.append(el("div", { class: "pager" }, `共 ${data.total} 筆`));
  };
  startPolling(paint, 5000);
}

function openAlarm(alarm, refresh) {
  const evidence = alarm.evidence ?? {};
  const readings = evidence.readings ?? {};
  const content = el("div", {},
    ui.kv([
      ["告警代號", alarm.alarm_id],
      ["站點 / 機台", `${alarm.site_id} / ${alarm.machine_id || "--"}`],
      ["嚴重度", ui.severityBadge(alarm.severity)],
      ["狀態", ui.stateBadge(alarm.state)],
      ["內容", alarm.title, true],
      ["說明", alarm.detail || "--", true],
      ["發生時間", fmt.datetime(alarm.raised_at)],
      ["重複次數", String(alarm.occurrence_count)],
      ["確認", alarm.acked_by ? `${alarm.acked_by}（${fmt.dur(alarm.ack_latency_min)}後）` : "尚未確認", true],
      ["結案", alarm.resolved_by ? `${alarm.resolved_by}　${alarm.resolution}` : "--", true],
      ["來源", alarm.source],
    ]));
  if (Object.keys(readings).length) {
    content.append(el("div", { style: "margin-top:12px" },
      el("div", { class: "note", style: "margin-bottom:6px" }, "觸發當下的感測器讀值"),
      ui.table([
        { label: "訊號", cls: "mono", render: (r) => r[0] },
        { label: "數值", cls: "num", render: (r) => `${fmt.num(r[1].value, 2)} ${r[1].unit ?? ""}` },
        { label: "區間", render: (r) => el("span", { class: `badge ${
            r[1].band === "critical" ? "critical" : r[1].band === "warning" ? "warning" : "ok"}` },
            r[1].band) },
      ], Object.entries(readings))));
  }

  const actions = [{ label: "關閉" }];
  if (alarm.state === "open" && can("alarm:ack")) {
    actions.push({ label: "確認告警", onClick: async () => {
      try { await api.post(`/alarms/${alarm.alarm_id}/acknowledge`, { note: "" });
            ui.toast("已確認告警", "ok"); refresh(); }
      catch (e) { ui.toast(e.message, "err"); return false; }
    } });
  }
  if (["open", "acknowledged"].includes(alarm.state) && can("alarm:resolve")) {
    actions.push({ label: "結案", kind: "go", onClick: async () => {
      const input = el("textarea", { placeholder: "請說明處理方式（必填）" });
      return new Promise((resolve) => {
        ui.modal("結案說明", el("label", { class: "field" },
          el("span", {}, "處理方式"), input),
          [{ label: "取消", onClick: () => { resolve(false); } },
           { label: "確認結案", kind: "go", onClick: async () => {
              if (!input.value.trim()) { ui.toast("請填寫處理方式", "err"); return false; }
              try {
                await api.post(`/alarms/${alarm.alarm_id}/resolve`, { resolution: input.value.trim() });
                ui.toast("告警已結案", "ok"); refresh(); resolve(true);
              } catch (e) { ui.toast(e.message, "err"); return false; }
            } }]);
      });
    } });
  }
  if (can("workorder:create")) {
    actions.push({ label: "開立工單", onClick: () => {
      openWorkOrderForm({ site_id: alarm.site_id, machine_id: alarm.machine_id,
                          title: alarm.title, alarm_id: alarm.alarm_id });
    } });
  }
  ui.modal(`告警 ${alarm.alarm_id}`, content, actions);
}

// ==================================================================== 頁面：工單
async function viewWorkOrders(main) {
  main.innerHTML = "";
  const filters = { site_id: "", state: "", open_only: "true" };
  const siteSelect = el("select", { onchange: (e) => { filters.site_id = e.target.value; paint(); } },
    el("option", { value: "" }, "全部站點"));
  const stateSelect = el("select", { onchange: (e) => {
      filters.state = e.target.value;
      filters.open_only = e.target.value ? "false" : "true";
      paint();
    } },
    el("option", { value: "" }, "未完成"),
    el("option", { value: "open" }, "待處理"),
    el("option", { value: "in_progress" }, "進行中"),
    el("option", { value: "blocked" }, "受阻"),
    el("option", { value: "completed" }, "已完成"),
    el("option", { value: "cancelled" }, "已取消"));
  const createBtn = can("workorder:create")
    ? el("button", { class: "btn primary", onclick: () => openWorkOrderForm({}, paint) }, "新增工單")
    : null;

  main.append(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, "工單管理"),
        el("p", {}, "Agent 自動開立與人工建立的維修工單。")),
      el("div", { class: "spacer" }), createBtn),
    el("div", { class: "filters" }, siteSelect, stateSelect),
    el("div", { class: "card", id: "woCard" }));

  api.get("/sites").then(({ sites }) => {
    for (const s of sites) siteSelect.append(el("option", { value: s.site_id }, `${s.site_id}　${s.name}`));
  }).catch(() => {});

  const paint = async () => {
    const qs = new URLSearchParams({ limit: "200" });
    if (filters.site_id) qs.set("site_id", filters.site_id);
    if (filters.state) qs.set("state", filters.state);
    if (filters.open_only === "true") qs.set("open_only", "true");
    const data = await api.get(`/work-orders?${qs}`);
    state.navCounts.workOrders = data.work_orders.filter((w) =>
      ["open", "in_progress", "blocked"].includes(w.state)).length;
    renderNav();

    const card = $("#woCard");
    card.innerHTML = "";
    card.append(ui.table([
      { label: "優先度", render: (w) => ui.priorityBadge(w.priority) },
      { label: "狀態", render: (w) => ui.stateBadge(w.state) },
      { label: "工單", cls: "mono", key: "work_order_id" },
      { label: "站點", cls: "mono", key: "site_id" },
      { label: "機台", cls: "mono", render: (w) => w.machine_id || "--" },
      { label: "內容", cls: "cjk", key: "title" },
      { label: "負責人", cls: "cjk", render: (w) => w.assignee || "未指派" },
      { label: "建立者", cls: "mono", key: "created_by" },
      { label: "建立", cls: "mono", render: (w) => fmt.datetime(w.created_at) },
    ], data.work_orders, {
      onRowClick: (w) => openWorkOrder(w, paint),
      empty: "目前沒有符合條件的工單。",
    }));
    card.append(el("div", { class: "pager" }, `共 ${data.total} 筆`));
  };
  startPolling(paint, 5000);
}

function openWorkOrder(wo, refresh) {
  const content = el("div", {},
    ui.kv([
      ["工單代號", wo.work_order_id],
      ["站點 / 機台", `${wo.site_id} / ${wo.machine_id || "--"}`],
      ["標題", wo.title, true],
      ["內容", el("span", { style: "white-space:pre-wrap" }, wo.detail || "--"), true],
      ["類型", wo.kind],
      ["優先度", ui.priorityBadge(wo.priority)],
      ["狀態", ui.stateBadge(wo.state)],
      ["負責人", wo.assignee || "未指派", true],
      ["建立者", wo.created_by],
      ["建立時間", fmt.datetime(wo.created_at)],
      ["開始 / 完成", `${fmt.datetime(wo.started_at)} / ${fmt.datetime(wo.completed_at)}`],
      ["實際工時", fmt.dur(wo.duration_min)],
      ["預估工時", wo.estimated_min ? `${fmt.num(wo.estimated_min, 0)} 分` : "--"],
      ["完成說明", wo.completion_note || "--", true],
    ]));

  const actions = [{ label: "關閉" }];
  if (can("workorder:update") && !["completed", "cancelled"].includes(wo.state)) {
    if (wo.state === "open") {
      actions.push({ label: "認領並開始", onClick: async () => {
        try {
          await api.patch(`/work-orders/${wo.work_order_id}`,
            { state: "in_progress", assignee: state.user.username });
          ui.toast("已認領工單", "ok"); refresh();
        } catch (e) { ui.toast(e.message, "err"); return false; }
      } });
    }
    actions.push({ label: "標記完成", kind: "go", onClick: async () => {
      const input = el("textarea", { placeholder: "完成說明（必填）" });
      return new Promise((resolve) => {
        ui.modal("完成工單", el("label", { class: "field" }, el("span", {}, "處理內容"), input),
          [{ label: "取消", onClick: () => resolve(false) },
           { label: "確認完成", kind: "go", onClick: async () => {
              if (!input.value.trim()) { ui.toast("請填寫完成說明", "err"); return false; }
              try {
                await api.patch(`/work-orders/${wo.work_order_id}`,
                  { state: "completed", completion_note: input.value.trim() });
                ui.toast("工單已完成", "ok"); refresh(); resolve(true);
              } catch (e) { ui.toast(e.message, "err"); return false; }
            } }]);
      });
    } });
  }
  ui.modal(`工單 ${wo.work_order_id}`, content, actions);
}

function openWorkOrderForm(preset = {}, refresh = null) {
  const meta = state.meta ?? {};
  const site = el("select", {});
  const title = el("input", { type: "text", value: preset.title ?? "" });
  const machine = el("input", { type: "text", value: preset.machine_id ?? "" });
  const detail = el("textarea", {});
  const kind = el("select", {}, (meta.work_order_kinds ?? ["corrective"]).map((k) =>
    el("option", { value: k }, k)));
  const priority = el("select", {}, (meta.work_order_priorities ?? ["normal"]).map((p) =>
    el("option", { value: p, selected: p === "normal" }, p)));
  const assignee = el("input", { type: "text", placeholder: "留空表示待指派" });
  const estimated = el("input", { type: "number", min: "0", step: "5", value: "0" });

  api.get("/sites").then(({ sites }) => {
    for (const s of sites) {
      site.append(el("option", { value: s.site_id,
        selected: s.site_id === preset.site_id }, `${s.site_id}　${s.name}`));
    }
  }).catch(() => {});

  ui.modal("新增工單", el("div", {},
    el("label", { class: "field" }, el("span", {}, "站點"), site),
    el("label", { class: "field" }, el("span", {}, "標題"), title),
    el("label", { class: "field" }, el("span", {}, "機台代號"), machine),
    el("label", { class: "field" }, el("span", {}, "內容說明"), detail),
    el("div", { class: "grid g3" },
      el("label", { class: "field" }, el("span", {}, "類型"), kind),
      el("label", { class: "field" }, el("span", {}, "優先度"), priority),
      el("label", { class: "field" }, el("span", {}, "預估工時（分）"), estimated)),
    el("label", { class: "field" }, el("span", {}, "負責人"), assignee)),
    [{ label: "取消" },
     { label: "建立工單", kind: "primary", onClick: async () => {
        if (!title.value.trim()) { ui.toast("請填寫標題", "err"); return false; }
        if (!site.value) { ui.toast("請選擇站點", "err"); return false; }
        try {
          await api.post("/work-orders", {
            site_id: site.value, title: title.value.trim(), machine_id: machine.value.trim(),
            detail: detail.value, kind: kind.value, priority: priority.value,
            assignee: assignee.value.trim(), estimated_min: Number(estimated.value) || 0,
            alarm_id: preset.alarm_id ?? null,
          });
          ui.toast("工單已建立", "ok");
          if (refresh) refresh();
        } catch (e) { ui.toast(e.message, "err"); return false; }
      } }]);
}

// ==================================================================== 頁面：交接
async function viewHandovers(main) {
  main.innerHTML = "";
  const createBtn = can("handover:write")
    ? el("button", { class: "btn primary", onclick: () => openHandoverForm(paint) }, "撰寫交接")
    : null;
  main.append(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, "班別交接"),
        el("p", {}, "交班記錄與未結案事項。系統會自動帶入當下未結的告警與工單。")),
      el("div", { class: "spacer" }), createBtn),
    el("div", { id: "hoList", class: "grid", style: "gap:12px" }));

  const paint = async () => {
    const { handovers } = await api.get("/handovers?limit=50");
    const list = $("#hoList");
    list.innerHTML = "";
    if (!handovers.length) {
      list.append(el("div", { class: "card" }, el("div", { class: "empty" }, "尚無交接記錄。")));
      return;
    }
    for (const h of handovers) {
      list.append(el("div", { class: "card" },
        el("div", { class: "card-h" },
          el("b", {}, `${h.site_id}　${h.shift}`),
          el("div", { class: "spacer" }),
          el("span", { class: "note" }, `${h.author}　${fmt.datetime(h.created_at)}`)),
        el("div", { class: "card-b" },
          el("p", { class: "note", style: "white-space:pre-wrap;margin:0 0 10px" }, h.summary),
          h.open_items?.length
            ? el("div", {},
                el("div", { class: "note", style: "margin-bottom:5px" },
                  `未結案事項（${h.open_items.length}）`),
                el("ul", { class: "note", style: "margin:0;padding-left:18px;line-height:1.9" },
                  h.open_items.map((i) => el("li", {}, i))))
            : el("div", { class: "note" }, "交班時無未結案事項。"))));
    }
  };
  startPolling(paint, 15000);
}

function openHandoverForm(refresh) {
  const site = el("select", {});
  const shift = el("select", {},
    ["早班", "中班", "夜班"].map((s) => el("option", { value: s }, s)));
  const summary = el("textarea", { placeholder: "本班重點、待追蹤事項、交接注意" });
  api.get("/sites").then(({ sites }) => {
    for (const s of sites) site.append(el("option", { value: s.site_id }, `${s.site_id}　${s.name}`));
  }).catch(() => {});

  ui.modal("撰寫班別交接", el("div", {},
    el("div", { class: "grid g2" },
      el("label", { class: "field" }, el("span", {}, "站點"), site),
      el("label", { class: "field" }, el("span", {}, "班別"), shift)),
    el("label", { class: "field" }, el("span", {}, "交接摘要"), summary),
    el("div", { class: "note" }, "送出時會自動附上該站點目前未結案的告警與工單清單。")),
    [{ label: "取消" },
     { label: "送出交接", kind: "primary", onClick: async () => {
        if (!summary.value.trim()) { ui.toast("請填寫交接摘要", "err"); return false; }
        try {
          await api.post("/handovers", { site_id: site.value, shift: shift.value,
                                         summary: summary.value.trim() });
          ui.toast("交接記錄已送出", "ok");
          if (refresh) refresh();
        } catch (e) { ui.toast(e.message, "err"); return false; }
      } }]);
}

// ==================================================================== 頁面：分析
async function viewAnalytics(main) {
  main.innerHTML = "";
  let siteId = "";
  let days = 7;
  const siteSelect = el("select", { onchange: (e) => { siteId = e.target.value; paint(); } },
    el("option", { value: "" }, "全部站點"));
  const daySelect = el("select", { onchange: (e) => { days = Number(e.target.value); paint(); } },
    el("option", { value: "1" }, "近 1 天"),
    el("option", { value: "7", selected: true }, "近 7 天"),
    el("option", { value: "30" }, "近 30 天"));

  main.append(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, "維運指標"),
        el("p", {}, "MTTA／MTTR、告警分布與工單負載。"))),
    el("div", { class: "filters" }, siteSelect, daySelect),
    el("div", { class: "grid g4", id: "anStats" }),
    el("div", { class: "grid g2", id: "anBody", style: "margin-top:12px" }));

  api.get("/sites").then(({ sites }) => {
    for (const s of sites) siteSelect.append(el("option", { value: s.site_id }, `${s.site_id}　${s.name}`));
  }).catch(() => {});

  const paint = async () => {
    const qs = new URLSearchParams({ days: String(days) });
    if (siteId) qs.set("site_id", siteId);
    const data = await api.get(`/analytics/summary?${qs}`);
    const a = data.alarms, w = data.work_orders;

    const stats = $("#anStats");
    stats.innerHTML = "";
    stats.append(
      ui.stat("告警總數", String(a.total), `${a.open_now} 件未結案`,
        a.open_now > 0 ? "warn" : "ok"),
      ui.stat("MTTA", a.mtta_min === null ? "--" : fmt.dur(a.mtta_min),
        "平均確認時間", a.mtta_min > 15 ? "warn" : "ok"),
      ui.stat("MTTR", a.mttr_min === null ? "--" : fmt.dur(a.mttr_min),
        "平均結案時間", a.mttr_min > 60 ? "warn" : "ok"),
      ui.stat("工單", String(w.total),
        `${w.open_now} 件未完成・平均 ${w.avg_duration_min === null ? "--" : fmt.dur(w.avg_duration_min)}`,
        w.open_now > 0 ? "warn" : "ok"));

    const body = $("#anBody");
    body.innerHTML = "";
    body.append(
      el("div", { class: "card" },
        el("div", { class: "card-h" }, el("b", {}, "告警最多的機台")),
        el("div", { class: "card-b flush" },
          ui.table([
            { label: "機台", cls: "mono", render: (r) => r[0] },
            { label: "告警數", cls: "num", render: (r) => String(r[1]) },
          ], Object.entries(a.by_machine ?? {}), { empty: "區間內沒有告警" }))),
      el("div", { class: "card" },
        el("div", { class: "card-h" }, el("b", {}, "分布")),
        el("div", { class: "card-b" },
          el("div", { class: "note", style: "margin-bottom:6px" }, "告警嚴重度"),
          ui.table([
            { label: "嚴重度", render: (r) => ui.severityBadge(r[0]) },
            { label: "數量", cls: "num", render: (r) => String(r[1]) },
          ], Object.entries(a.by_severity ?? {}), { empty: "無" }),
          el("div", { class: "note", style: "margin:12px 0 6px" }, "工單狀態"),
          ui.table([
            { label: "狀態", render: (r) => ui.stateBadge(r[0]) },
            { label: "數量", cls: "num", render: (r) => String(r[1]) },
          ], Object.entries(w.by_state ?? {}), { empty: "無" }))));

    if (siteId) {
      const { samples } = await api.get(`/analytics/kpi-trend?site_id=${encodeURIComponent(siteId)}&limit=300`);
      const card = el("div", { class: "card", style: "grid-column:1/-1" },
        el("div", { class: "card-h" }, el("b", {}, "KPI 歷史"),
          el("div", { class: "spacer" }),
          el("span", { class: "note" }, `${samples.length} 個取樣點・綠＝健康度　藍＝產出%`)),
        el("div", { class: "card-b" },
          samples.length > 1
            ? ui.sparkline([
                { values: samples.map((s) => s.factory_health), cls: "health" },
                { values: samples.map((s) => s.production_pct), cls: "prod" },
              ], { height: 160 })
            : el("div", { class: "empty" }, "取樣資料累積中")));
      body.append(card);
    }
  };
  startPolling(paint, 10000);
}

// ==================================================================== 頁面：稽核
async function viewAudit(main) {
  main.innerHTML = "";
  let actor = "", action = "";
  const actorInput = el("input", { type: "text", placeholder: "操作者帳號",
    onchange: (e) => { actor = e.target.value.trim(); paint(); } });
  const actionInput = el("input", { type: "text", placeholder: "動作（如 alarm.resolve）",
    onchange: (e) => { action = e.target.value.trim(); paint(); } });

  main.append(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, "操作稽核"),
        el("p", {}, "平台上所有具備影響的操作，皆記錄真實操作者身分與時間。"))),
    el("div", { class: "filters" }, actorInput, actionInput),
    el("div", { class: "card", id: "auditCard" }));

  const paint = async () => {
    const qs = new URLSearchParams({ limit: "300" });
    if (actor) qs.set("actor", actor);
    if (action) qs.set("action", action);
    const data = await api.get(`/audit?${qs}`);
    const card = $("#auditCard");
    card.innerHTML = "";
    card.append(ui.table([
      { label: "時間", cls: "mono", render: (r) => fmt.datetime(r.ts) },
      { label: "操作者", cls: "mono", key: "actor" },
      { label: "角色", cls: "mono", render: (r) => r.actor_role || "--" },
      { label: "動作", cls: "mono", key: "action" },
      { label: "站點", cls: "mono", render: (r) => r.site_id || "--" },
      { label: "對象", cls: "mono", render: (r) => r.target || "--" },
      { label: "結果", render: (r) => el("span", {
          class: `badge ${r.outcome === "ok" || r.outcome === "approved" ? "ok"
                  : r.outcome === "denied" || r.outcome === "rejected" ? "critical" : "idle"}` },
          r.outcome) },
      { label: "細節", cls: "mono", render: (r) => {
          const d = JSON.stringify(r.detail ?? {});
          return d === "{}" ? "--" : d.length > 60 ? d.slice(0, 60) + "…" : d;
        } },
    ], data.records, { empty: "沒有稽核記錄。" }));
    card.append(el("div", { class: "pager" }, `共 ${data.total} 筆（顯示最新 ${data.records.length} 筆）`));
  };
  startPolling(paint, 10000);
}

// ==================================================================== 頁面：管理
async function viewAdmin(main) {
  main.innerHTML = "";
  main.append(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, "系統管理"),
        el("p", {}, "使用者帳號與站點註冊。"))),
    el("div", { class: "card", style: "margin-bottom:12px" },
      el("div", { class: "card-h" }, el("b", {}, "使用者"),
        el("div", { class: "spacer" }),
        el("button", { class: "btn sm primary", onclick: () => openUserForm(paint) }, "新增使用者")),
      el("div", { class: "card-b flush", id: "userTable" })),
    el("div", { class: "card" },
      el("div", { class: "card-h" }, el("b", {}, "站點"),
        el("div", { class: "spacer" }),
        el("button", { class: "btn sm primary", onclick: () => openSiteForm(paint) }, "註冊站點")),
      el("div", { class: "card-b flush", id: "siteTable" })));

  const paint = async () => {
    const [users, sites] = await Promise.all([
      api.get("/admin/users"), api.get("/sites"),
    ]);
    $("#userTable").innerHTML = "";
    $("#userTable").append(ui.table([
      { label: "帳號", cls: "mono", key: "username" },
      { label: "顯示名稱", cls: "cjk", key: "display_name" },
      { label: "角色", render: (u) => el("span", { class: "badge idle" },
          users.role_labels[u.role] ?? u.role) },
      { label: "狀態", render: (u) => el("span", { class: `badge ${u.enabled ? "ok" : "idle"}` },
          u.enabled ? "啟用" : "停用") },
      { label: "上次登入", cls: "mono", render: (u) => fmt.datetime(u.last_login_at) },
      { label: "操作", render: (u) => el("div", { class: "btn-row" },
          el("button", { class: "btn sm", onclick: (e) => {
            e.stopPropagation(); openUserEdit(u, users.roles, users.role_labels, paint);
          } }, "編輯"),
          u.username === state.user.username ? null
            : el("button", { class: "btn sm", onclick: async (e) => {
                e.stopPropagation();
                if (!confirm(`確定刪除帳號 ${u.username}？`)) return;
                try { await api.del(`/admin/users/${u.username}`); ui.toast("已刪除", "ok"); paint(); }
                catch (err) { ui.toast(err.message, "err"); }
              } }, "刪除")) },
    ], users.users, { empty: "沒有使用者" }));

    $("#siteTable").innerHTML = "";
    $("#siteTable").append(ui.table([
      { label: "代號", cls: "mono", key: "site_id" },
      { label: "名稱", cls: "cjk", key: "name" },
      { label: "區域", cls: "cjk", render: (s) => s.region || "--" },
      { label: "資料來源", render: (s) => el("span", {
          class: `badge ${s.adapter_kind === "simulated" ? "sim" : "idle"}` }, s.adapter_kind) },
      { label: "啟用", render: (s) => el("span", { class: `badge ${s.enabled ? "ok" : "idle"}` },
          s.enabled ? "啟用" : "停用") },
      { label: "操作", render: (s) => el("button", { class: "btn sm", onclick: async (e) => {
          e.stopPropagation();
          if (!confirm(`確定移除站點 ${s.site_id}？其告警與工單記錄會保留。`)) return;
          try { await api.del(`/admin/sites/${s.site_id}`); ui.toast("站點已移除", "ok"); paint(); }
          catch (err) { ui.toast(err.message, "err"); }
        } }, "移除") },
    ], sites.sites, { empty: "沒有站點" }));
  };
  startPolling(paint, 15000);
}

function openUserForm(refresh) {
  const meta = state.meta ?? {};
  const username = el("input", { type: "text" });
  const display = el("input", { type: "text" });
  const password = el("input", { type: "password" });
  const role = el("select", {}, (meta.roles ?? []).map((r) =>
    el("option", { value: r.role }, `${r.label}（${r.role}）`)));
  ui.modal("新增使用者", el("div", {},
    el("label", { class: "field" }, el("span", {}, "帳號"), username),
    el("label", { class: "field" }, el("span", {}, "顯示名稱"), display),
    el("label", { class: "field" }, el("span", {}, "密碼（至少 8 字元）"), password),
    el("label", { class: "field" }, el("span", {}, "角色"), role)),
    [{ label: "取消" },
     { label: "建立", kind: "primary", onClick: async () => {
        try {
          await api.post("/admin/users", { username: username.value.trim(),
            display_name: display.value.trim(), password: password.value, role: role.value });
          ui.toast("使用者已建立", "ok"); refresh();
        } catch (e) { ui.toast(e.message, "err"); return false; }
      } }]);
}

function openUserEdit(user, roles, labels, refresh) {
  const role = el("select", {}, roles.map((r) =>
    el("option", { value: r, selected: r === user.role }, `${labels[r] ?? r}（${r}）`)));
  const enabled = el("select", {},
    el("option", { value: "true", selected: !!user.enabled }, "啟用"),
    el("option", { value: "false", selected: !user.enabled }, "停用"));
  const password = el("input", { type: "password", placeholder: "留空表示不變更" });
  const isSelf = user.username === state.user.username;
  ui.modal(`編輯 ${user.username}`, el("div", {},
    isSelf ? el("div", { class: "warnbar" }, "這是你自己的帳號：不能變更自己的角色或停用自己。") : null,
    el("label", { class: "field" }, el("span", {}, "角色"), role),
    el("label", { class: "field" }, el("span", {}, "狀態"), enabled),
    el("label", { class: "field" }, el("span", {}, "重設密碼"), password)),
    [{ label: "取消" },
     { label: "儲存", kind: "primary", onClick: async () => {
        const body = {};
        if (!isSelf) {
          if (role.value !== user.role) body.role = role.value;
          if ((enabled.value === "true") !== !!user.enabled) body.enabled = enabled.value === "true";
        }
        if (password.value) body.password = password.value;
        if (!Object.keys(body).length) { ui.toast("沒有變更", "info"); return true; }
        try { await api.patch(`/admin/users/${user.username}`, body);
              ui.toast("已更新", "ok"); refresh(); }
        catch (e) { ui.toast(e.message, "err"); return false; }
      } }]);
}

function openSiteForm(refresh) {
  const meta = state.meta ?? {};
  const siteId = el("input", { type: "text", placeholder: "如 TN-01" });
  const name = el("input", { type: "text", placeholder: "如 台南一廠" });
  const region = el("input", { type: "text", placeholder: "如 南區" });
  const kind = el("select", {}, (meta.adapter_kinds ?? []).map((a) =>
    el("option", { value: a.kind, disabled: !a.implemented },
      `${a.label}${a.implemented ? "" : "（尚未實作）"}`)));
  const seed = el("input", { type: "number", value: "20260809" });
  const scenario = el("select", {},
    el("option", { value: "" }, "（無：正常運轉）"),
    (meta.scenarios ?? []).map((s) => el("option", { value: s.scenario_id }, s.title)));

  ui.modal("註冊站點", el("div", {},
    el("div", { class: "grid g2" },
      el("label", { class: "field" }, el("span", {}, "站點代號"), siteId),
      el("label", { class: "field" }, el("span", {}, "站點名稱"), name)),
    el("div", { class: "grid g2" },
      el("label", { class: "field" }, el("span", {}, "區域"), region),
      el("label", { class: "field" }, el("span", {}, "資料來源"), kind)),
    el("div", { class: "grid g2" },
      el("label", { class: "field" }, el("span", {}, "模擬 seed"), seed),
      el("label", { class: "field" }, el("span", {}, "初始情境"), scenario)),
    el("div", { class: "note" },
      "真實資料源（OPC-UA / MQTT）需要現場點位表與憑證，目前僅保留介面。")),
    [{ label: "取消" },
     { label: "註冊", kind: "primary", onClick: async () => {
        if (!siteId.value.trim() || !name.value.trim()) {
          ui.toast("請填寫站點代號與名稱", "err"); return false;
        }
        try {
          await api.post("/admin/sites", {
            site_id: siteId.value.trim(), name: name.value.trim(), region: region.value.trim(),
            adapter_kind: kind.value,
            adapter_config: { seed: Number(seed.value) || 20260809,
                              scenario_id: scenario.value || null },
          });
          ui.toast("站點已註冊。請至站點清單啟動它。", "ok"); refresh();
        } catch (e) { ui.toast(e.message, "err"); return false; }
      } }]);
}

// ==================================================================== 路由
const ROUTES = [
  { pattern: /^#\/fleet$/, view: viewFleet, perm: "fleet:read" },
  { pattern: /^#\/sites$/, view: viewSites, perm: "site:read" },
  { pattern: /^#\/sites\/([^/]+)$/, view: viewSiteDetail, perm: "site:read" },
  { pattern: /^#\/alarms$/, view: viewAlarms, perm: "alarm:read" },
  { pattern: /^#\/work-orders$/, view: viewWorkOrders, perm: "workorder:read" },
  { pattern: /^#\/handovers$/, view: viewHandovers, perm: "site:read" },
  { pattern: /^#\/analytics$/, view: viewAnalytics, perm: "analytics:read" },
  { pattern: /^#\/audit$/, view: viewAudit, perm: "audit:read" },
  { pattern: /^#\/admin$/, view: viewAdmin, perm: "user:manage" },
];

async function route() {
  if (!state.user) return;
  const hash = location.hash || "#/fleet";
  if (!location.hash) { location.hash = "#/fleet"; return; }
  const main = $("#main");
  if (!main) return;

  stopPolling();
  renderNav();

  for (const r of ROUTES) {
    const match = hash.match(r.pattern);
    if (!match) continue;
    if (r.perm && !can(r.perm)) {
      main.innerHTML = "";
      main.append(el("div", { class: "card" },
        el("div", { class: "empty" }, "你的角色沒有檢視此頁面的權限。")));
      return;
    }
    try {
      await r.view(main, ...match.slice(1));
    } catch (err) {
      if (err.message === "unauthorized") return;
      main.innerHTML = "";
      main.append(el("div", { class: "card" }, el("div", { class: "empty" }, err.message)));
    }
    return;
  }
  location.hash = "#/fleet";
}

// ==================================================================== 啟動
async function boot() {
  try {
    state.meta = await api.get("/meta");
    state.user = state.meta.user;
  } catch (err) {
    if (err.message === "unauthorized") return;
    renderLogin(err.message);
    return;
  }
  renderShell();
  await route();
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", async () => {
  // 已有有效 cookie 就直接進主控台，沒有才顯示登入頁。
  try {
    const me = await api.get("/auth/me");
    state.user = me.user;
    await boot();
  } catch {
    renderLogin();
  }
});
