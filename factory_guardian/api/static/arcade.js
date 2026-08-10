/* ==========================================================================
   ARCADE VIEW — 2D 像素工廠模擬
   --------------------------------------------------------------------------
   把同一份 /api/state 畫成一座橫向捲軸遊戲裡的工廠：機台是像素設備、
   輸送帶上跑箱子、地上有作業員走來走去、維修技師會跑到故障機台旁邊敲。

   刻意的取捨：

   · 所有圖形都用 fillRect 程序化畫出來，不載任何圖檔或字型。
     競賽場地的網路不能賭，離線打開也要一模一樣。
   · 內部座標是一套虛擬像素（vw × vh），畫的時候乘上 px 再四捨五入到
     整數裝置像素。這樣面板寬度不管多少，色塊邊緣都不會糊。
   · 動畫吃 prefers-reduced-motion：關掉動態時只畫一張靜止畫面，不開 rAF。
   · 資料驅動，沒有一個數字是寫死的：
       輸送帶速度 = 上游機台當下 U/HR ÷ 額定
       HP 條      = health
       燈號／濃煙／火花 = worst_band 與 state
       技師出現   = 有機台在 maintenance
       危險區人員 = camera.person_in_hazard_zone
   ========================================================================== */
(function () {
"use strict";

/* ------------------------------------------------------------------ 點陣字
   3×5 的字模。每個字五列，每列用一個八進位數字表示三個位元（4=左 2=中 1=右）。
   例如 A = 010 / 101 / 111 / 101 / 101 → "25755"。 */
const GLYPH = {
  A:"25755", B:"65656", C:"34443", D:"65556", E:"74647", F:"74644", G:"34553",
  H:"55755", I:"72227", J:"11152", K:"55655", L:"44447", M:"57755", N:"57775",
  O:"25552", P:"65644", Q:"25573", R:"65655", S:"34216", T:"72222", U:"55557",
  V:"55552", W:"55775", X:"55255", Y:"55222", Z:"71247",
  "0":"75557","1":"26227","2":"61247","3":"61616","4":"55711","5":"74616",
  "6":"34757","7":"71222","8":"75757","9":"75716",
  " ":"00000","-":"00700","/":"11244",".":"00002",":":"02020","%":"51245",
  "!":"22202","+":"02720","(":"12441",")":"42114","x":"00525",
};
const GW = 4;   // 每字佔的寬度（3 點 + 1 空隙）
const GH = 5;

/* --------------------------------------------------------------- NES 調色盤 */
const C = {
  ink:"#101018", white:"#FFFFFF",
  sky:"#5C94FC", skyHi:"#8CB4FC", cloud:"#FFFFFF", cloudSh:"#B8D4FC",
  hill:"#00A844", hillHi:"#58D854",
  brick:"#C84C0C", brickHi:"#E88030", brickLo:"#7C2A08", mortar:"#3A1404",
  steel:"#8C8C9C", steelHi:"#C8C8D8", steelLo:"#4C4C5C",
  belt:"#32323C", beltHi:"#5A5A6A", roller:"#A0A0B0",
  crate:"#E39B2A", crateHi:"#F7C463", crateLo:"#8C5410",
  boxDone:"#38B8F8", boxDoneHi:"#7CD8FF", boxDoneLo:"#1858A8",
  lampOk:"#58D854", lampWarn:"#FCD800", lampBad:"#F83800", lampOff:"#3A3A46",
  hpOk:"#58D854", hpMid:"#FCD800", hpBad:"#F83800", hpBg:"#282830",
  skin:"#FCB08C", cap:"#E43B44", suit:"#2038EC", vest:"#FF7A1A",
  helmet:"#FCD800", tool:"#C8C8D8",
  smoke:"#B0B0C0", smokeBad:"#6C6C7C", spark:"#FCD800",
  hud:"#000000", hudDim:"#9C9CB4", hazard:"#F83800", hazardAlt:"#FCD800",
  accentMach:"#2E6BE6", accentPack:"#F08000",
};

/* ------------------------------------------------------------------ 版面常數
   精靈本身固定 40×26 個虛擬像素，場景的寬鬆度靠 cellW / siloW / truckW 調。
   把場景放寬等於把整體縮放拉小：面板寬度固定時，虛擬像素越多、每一格越小，
   畫面才不會像貼著螢幕看一台機器。目標比例約 2.8:1。 */
const L = {
  hudH:11, topPad:16, cellW:110, sprW:40, sprH:26, lblH:5, hpH:4,
  platH:3, gapY:14, groundH:18, siloW:64, truckW:84, crateGap:18,
  // 最下層機台與地面之間留一條走道，人才不會跟機台疊在一起；
  // 上方留白則是給煙囪的煙與警示氣泡用的，不留就會被 HUD 切掉。
  floorGap:17,
};
const cellH = L.lblH + 1 + L.hpH + 1 + L.sprH + L.platH;   // = 40

const clamp01 = v => Math.max(0, Math.min(1, v));
const pad = (n, w) => String(Math.max(0, Math.round(n))).padStart(w, "0");

/* ====================================================================== */
const A = {
  host:null, cv:null, ctx:null, px:3, vw:0, vh:0,
  cells:{}, stages:[], belts:[], silo:null, truck:null, ground:0,
  kind:{}, rated:{}, snap:null, live:null,
  t:0, last:0, raf:0, active:false, ready:false, ui:null,
  textCache:new Map(), ro:null, reduce:false,
};

/* --------------------------------------------------------- 低階繪圖工具 */
const R = v => Math.round(v * A.px);
function rect(x, y, w, h, col){
  const x0 = R(x), y0 = R(y);
  A.ctx.fillStyle = col;
  A.ctx.fillRect(x0, y0, R(x + w) - x0, R(y + h) - y0);
}
/* 外框：先畫一圈墨色再填色，NES 精靈的輪廓感就是這樣來的 */
function box(x, y, w, h, col){
  rect(x - 1, y - 1, w + 2, h + 2, C.ink);
  rect(x, y, w, h, col);
}

/* 文字先畫進離螢幕畫布再貼上。整場景每幀有上百個字，
   逐點 fillRect 會讓 CPU 白白燒在同樣的字串上。 */
function textW(str, s){ return (str.length * GW - 1) * s; }
function text(str, x, y, col, s){
  s = s || 1;
  const key = str + "|" + s + "|" + col + "|" + A.px;
  let cv = A.textCache.get(key);
  if(!cv){
    const w = Math.max(1, R(textW(str, s))), h = Math.max(1, R(GH * s));
    cv = document.createElement("canvas");
    cv.width = w; cv.height = h;
    const c = cv.getContext("2d");
    c.fillStyle = col;
    for(let i = 0; i < str.length; i++){
      const g = GLYPH[str[i]] || GLYPH[str[i].toUpperCase()] || GLYPH[" "];
      for(let r = 0; r < GH; r++){
        const bits = parseInt(g[r], 8);
        for(let b = 0; b < 3; b++){
          if(!(bits & (4 >> b))) continue;
          const px0 = R((i * GW + b) * s), py0 = R(r * s);
          c.fillRect(px0, py0, R((i * GW + b + 1) * s) - px0, R((r + 1) * s) - py0);
        }
      }
    }
    if(A.textCache.size > 160) A.textCache.clear();
    A.textCache.set(key, cv);
  }
  A.ctx.drawImage(cv, R(x), R(y));
}
function textR(str, xRight, y, col, s){ text(str, xRight - textW(str, s || 1), y, col, s); }

/* ====================================================================== 版面 */
function layout(topo){
  const line = ((topo || {}).lines || [])[0];
  if(!line || !line.stages.length) return false;

  A.kind = {}; A.rated = {};
  (topo.machines || []).forEach(m => {
    A.kind[m.machine_id] = m.kind;
    A.rated[m.machine_id] = m.rated_rate_uph || 1;
  });

  A.stages = line.stages;
  const maxN = Math.max(...A.stages.map(s => s.length));
  const rowsTop = L.hudH + L.topPad;
  const maxRows = maxN * cellH + (maxN - 1) * L.gapY;

  A.vw = L.siloW + A.stages.length * L.cellW + L.truckW;
  A.vh = rowsTop + maxRows + L.floorGap + L.groundH;
  A.ground = A.vh - L.groundH;

  A.cells = {};
  A.stages.forEach((stage, si) => {
    const n = stage.length;
    const rows = n * cellH + (n - 1) * L.gapY;
    const top = rowsTop + (maxRows - rows) / 2;
    stage.forEach((mid, i) => {
      const cellTop = top + i * (cellH + L.gapY);
      const sx = L.siloW + si * L.cellW + (L.cellW - L.sprW) / 2;
      const sy = cellTop + L.lblH + 1 + L.hpH + 1;
      A.cells[mid] = {
        cx:L.siloW + si * L.cellW, cellTop, x:sx, y:sy,
        w:L.sprW, h:L.sprH, mid:sy + L.sprH / 2,
        plat:sy + L.sprH, si,
        // 支柱要落在哪裡：最下層那台落到地面，上層那台只撐到下一台的頭頂，
        // 不然柱子會直接穿過下面那台機器的標籤。
        foot:(i === n - 1) ? A.ground : top + (i + 1) * (cellH + L.gapY) - 2,
      };
    });
  });

  const midY = rowsTop + maxRows / 2;
  A.silo  = {x:6, y:A.ground - 50, w:L.siloW - 20, h:50,
             out:{x:6 + L.siloW - 20, y:A.ground - 36}};
  A.truck = {x:A.vw - L.truckW + 8, y:A.ground - 30, w:L.truckW - 16, h:30,
             in:{x:A.vw - L.truckW + 8, y:A.ground - 22}};

  // 輸送帶：進料 → 首階段、階段之間、末階段 → 出貨車
  A.belts = [];
  const addBelt = (from, x1, y1, x2, y2, cargo) => {
    const pts = (y1 === y2) ? [[x1, y1], [x2, y2]]
      : [[x1, y1], [(x1 + x2) / 2, y1], [(x1 + x2) / 2, y2], [x2, y2]];
    const segs = []; let len = 0;
    for(let i = 0; i < pts.length - 1; i++){
      const d = Math.abs(pts[i + 1][0] - pts[i][0]) + Math.abs(pts[i + 1][1] - pts[i][1]);
      segs.push({a:pts[i], b:pts[i + 1], d, at:len}); len += d;
    }
    A.belts.push({from, pts, segs, len, ph:0, cargo});
  };
  A.stages[0].forEach(mid => {
    const c = A.cells[mid];
    addBelt(mid, A.silo.out.x, A.silo.out.y, c.x, c.mid, "raw");
  });
  for(let i = 0; i < A.stages.length - 1; i++)
    A.stages[i].forEach(src => A.stages[i + 1].forEach(dst => {
      const a = A.cells[src], b = A.cells[dst];
      addBelt(src, a.x + a.w, a.mid, b.x, b.mid, "raw");
    }));
  A.stages[A.stages.length - 1].forEach(src => {
    const a = A.cells[src];
    addBelt(src, a.x + a.w, a.mid, A.truck.in.x, A.truck.in.y, "done");
  });

  // 走道上的作業員。速度與起點各自不同，看起來才像兩個人而不是同一個人複製。
  A.crew = [
    {x:L.siloW + 10, dir:1,  sp:15, cap:C.cap,   frame:0, ft:0, wait:0},
    {x:A.vw - L.truckW - 30, dir:-1, sp:11, cap:C.boxDone, frame:0, ft:0, wait:1.5},
  ];
  A.midY = midY;
  return true;
}

function pointAt(belt, d){
  d = Math.max(0, Math.min(belt.len, d));
  for(const s of belt.segs){
    if(d <= s.at + s.d || s === belt.segs[belt.segs.length - 1]){
      const k = s.d ? (d - s.at) / s.d : 0;
      return [s.a[0] + (s.b[0] - s.a[0]) * k, s.a[1] + (s.b[1] - s.a[1]) * k];
    }
  }
  return belt.pts[belt.pts.length - 1];
}

/* ====================================================================== 尺寸 */
function resize(){
  if(!A.host) return;
  const cssW = A.host.clientWidth || 640;
  const dpr = window.devicePixelRatio || 1;
  A.px = (cssW * dpr) / A.vw;
  A.cv.width  = Math.round(A.vw * A.px);
  A.cv.height = Math.round(A.vh * A.px);
  A.cv.style.width = "100%";
  A.cv.style.height = "auto";
  A.textCache.clear();
  draw();
}

/* ====================================================================== 場景 */
function drawSky(){
  rect(0, 0, A.vw, A.ground, C.sky);
  rect(0, A.ground - 26, A.vw, 26, C.skyHi);
  // 雲：慢慢往左飄，出界就繞回來
  const drift = (A.t * 3) % (A.vw + 40);
  [[0, 16, 1], [78, 24, 0.8], [150, 13, 1.1]].forEach(([bx, by, sc], i) => {
    let x = bx - drift * sc; while(x < -34) x += A.vw + 40;
    cloud(x, by);
  });
  // 遠景綠丘
  for(let i = 0; i < 3; i++){
    const bx = 18 + i * 74, by = A.ground - 14;
    rect(bx, by, 34, 14, C.hill);
    rect(bx + 6, by - 5, 22, 5, C.hill);
    rect(bx + 12, by - 8, 10, 3, C.hill);
    rect(bx + 8, by - 4, 6, 2, C.hillHi);
  }
}
function cloud(x, y){
  rect(x + 4, y, 16, 4, C.cloud);
  rect(x, y + 4, 24, 5, C.cloud);
  rect(x + 8, y - 3, 8, 3, C.cloud);
  rect(x, y + 7, 24, 2, C.cloudSh);
}
function drawGround(){
  rect(0, A.ground, A.vw, L.groundH, C.brick);
  rect(0, A.ground, A.vw, 2, C.brickHi);
  for(let x = 0; x < A.vw; x += 8){
    rect(x, A.ground + 2, 1, L.groundH - 2, C.mortar);
    rect(x, A.ground + 7, 8, 1, C.mortar);
    rect(x + 4, A.ground + 8, 1, 6, C.mortar);
  }
  rect(0, A.ground + L.groundH - 2, A.vw, 2, C.brickLo);
}

/* 機台底下的鋼構平台；懸空的還要補兩根柱子撐到地面 */
function drawPlatform(c){
  const px0 = c.x - 4, w = c.w + 8;
  box(px0, c.plat, w, L.platH, C.steel);
  rect(px0, c.plat, w, 1, C.steelHi);
  for(let i = 2; i < w - 2; i += 6) rect(px0 + i, c.plat + 1, 1, 1, C.steelLo);
  const legH = c.foot - c.plat - L.platH;
  if(legH > 4){
    [px0 + 3, px0 + w - 5].forEach(x => {
      rect(x, c.plat + L.platH, 2, legH, C.steelLo);
      rect(x, c.plat + L.platH, 1, legH, C.steel);
    });
  }
}

function drawBelt(b, m){
  const rate = m ? (m.online === false ? 0 : m.production_rate_uph || 0) : 0;
  const ratio = clamp01(rate / (A.rated[b.from] || 1));
  const off = b.ph;
  b.segs.forEach(s => {
    const horiz = s.a[1] === s.b[1];
    const x0 = Math.min(s.a[0], s.b[0]), y0 = Math.min(s.a[1], s.b[1]);
    // 滾輪是「跑動的短刻痕」，位移取 off 的餘數再逐格前進，
    // 兩端各留 1 格避免刻痕壓到轉角的外框。
    const o = ((off % 6) + 6) % 6;
    if(horiz){
      box(x0, y0 - 3, s.d, 6, C.belt);
      rect(x0, y0 - 3, s.d, 1, C.beltHi);
      for(let i = -6; i < s.d; i += 6){
        const rx = x0 + i + o;
        if(rx >= x0 + 1 && rx <= x0 + s.d - 3) rect(rx, y0 - 1, 2, 2, ratio ? C.roller : C.beltHi);
      }
    }else{
      box(x0 - 3, y0, 6, s.d, C.belt);
      rect(x0 - 3, y0, 1, s.d, C.beltHi);
      for(let i = -6; i < s.d; i += 6){
        const ry = y0 + i + o;
        if(ry >= y0 + 1 && ry <= y0 + s.d - 3) rect(x0 - 1, ry, 2, 2, ratio ? C.roller : C.beltHi);
      }
    }
  });
  for(let d = b.ph % L.crateGap; d < b.len; d += L.crateGap){
    const [x, y] = pointAt(b, d);
    crate(x - 3, y - 9, b.cargo);
  }
}
function crate(x, y, cargo){
  const base = cargo === "done" ? C.boxDone : C.crate;
  const hi   = cargo === "done" ? C.boxDoneHi : C.crateHi;
  const lo   = cargo === "done" ? C.boxDoneLo : C.crateLo;
  box(x, y, 6, 6, base);
  rect(x, y, 6, 1, hi);
  rect(x, y + 5, 6, 1, lo);
  rect(x + 2, y + 2, 2, 2, lo);
}

function drawSilo(orders){
  const s = A.silo;
  box(s.x, s.y, s.w, s.h - 12, C.steel);
  rect(s.x, s.y, s.w, 2, C.steelHi);
  rect(s.x + 2, s.y + 6, s.w - 4, 12, C.steelLo);
  // 料位：未完成訂單越多，槽裡的料越滿
  const fill = Math.min(10, 2 + (orders || 0));
  rect(s.x + 3, s.y + 17 - fill, s.w - 6, fill, C.crate);
  rect(s.x + 3, s.y + 17 - fill, s.w - 6, 1, C.crateHi);
  // 漏斗
  for(let i = 0; i < 6; i++) rect(s.x + 4 + i, s.y + s.h - 12 + i, s.w - 8 - i * 2, 1, C.steelLo);
  box(s.x + 10, s.y + s.h - 6, 10, 6, C.steel);
  rect(s.x + 2, A.ground - 4, s.w - 4, 4, C.steelLo);
  text("MATERIAL", s.x - 2, s.y - 15, C.white, 1);
  text(pad(orders, 2) + " ORDERS", s.x - 2, s.y - 8, C.crateHi, 1);
}

function drawTruck(shipped){
  const t = A.truck;
  // 貨櫃
  box(t.x, t.y, t.w - 14, t.h - 6, C.steelLo);
  rect(t.x + 1, t.y + 1, t.w - 16, 3, C.steel);
  for(let i = 2; i < t.w - 16; i += 5) rect(t.x + i, t.y + 4, 1, t.h - 12, C.steel);
  // 車頭
  box(t.x + t.w - 14, t.y + 6, 14, t.h - 12, C.cap);
  rect(t.x + t.w - 12, t.y + 8, 6, 5, C.boxDoneHi);
  rect(t.x + t.w - 14, t.y + 6, 14, 1, "#F87878");
  // 輪子
  [t.x + 4, t.x + 14, t.x + t.w - 8].forEach(x => {
    box(x, A.ground - 7, 7, 7, C.ink);
    rect(x + 2, A.ground - 5, 3, 3, C.steelLo);
  });
  text("SHIPPED", t.x - 2, t.y - 14, C.white, 1);
  text(pad(shipped, 4), t.x + 4, t.y - 8, C.boxDoneHi, 1);
}

/* ------------------------------------------------------------------- 機台 */
function drawMachine(mid, m, evt, hazard, ui){
  const c = A.cells[mid];
  const down = !m || m.online === false;
  const band = m ? m.worst_band : "normal";
  const health = m ? m.health : 0;
  const rate = m ? m.production_rate_uph : 0;
  const ratio = clamp01(rate / (A.rated[mid] || 1));
  const packaging = A.kind[mid] === "packaging";
  const focused = !!(ui && ui.machineId === mid && ui.mode !== "nominal");
  const confidence = focused ? Math.max(.2, ui.confidence || 0) : 0;
  const tempo = focused ? Math.max(2.5, 6 - confidence * 3) : 3;
  const blink = Math.floor(A.t * tempo) % 2 === 0;

  drawPlatform(c);

  // 危險區：機台前方的黃黑警示帶，有人闖入時轉紅並閃爍
  if(hazard){
    const on = blink;
    for(let i = 0; i < c.w + 8; i += 4)
      rect(c.x - 4 + i, c.plat - 1, 2, 1, on ? C.hazard : C.hazardAlt);
  }

  const bodyCol = down ? C.steelLo : C.steel;
  box(c.x, c.y, c.w, c.h, bodyCol);
  rect(c.x, c.y, c.w, 2, down ? C.steel : C.steelHi);
  rect(c.x, c.y + c.h - 3, c.w, 3, C.steelLo);
  // 機種色帶
  rect(c.x, c.y + 3, c.w, 3, down ? C.steelLo : (packaging ? C.accentPack : C.accentMach));

  // 觀察窗：CNC 是轉動的主軸，包裝機是移動中的箱子
  const wx = c.x + 3, wy = c.y + 8, ww = 15, wh = 13;
  rect(wx, wy, ww, wh, C.ink);
  if(!down && ratio > 0.02){
    const cx = wx + ww / 2, cy = wy + wh / 2;
    if(packaging){
      const k = (A.t * 12 * (0.3 + ratio)) % (ww + 6) - 3;
      crate(wx + k - 2, cy - 3, "raw");
      rect(wx, cy + 3, ww, 1, C.beltHi);
    }else{
      const f = Math.floor(A.t * 14 * (0.25 + ratio)) % 2;
      const col = band === "critical" ? C.lampBad : C.boxDoneHi;
      if(f){ rect(cx - 4, cy - 1, 9, 2, col); rect(cx - 1, cy - 4, 2, 9, col); }
      else { for(let i = -3; i <= 3; i++){ rect(cx + i - 1, cy + i - 1, 2, 2, col); rect(cx + i - 1, cy - i - 1, 2, 2, col); } }
      rect(cx - 1, cy - 1, 2, 2, C.white);
    }
  }else{
    rect(wx + 4, wy + 5, 7, 2, C.steelLo);
  }

  // 控制面板 + 燈號
  rect(c.x + 21, c.y + 9, 16, 11, C.steelLo);
  for(let i = 0; i < 3; i++)
    rect(c.x + 23 + i * 4, c.y + 16, 3, 2, down ? C.steel : (i <= ratio * 3 ? C.hpOk : C.steel));
  const lamp = down ? (m && m.state === "maintenance" ? (blink ? C.lampWarn : C.lampOff) : C.lampOff)
             : band === "critical" ? (blink ? C.lampBad : C.lampOff)
             : band === "warning" ? C.lampWarn
             : m && m.state === "derated" ? C.lampWarn : C.lampOk;
  box(c.x + 23, c.y + 10, 4, 4, lamp);
  // 面板上的跑馬燈：轉得越快跑得越快，停機就全滅
  for(let i = 0; i < 4; i++){
    const on = !down && ratio > 0.02 && (Math.floor(A.t * (2 + ratio * 8)) % 4) === i;
    rect(c.x + 29 + i * 2, c.y + 11, 1, 2, on ? C.lampOk : C.steel);
  }

  // 煙囪
  box(c.x + c.w - 8, c.y - 4, 5, 4, C.steelLo);

  // HP 條與標籤
  const hpx = c.x, hpy = c.y - L.hpH - 1, hpw = c.w;
  box(hpx, hpy, hpw, L.hpH, C.hpBg);
  const hc = health < 40 ? C.hpBad : health < 70 ? C.hpMid : C.hpOk;
  const seg = Math.round((hpw - 2) * clamp01(health / 100));
  for(let i = 0; i < seg; i += 3) rect(hpx + 1 + i, hpy + 1, 2, L.hpH - 2, hc);
  text(mid, c.x, c.cellTop, C.white, 1);
  const lbl = down ? (m && m.state === "maintenance"
                        ? "FIX " + pad(m.maintenance_remaining_min, 2) : "STOP")
                   : pad(rate, 3) + "U/H";
  textR(lbl, c.x + c.w, c.cellTop, down ? C.lampWarn : C.crateHi, 1);

  // 效果：過熱冒煙、critical 噴火花、事件鎖定的機台掛驚嘆號
  if(!down && (band === "warning" || band === "critical" || (focused && ui.kind === "thermal")))
    smoke(c.x + c.w - 6, c.y - 5, band === "critical" || ui?.mode === "critical", focused ? confidence : .35);
  if(!down && (band === "critical" || (focused && ui.kind === "electrical")))
    sparks(c.x + c.w / 2, c.y + c.h / 2, focused ? confidence : .55);
  if(!down && focused && ui.kind === "vibration") stress(c.x, c.y, c.w, c.h, confidence);
  // 兩個圖示可能同時出現（事件鎖定 + 正在維修），各自靠邊站不要疊在一起
  const both = evt && down && m && m.state === "maintenance";
  if(evt && blink) bubble(c.x + c.w / 2 - (both ? 11 : 4), c.cellTop - 12);
  if(down && m && m.state === "maintenance")
    wrenchIcon(c.x + c.w / 2 + (both ? 3 : -4), c.cellTop - 12);
}

function smoke(x, y, bad, intensity){
  const count=3+Math.round(4*(intensity||0));
  for(let i = 0; i < count; i++){
    const ph = (A.t * (0.75+(intensity||0)*.7) + i / count) % 1;
    const s = 1 + Math.floor(ph * 3);
    rect(x + Math.sin((ph + i) * 5) * 3, y - ph * 14, s, s, bad ? C.smokeBad : C.smoke);
  }
}
function sparks(x, y, intensity){
  const count=3+Math.round(6*(intensity||0));
  for(let i = 0; i < count; i++){
    const ph = (A.t * (1.6+(intensity||0)*1.8) + i * 0.37) % 1;
    const a = i * 1.7 + A.t;
    rect(x + Math.cos(a) * ph * 16, y + Math.sin(a) * ph * 12, 1, 1,
         ph > 0.6 ? C.lampBad : C.spark);
  }
}
function stress(x, y, w, h, intensity){
  const on=Math.floor(A.t*(4+intensity*7))%2===0;
  if(!on) return;
  const reach=2+Math.round(intensity*4), col=intensity>.7?C.lampBad:C.lampWarn;
  [[x-2,y+5,-1,-1],[x+w+2,y+7,1,-1],[x-2,y+h-6,-1,1],[x+w+2,y+h-5,1,1]].forEach(([sx,sy,dx,dy])=>{
    rect(sx+dx*reach,sy+dy*reach,Math.max(1,reach-1),1,col);
    rect(sx+dx*reach,sy+dy*reach,1,Math.max(1,reach-1),col);
  });
}
function bubble(x, y){
  box(x, y, 8, 9, C.white);
  rect(x + 3, y + 1, 2, 5, C.lampBad);
  rect(x + 3, y + 7, 2, 1, C.lampBad);
}
/* 扳手：左上是開口的頭，往右下拉一條斜柄。8×8 太小畫不出弧線，
   靠「開口那一格留白」讓它不會被看成一支鎚子。 */
function wrenchIcon(x, y){
  box(x, y, 8, 8, C.white);
  rect(x + 1, y + 1, 3, 3, C.steelLo);
  rect(x + 1, y + 1, 1, 1, C.white);
  rect(x + 3, y + 1, 1, 1, C.white);
  rect(x + 3, y + 3, 2, 2, C.steelLo);
  rect(x + 4, y + 4, 2, 2, C.steelLo);
  rect(x + 5, y + 5, 2, 2, C.steelLo);
}

/* ------------------------------------------------------------------ 人物 */
function person(x, y, opt){
  const f = opt.frame, hatCol = opt.tech ? C.helmet : (opt.cap || C.cap);
  box(x + 1, y, 6, 3, hatCol);            // 帽子
  rect(x, y + 2, 8, 1, hatCol);           // 帽簷
  rect(x + 2, y + 3, 4, 3, C.skin);       // 臉
  rect(x + (opt.flip ? 2 : 4), y + 4, 1, 1, C.ink);
  box(x + 1, y + 6, 6, 5, C.suit);        // 身體
  rect(x + 2, y + 6, 4, 4, opt.tech ? C.vest : C.suit);
  rect(x, y + 6, 1, 3, C.skin);
  rect(x + 7, y + 6, 1, 3, C.skin);
  if(f){ rect(x + 1, y + 11, 2, 3, C.ink); rect(x + 5, y + 11, 2, 3, C.ink); }
  else { rect(x + 2, y + 11, 2, 3, C.ink); rect(x + 4, y + 11, 2, 3, C.ink); }
  if(opt.tech){                            // 揮扳手
    const sw = f ? -2 : 1;
    rect(x + 8, y + 5 + sw, 1, 4, C.tool);
    rect(x + 7, y + 4 + sw, 3, 2, C.tool);
  }
}

/* -------------------------------------------------------------------- HUD */
function drawHUD(s, kpi){
  rect(0, 0, A.vw, L.hudH, C.hud);
  rect(0, L.hudH - 1, A.vw, 1, C.steelLo);
  const health = s ? s.factory_health : 0;
  const out = s ? Object.values(s.machines).reduce((a, m) =>
        Math.max(a, A.stages[A.stages.length - 1].includes(m.machine_id) ? m.production_rate_uph : 0), 0) : 0;
  const cells = [
    ["LINE-1", "", C.white],
    ["HEALTH", pad(health, 3), health < 60 ? C.lampBad : C.lampOk],
    ["OUTPUT", pad(out, 3) + "U/H", C.crateHi],
    ["TIME", "T+" + pad(s ? s.sim_minutes : 0, 3), C.white],
  ];
  let x = 3;
  cells.forEach(([k, v, col]) => {
    text(k, x, 2, C.hudDim, 1); x += textW(k, 1) + 3;
    if(v){ text(v, x, 2, col, 1); x += textW(v, 1) + 6; }
  });
}

/* ==================================================================== 主畫面 */
function draw(){
  if(!A.ready) return;
  const st = A.snap, s = st ? st.snapshot : null;
  A.ctx.clearRect(0, 0, A.cv.width, A.cv.height);
  drawSky();

  const cam = s ? (s.cameras || [])[0] || {} : {};
  const ev = st ? ((st.last_loop && st.last_loop.event) ||
                   ((st.live || {}).detect || {}).event) : null;
  const evtM = ev ? ev.machine_id : null;
  const hazardM = cam.person_in_hazard_zone ? cam.machine_id : null;

  A.belts.forEach(b => drawBelt(b, s ? s.machines[b.from] : null));

  const orders = s ? Object.values(s.orders) : [];
  drawSilo(orders.filter(o => o.remaining > 0).length);
  drawTruck(orders.reduce((a, o) => a + (o.produced || 0), 0));
  drawGround();

  Object.keys(A.cells).forEach(mid =>
    drawMachine(mid, s ? s.machines[mid] : null, mid === evtM, mid === hazardM, A.ui));

  // 走道上的巡線人員；停下來的時候不擺腿
  A.crew.forEach(w => person(w.x, A.ground - 14,
    {frame:w.wait > 0 ? 0 : w.frame, flip:w.dir < 0, cap:w.cap}));

  // Vision Agent 判定有人進入運轉危險區：那個人站到機台旁邊，頭上跳驚嘆號
  if(hazardM && A.cells[hazardM]){
    const c = A.cells[hazardM];
    person(c.x + c.w + 3, c.plat - 14, {frame:Math.floor(A.t * 6) % 2, flip:true});
    if(Math.floor(A.t * 3) % 2) bubble(c.x + c.w + 4, c.plat - 26);
  }
  // 維修技師：只要有機台在維修就會出現在它旁邊敲
  if(s) Object.keys(A.cells).forEach(mid => {
    const m = s.machines[mid];
    if(m && m.state === "maintenance"){
      const c = A.cells[mid];
      person(c.x - 11, c.plat - 14, {frame:Math.floor(A.t * 5) % 2, tech:true});
      sparks(c.x - 1, c.plat - 8);
    }
  });

  drawHUD(s, st ? st.kpi : null);
}

/* ====================================================================== 迴圈 */
function step(ts){
  A.raf = 0;
  if(!A.active) return;
  const dt = Math.min(0.1, (ts - A.last) / 1000 || 0.033);
  A.last = ts; A.t += dt;

  const s = A.snap ? A.snap.snapshot : null;
  A.belts.forEach(b => {
    const m = s ? s.machines[b.from] : null;
    const rate = m ? (m.online === false ? 0 : m.production_rate_uph || 0) : 0;
    b.ph = (b.ph + dt * 26 * clamp01(rate / (A.rated[b.from] || 1))) % (L.crateGap * 12);
  });
  // 巡線人員：走到底就轉身，轉身前站一下（純粹讓動作不像鐘擺）
  const lo = L.siloW - 6, hi = A.vw - L.truckW + 6;
  A.crew.forEach(w => {
    if(w.wait > 0){ w.wait -= dt; return; }
    w.x += w.dir * dt * w.sp;
    if(w.x > hi){ w.x = hi; w.dir = -1; w.wait = 0.6 + Math.random(); }
    else if(w.x < lo){ w.x = lo; w.dir = 1; w.wait = 0.6 + Math.random(); }
    w.ft += dt; if(w.ft > 0.16){ w.ft = 0; w.frame ^= 1; }
  });

  draw();
  A.raf = requestAnimationFrame(step);
}
function start(){
  if(A.raf || !A.active || !A.ready) return;
  if(A.reduce){ draw(); return; }
  A.last = performance.now();
  A.raf = requestAnimationFrame(step);
}
function stop(){ if(A.raf){ cancelAnimationFrame(A.raf); A.raf = 0; } }

/* ======================================================================= API */
window.Arcade = {
  init(host, topo){
    if(!host || !host.getContext && !host.appendChild) return false;
    if(!layout(topo)) return false;
    A.host = host;
    host.innerHTML = "";
    A.cv = document.createElement("canvas");
    A.cv.className = "arc-cv";
    A.cv.setAttribute("role", "img");
    A.cv.setAttribute("aria-label", "2D 像素產線模擬畫面，數據與下方 Machine Telemetry 相同");
    host.appendChild(A.cv);
    A.ctx = A.cv.getContext("2d", {alpha:true});
    A.ctx.imageSmoothingEnabled = false;
    A.ready = true;

    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    A.reduce = mq.matches;
    mq.addEventListener("change", e => { A.reduce = e.matches; stop(); start(); });

    if(window.ResizeObserver){
      A.ro = new ResizeObserver(() => resize());
      A.ro.observe(host);
    }else window.addEventListener("resize", resize);

    document.addEventListener("visibilitychange", () => {
      if(document.hidden) stop(); else start();
    });
    resize();
    return true;
  },
  sync(state, ui){
    A.snap = state;
    A.ui = ui || null;
    if(!A.active || A.reduce) draw();   // 靜止模式也要跟著資料更新
  },
  setActive(on){
    A.active = !!on;
    if(A.active) start(); else stop();
  },
  get ready(){ return A.ready; },
};
})();
