/* ==========================================================================
   AGENTGATE — 運作機制視角
   --------------------------------------------------------------------------
   一個自足的模組：掛上任何容器就能跑（獨立頁 /mechanism，或 Dashboard 的
   一個分頁）。所有東西關在 IIFE 裡，只對外露出 window.AgentGateMechanism —
   index.html 的 script 是頂層 const（esc / api / GATES …），共用同一個
   全域語彙表，重名會直接 SyntaxError 讓整頁掛掉。

   說明文字一律來自 GET /api/gate/mechanism，前端不自己寫一份敘述：
   通道信任表、必填欄位、規則條數、門檻金額都是後端從生效中的程式碼匯出的。
   這裡只負責畫，以及把探針送出去。

   探針是這個視角的重點：每一關都能當場送一筆真的動作請求走完整管線，
   結果用和裁決卡同一套走廊語言呈現。機制的證據是它跑起來的樣子。
   ========================================================================== */
window.AgentGateMechanism = (function(){
"use strict";

/* --- 基礎工具。刻意和 index.html 的同名函式保持一致的行為。 ------------- */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const ic = (name, cls) =>
  `<svg class="ic${cls ? " " + cls : ""}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const intFmt = n => Number(n).toLocaleString("en-US");

async function api(path, opts){
  const init = opts ? {headers:{"Content-Type":"application/json"}, ...opts} : undefined;
  const res = await fetch(path, init);
  const body = await res.json().catch(() => null);
  if (!res.ok){
    const err = new Error(
      body && typeof body.detail === "string" ? body.detail
      : body && body.detail ? JSON.stringify(body.detail)
      : `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.detail = body ? body.detail : null;
    throw err;
  }
  return body;
}

/* --- 詞彙。和 Dashboard 用同一組譯名，同一個東西不能在兩個畫面叫不同名字。 */
const RISK_LABEL = {low:"低", medium:"中", high:"高", forbidden:"禁止",
                    ungoverned:"未治理", unresolved:"未裁決"};
const STATUS_LABEL = {executed:"已執行", blocked:"已攔下",
                      pending_approval:"待人工核准", rejected:"已駁回"};
const STATUS_ICON = {executed:"check", blocked:"block",
                     pending_approval:"clock", rejected:"block"};
const TRUST_LABEL = {verified:"可信", derived:"衍生", untrusted:"不可信"};
const CH_LABEL = {user_verified:"已驗證用戶", user_unverified:"未驗證用戶",
                  tool_output:"工具回傳", memory:"記憶", system:"系統"};
const LLM_LABEL = {
  no:      {text:"無 LLM", cls:""},
  mapping: {text:"LLM 僅映射 · 輸出須過 schema", cls:""},
  never:   {text:"刻意不用 LLM", cls:"never"},
};

/* 走廊六格。和 index.html 的 GATES 同序同名 —— 同一道關卡在不同畫面
   必須長得一樣，不然它看起來就是兩套系統。 */
const RAIL = [["G0","來源信任"],["G1","動作解析"],["G2","政策裁決"],
              ["G3","後果預演"],["G4","人工核准"],["G5","執行封存"]];

/* --- 圖示。獨立頁沒有 index.html 的 sprite，缺了就自己補一份同 id 的。 --- */
const SPRITE = `
<symbol id="i-g0" viewBox="0 0 24 24"><path d="M10,7 H6.5 A4,4 0 0 0 6.5,15 H10"/><path d="M14,7 H17.5 A4,4 0 0 1 17.5,15 H14"/><path d="M8.5,11 H15.5"/></symbol>
<symbol id="i-g1" viewBox="0 0 24 24"><path d="M8,3 H4 V21 H8"/><path d="M16,3 H20 V21 H16"/><path d="M9,9 H15"/><path d="M9,12.5 H15"/><path d="M9,16 H12.5"/></symbol>
<symbol id="i-g2" viewBox="0 0 24 24"><path d="M12,3.5 V21"/><path d="M6.5,21 H17.5"/><path d="M4,7 H20"/><path d="M4,7 L1.8,13 H6.2 Z"/><path d="M20,7 L17.8,13 H22.2 Z"/></symbol>
<symbol id="i-g3" viewBox="0 0 24 24"><path d="M2.5,12 H7"/><path d="M7,12 L13.5,5.5"/><path d="M7,12 H13.5"/><path d="M7,12 L13.5,18.5"/><rect x="13.5" y="3" width="7" height="5"/><rect x="13.5" y="9.5" width="7" height="5"/><rect x="13.5" y="16" width="7" height="5"/></symbol>
<symbol id="i-g4" viewBox="0 0 24 24"><circle cx="12" cy="6" r="3.3" fill="currentColor" stroke="none"/><path d="M5.5,19.5 V18 A5,5 0 0 1 10.5,13 H13.5 A5,5 0 0 1 18.5,18 V19.5 Z" fill="currentColor" stroke="none"/><path d="M3,22.2 H21"/></symbol>
<symbol id="i-g5" viewBox="0 0 24 24"><path d="M6.5,21.5 H17.5"/><path d="M7.5,17.5 H16.5 V21 H7.5 Z"/><path d="M9.5,17.5 V13.5 A3.2,3.2 0 0 1 8.6,10.8 L10,3.5 H14 L15.4,10.8 A3.2,3.2 0 0 1 14.5,13.5 V17.5"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M4,12.5 L9.5,18 L20,6"/></symbol>
<symbol id="i-block" viewBox="0 0 24 24"><path d="M12,2.5 L20.5,6 V12 Q20.5,18 12,21.5 Q3.5,18 3.5,12 V6 Z"/><path d="M8,15.5 L16,8"/></symbol>
<symbol id="i-shield" viewBox="0 0 24 24"><path d="M12,2.5 L20.5,6 V12 Q20.5,18 12,21.5 Q3.5,18 3.5,12 V6 Z"/><path d="M8,12 L11,15 L16,9"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12,6.5 V12 L16,14.5"/></symbol>
<symbol id="i-warn" viewBox="0 0 24 24"><path d="M12,3 L22,20 H2 Z"/><path d="M12,9.5 V14.5"/><path d="M12,16.8 V17.6"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24"><path d="M6.5,3.5 L20,12 L6.5,20.5 Z" fill="currentColor" stroke="none"/></symbol>
<symbol id="i-policy" viewBox="0 0 24 24"><path d="M4,2.5 H20 V21.5 H4 Z"/><path d="M7.5,7 H16.5"/><path d="M7.5,11 H16.5"/><path d="M7.5,15 H13"/><path d="M7.5,18.5 H10.5"/></symbol>`;

function ensureSprite(){
  if (document.getElementById("i-g0")) return;   /* Dashboard 已經有了就用它的 */
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ic-sprite");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = SPRITE;
  document.body.insertBefore(svg, document.body.firstChild);
}

/* 走廊是按 G0…G5 排的，但管線不是按這個順序跑的：schema 驗證（G1）在最前面，
   來源信任（G0）之後才評估。所以「這一格通過了沒」要按**執行順序**判斷，
   不能按格子的左右位置 —— 不然一筆被 G1 擋下的畸形請求，畫面上會顯示
   G0 已通過，而 G0 根本還沒跑到。這一頁在講的就是這件事，自己不能畫錯。 */
const EXEC_ORDER = ["G1", "G0", "G2", "G3", "G4", "G5"];
const execIdx = id => EXEC_ORDER.indexOf(id);

/* ==========================================================================
   關卡走廊（結果用）
   六格的狀態由裁決推導，和 Dashboard 的裁決卡同一套規則：
   被擋 → 該格 STOP，尚未跑到的是「從未執行」而不是「失敗」；待核 → G4 HOLD。
   ========================================================================== */
function corridor(v){
  const stopExec = v.gate_blocked_at ? execIdx(v.gate_blocked_at) : -1;

  return `<div class="corridor" role="img" aria-label="${esc(corridorAlt(v))}">` +
    RAIL.map(([id, name], i) => {
      let cls = "pass", st = "PASS";
      if (stopExec >= 0){
        const e = execIdx(id);
        if (e < stopExec)       { cls = "pass"; st = "PASS"; }
        else if (e === stopExec){ cls = "stop"; st = "STOP"; }
        else                    { cls = "void"; st = "—";    }
      } else if (v.status === "pending_approval"){
        if (i < 4)       { cls = "pass"; st = "PASS"; }
        else if (i === 4){ cls = "hold"; st = "HOLD"; }
        else             { cls = "void"; st = "—";    }
      }
      return `<div class="gt ${cls}">
        <div class="gt-rail"><span class="gt-node"></span></div>
        <span class="gt-id">${id}</span>
        <svg class="ic" aria-hidden="true"><use href="#i-${id.toLowerCase()}"/></svg>
        <span class="gt-name">${name}</span>
        <span class="gt-st">${st}</span>
      </div>`;
    }).join("") + `</div>`;
}

function corridorAlt(v){
  if (v.gate_blocked_at) return `指令在 ${v.gate_blocked_at} 被攔下,之後的關卡未執行。`;
  if (v.status === "pending_approval") return "指令通過 G0 至 G3,停在 G4 等待人工核准。";
  return "指令通過全部六道關卡並完成封存。";
}

/* ==========================================================================
   狀態
   ========================================================================== */
let spec = null;          /* GET /api/gate/mechanism 的內容 */
let selected = "G0";
const results = {};       /* gate → 探針結果（換分頁回來時不重跑） */
let root = null;

const gateOf = id => spec.gates.find(g => g.gate === id);

/* ==========================================================================
   繪製
   ========================================================================== */
function render(){
  const g = gateOf(selected);
  root.innerHTML = `
  <div class="view-head">
    <div class="eyebrow">How the gate actually works</div>
    <h2>運作機制</h2>
    <p>一個 Agent 動作要通過<b>六道關卡</b>才會生效,任何一關都能否決,而<b>否決本身也是稽核事件</b>。
       點走廊上任一關,看它到底在算什麼、依據什麼資料、以及為什麼這樣設計 ——
       每一關都可以<b>當場送一筆真的動作請求</b>進去,結果就在同一張卡上。</p>
  </div>

  <div class="stack-lg">
    <div class="mech-principles">
      ${spec.principles.map((p, i) => `
        <div class="mech-principle">
          <span class="n">原則 ${i + 1}</span>
          <span class="t">${esc(p.title)}</span>
          <span class="b">${esc(p.body)}</span>
          <span class="r">${esc(p.ref)}</span>
        </div>`).join("")}
    </div>

    <div class="sec">
      <div class="sec-h">
        <b>${ic("policy")}六道關卡</b>
        <span class="meta">CLICK A GATE</span>
      </div>
      <div class="sec-b flush">
        <div class="mech-rail" role="tablist" aria-label="六道關卡">
          ${RAIL.map(([id, name]) => `
            <button class="mech-gt" role="tab" data-gate="${id}"
                    aria-selected="${id === selected}">
              <span class="rail"><span class="node"></span></span>
              <span class="id">${id}</span>
              <svg class="ic" aria-hidden="true"><use href="#i-${id.toLowerCase()}"/></svg>
              <span class="nm">${name}</span>
            </button>`).join("")}
        </div>
        <div class="mech-order">
          <span class="micro" style="margin-right:.3rem">實際執行順序</span>
          ${spec.flow.order.map((s, i) =>
            `${i ? `<span class="arr">→</span>` : ""}
             <span class="st ${s === "G0-R1" ? "hi" : ""}">${esc(s)}</span>`).join("")}
        </div>
        <div class="mech-order-b">
          <b>${esc(spec.flow.title)}。</b>${esc(spec.flow.body)}
          <span class="mono dimmer" style="font-size:var(--t-micro)"> ${esc(spec.flow.ref)}</span>
        </div>
      </div>
    </div>

    <div class="sec" id="mechCard">${gateCard(g)}</div>

    <div class="foot">
      <b>關於這一頁</b>
      <div class="mt6">說明由 <code>GET /api/gate/mechanism</code> 從生效中的程式碼匯出
        —— 通道信任表、必填欄位、規則條數與門檻金額都不是前端寫死的。
        ${esc(spec.probe_note)}</div>
    </div>
  </div>`;

  root.querySelectorAll(".mech-gt").forEach(btn => {
    btn.addEventListener("click", () => select(btn.dataset.gate));
    btn.addEventListener("keydown", e => {
      const i = RAIL.findIndex(x => x[0] === btn.dataset.gate);
      if (e.key === "ArrowRight" || e.key === "ArrowLeft"){
        e.preventDefault();
        const next = RAIL[(i + (e.key === "ArrowRight" ? 1 : RAIL.length - 1)) % RAIL.length][0];
        select(next);
        root.querySelector(`.mech-gt[data-gate="${next}"]`)?.focus();
      }
    });
  });
  bindCard();
}

function select(id){
  if (id === selected) return;
  selected = id;
  root.querySelectorAll(".mech-gt").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.gate === id)));
  document.getElementById("mechCard").innerHTML = gateCard(gateOf(id));
  bindCard();
}

function gateCard(g){
  const llm = LLM_LABEL[g.llm] ?? LLM_LABEL.no;
  return `
  <div class="mech-card-h">
    <span class="g">${esc(g.gate)}</span>
    <h3>${esc(g.name)}</h3>
    <span class="en">${esc(g.en)}</span>
    <span class="mech-llm ${llm.cls}">${esc(llm.text)}</span>
    <span class="mod">${esc(g.module)}</span>
  </div>
  <p class="mech-lede">${esc(g.one_liner)}</p>

  <div class="mech-io">
    <span class="k">輸入</span><span class="ty">${esc(g.input)}</span>
    <span class="arr">→</span>
    <span class="k">輸出</span><span class="ty">${esc(g.output)}</span>
  </div>

  <ul class="mech-does">
    ${g.does.map(d => `<li>${bold(d)}</li>`).join("")}
  </ul>

  <div class="mech-decision">
    <div class="t">關鍵設計決定</div>
    <div class="h">${esc(g.decision.title)}</div>
    <div class="b">${esc(g.decision.body)}</div>
  </div>

  ${reference(g.reference)}
  ${probe(g)}`;
}

/* 說明句子裡的 **粗體** 是後端標的重點詞，不是 Markdown 全解析 —— 只做這一種。 */
function bold(s){
  return esc(s).replace(/\*\*(.+?)\*\*/g, (_, t) => `<b>${t}</b>`);
}

/* ==========================================================================
   「依據」——這一關實際吃的資料表
   ========================================================================== */
function reference(ref){
  if (!ref) return "";
  const wrap = (title, sub, inner) => `
    <div class="mech-ref">
      <div class="hd"><span class="t">依據 · ${esc(title)}</span><span class="s">${esc(sub)}</span></div>
      <div class="tbl">${inner}</div>
    </div>`;

  if (ref.kind === "trust_table"){
    return wrap("通道信任分級", "來源鏈中最低的那一列決定整鏈上限", `<table>
      <thead><tr><th>指令通道</th><th>信任等級</th><th>可授權上限</th><th>說明</th></tr></thead>
      <tbody>${ref.rows.map(r => `
        <tr class="${r.trust === "untrusted" ? "untrusted" : ""}">
          <td class="mono">${esc(r.channel)}</td>
          <td>${esc(TRUST_LABEL[r.trust] ?? r.trust)}</td>
          <td class="mono">${esc(RISK_LABEL[r.max_risk] ?? r.max_risk)}</td>
          <td class="note">${esc(r.note)}</td>
        </tr>`).join("")}</tbody></table>`);
  }

  if (ref.kind === "schema_table"){
    return wrap("動作參數 schema", "畸形輸入在邊界就回絕,不進政策層", `<table>
      <thead><tr><th>動作</th><th>必填參數</th><th>額外驗證</th></tr></thead>
      <tbody>${ref.rows.map(r => `
        <tr>
          <td class="mono">${esc(r.action)}</td>
          <td class="mono">${r.required.length ? esc(r.required.join(", ")) : "—"}</td>
          <td class="note">${esc(r.extra || "—")}</td>
        </tr>`).join("")}</tbody></table>`);
  }

  if (ref.kind === "policy_ref"){
    return `
    <div class="mech-ref">
      <div class="hd"><span class="t">依據 · 政策條文</span>
        <span class="s">${ref.actions} 動作 × ${ref.rules} 條規則</span></div>
      <div class="sec-b flush">
        <div class="facts">
          ${Object.entries(ref.limits).map(([k, v]) => `
            <div class="fact">
              <span class="macro v" style="font-size:1.05rem">${esc(v.split("(")[0])}</span>
              <span class="k">${esc(k)}</span>
              <span class="s">${esc(v.includes("(") ? "(" + v.split("(")[1] : "")}</span>
            </div>`).join("")}
        </div>
      </div>
      <div class="mech-hint">${esc(ref.hint)}</div>
    </div>`;
  }

  if (ref.kind === "projection_fields"){
    return wrap("預演產出欄位", "每一欄都是影子環境實測的,不是申報的", `<table>
      <thead><tr><th>欄位</th><th>意義</th></tr></thead>
      <tbody>${ref.rows.map(r => `
        <tr><td class="mono">${esc(r.field)}</td><td class="note">${esc(r.meaning)}</td></tr>`
      ).join("")}</tbody></table>`);
  }

  if (ref.kind === "evidence_sections"){
    return wrap("證據包結構", "主管在 15 秒內要看到的東西", `<table>
      <thead><tr><th>區塊</th><th>來自</th><th>回答什麼</th></tr></thead>
      <tbody>${ref.rows.map(r => `
        <tr><td>${esc(r.section)}</td><td class="mono">${esc(r.from)}</td>
        <td class="note">${esc(r.why)}</td></tr>`).join("")}</tbody></table>`);
  }

  if (ref.kind === "chain_stages"){
    return wrap("稽核鏈階段", "被擋下的動作一樣入鏈", `<table>
      <thead><tr><th>stage</th><th>何時寫入</th><th>寫什麼</th></tr></thead>
      <tbody>${ref.rows.map(r => `
        <tr><td class="mono">${esc(r.stage)}</td><td>${esc(r.when)}</td>
        <td class="note">${esc(r.what)}</td></tr>`).join("")}</tbody></table>`);
  }
  return "";
}

/* ==========================================================================
   現場實證
   ========================================================================== */
function probe(g){
  const p = g.probe;
  const expectPill = p.expect === "executed" ? `<span class="pill ok">預期:放行</span>`
    : p.expect === "pending_approval" ? `<span class="pill wait">預期:停在人工核准</span>`
    : `<span class="pill blocked">預期:攔在 ${esc(p.expect_gate ?? g.gate)}</span>`;

  return `
  <div class="mech-probe">
    <div class="hd">
      <span class="t">現場實證</span>
      <button class="primary" data-probe="${esc(g.gate)}">${ic("play")}${esc(p.label)}</button>
      ${expectPill}
    </div>
    <div class="note">${esc(p.note)}</div>
    <details class="payload">
      <summary>看這一筆送出去的動作請求</summary>
      <pre>${esc(JSON.stringify(p.payload, null, 2))}</pre>
    </details>
    <div id="probeOut">${results[g.gate] ? outcome(results[g.gate]) : ""}</div>
  </div>`;
}

function bindCard(){
  const btn = root.querySelector("[data-probe]");
  btn?.addEventListener("click", () => runProbe(btn.dataset.probe));
}

async function runProbe(gateId){
  const g = gateOf(gateId);
  const out = document.getElementById("probeOut");
  const btn = root.querySelector("[data-probe]");
  if (btn) btn.disabled = true;
  out.innerHTML = `<div class="loading"><div class="t">EVALUATING</div>
    <div class="b"><i></i></div></div>`;

  let v;
  try {
    v = await api("/api/gate/evaluate", {method:"POST", body: JSON.stringify(g.probe.payload)});
  } catch(e){
    /* G1 的攔截發生在 API 邊界（HTTP 422），沒有 GateVerdict 可以回。
       它仍然是一次真的攔截，所以照樣畫成走廊，而不是一個紅色錯誤框 ——
       「畸形輸入被擋在 G1」是機制的一部分，不是這一頁壞了。 */
    if (e.status === 422 && e.detail && e.detail.gate){
      v = {
        status: "blocked", gate_blocked_at: e.detail.gate, risk: "unresolved",
        reasons: e.detail.errors ?? [], findings: [], boundary: true,
      };
    } else {
      out.innerHTML = `<div class="errbox"><div class="t">${ic("warn")}探針執行失敗</div>
        <div class="d"><code>${esc(e.message)}</code></div></div>`;
      if (btn) btn.disabled = false;
      return;
    }
  }

  results[gateId] = v;
  out.innerHTML = outcome(v);
  if (btn) btn.disabled = false;
}

function outcome(v){
  const st = v.status;
  const reasons = (v.reasons ?? []).map(r =>
    `<li class="${/^\[[A-Z]{2}-\d+\]/.test(r) ? "rule" : ""}">${esc(r)}</li>`).join("");

  const findings = (v.findings ?? []).map(f => `
    <div class="rulecard">
      <span class="rid">${esc(f.rule_id)} · ${esc(f.title)}</span>
      <div class="msg">${esc(f.message)}</div>
      ${f.statute ? `<span class="statute">「${esc(f.statute)}」</span>` : ""}
    </div>`).join("");

  const p = v.projection;
  const facts = p ? `
    <div class="facts tight mt10">
      <div class="fact ${p.affected_count > 1 ? "warn" : ""}">
        <span class="macro v">${intFmt(p.affected_count)}</span>
        <span class="k">受影響主體</span></div>
      <div class="fact ${p.reversible ? "good" : "bad"}">
        <span class="macro v">${p.reversible ? "可回復" : "不可回復"}</span>
        <span class="k">回復性</span></div>
      <div class="fact ${p.financial_delta < 0 ? "bad" : ""}">
        <span class="macro v">${p.financial_delta
          ? (p.financial_delta < 0 ? "−" : "+") + intFmt(Math.abs(p.financial_delta)) : "0"}</span>
        <span class="k">金流影響(元)</span></div>
      <div class="fact ${(p.pii_fields_exposed ?? []).length ? "bad" : "good"}">
        <span class="macro v">${(p.pii_fields_exposed ?? []).length || "無"}</span>
        <span class="k">觸及個資欄位</span></div>
    </div>` : "";

  const chain = v.trust && (v.trust.chain ?? []).length > 1 ? (() => {
    const weakRef = v.trust.weakest_link ? v.trust.weakest_link.source_ref : null;
    return `<div class="chain mt10">${v.trust.chain.map((n, i) => `
      ${i ? `<span class="arr">→</span>` : ""}
      <div class="lnk ${n.source_ref === weakRef ? "weak" : ""}">
        <span class="ch">${esc(CH_LABEL[n.channel] ?? n.channel)}</span>
        <span class="tr">${esc(TRUST_LABEL[n.trust] ?? n.trust)} · 上限 ${esc(RISK_LABEL[n.max_risk] ?? n.max_risk)}</span>
        <span class="ref">${esc(n.source_ref)}</span>
      </div>`).join("")}</div>`;
  })() : "";

  return `
  <div class="mech-out">
    <div class="hd">
      <span class="big ${esc(st)}">${ic(STATUS_ICON[st] ?? "warn")}${STATUS_LABEL[st] ?? esc(st)}</span>
      <span class="pill risk ${esc(v.risk)}">風險 ${esc(RISK_LABEL[v.risk] ?? v.risk)}</span>
      ${v.gate_blocked_at ? `<span class="pill blocked">攔截於 ${esc(v.gate_blocked_at)}</span>` : ""}
      ${v.approval_id ? `<span class="pill wait">${esc(v.approval_id)}</span>` : ""}
      ${v.boundary ? `<span class="pill info">HTTP 422 · API 邊界</span>` : ""}
      <span class="lat">${v.decision_latency_ms != null
        ? `裁決耗時 ${v.decision_latency_ms.toFixed(2)} ms` : ""}</span>
    </div>
    ${st === "blocked" ? `<div class="hazard"></div>` : ""}
    ${st === "pending_approval" ? `<div class="hazard wait"></div>` : ""}
    <div class="sec-b flush">${corridor(v)}</div>
    <div class="bd">
      <div class="micro">裁決理由</div>
      <ul class="reasons mt6">${reasons || "<li>(無)</li>"}</ul>
      ${findings ? `<div class="micro mt10">觸發規則與條文</div><div class="mt6">${findings}</div>` : ""}
      ${chain}
      ${facts}
      ${v.audit_ref ? `<div class="micro mt10">已封存 · 稽核雜湊
        <span class="hash"><span class="cur">${esc(v.audit_ref.slice(0, 16))}…</span></span></div>` : ""}
    </div>
  </div>`;
}

/* ==========================================================================
   掛載
   ========================================================================== */
async function mount(el){
  root = typeof el === "string" ? document.querySelector(el) : el;
  if (!root) return;
  ensureSprite();

  if (spec){ render(); return; }        /* 換分頁回來不重抓 */
  root.innerHTML = `<div class="loading"><div class="t">LOADING MECHANISM</div>
    <div class="b"><i></i></div></div>`;
  try {
    spec = await api("/api/gate/mechanism");
  } catch(e){
    root.innerHTML = `<div class="errbox"><div class="t">
      <svg class="ic" aria-hidden="true"><use href="#i-warn"/></svg>機制說明載入失敗</div>
      <div class="d"><code>${esc(e.message)}</code></div></div>`;
    return;
  }
  render();
}

return {mount};
})();
