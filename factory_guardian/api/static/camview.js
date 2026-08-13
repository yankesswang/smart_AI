/* ==========================================================================
   CAM-01 VIEW — 工安監視器畫面
   --------------------------------------------------------------------------
   Safety Agent 的裁決寫著「Camera CAM-01 偵測到人員進入 M-A 運轉中危險區」，
   但畫面上一直沒有影像可看，只有一句 caption。這支檔案把那句話畫出來。

   Demo 預設用專案內封裝的公開授權工廠影片作為監視器底片，並在 HUD 常駐標示
   DEMO FOOTAGE / SYNTHETIC EVENTS，避免被誤認成真實廠區或模型輸入。影片無法
   解碼時，才退回下方由 CameraObservation 驅動的像素場景，現場不會黑屏。

   刻意不放實景照片。照片是死的：人員離開危險區、煙霧停止，照片還是同一張，
   它會變成整個戰情中心裡唯一一個和資料對不上的東西 —— 而這套系統的立論
   正是「數字不是編的」。所以這裡每一格像素都綁在 CameraObservation 的欄位上：

     person_count          → 畫幾個人
     person_in_hazard_zone → 有沒有人站在黃黑警示帶裡（帶子轉紅並閃爍）
     ppe_compliant         → 戴不戴安全帽；未合規的人偵測框轉紅並標 NO PPE
     fall_detected         → 那個人躺在地上
     smoke_detected        → 機台上方冒煙
     confidence            → 每個偵測框上的信心值
     machine_id            → 畫面裡是哪一台，狀態燈與主軸跟著 snapshot 走

   疊在人身上的偵測框是刻意的：它讓畫面讀起來是「模型判讀的結果」，
   而不是一張卡通。VLM 後端換成真的模型時（agents/vision.py 的 OpenAIVLM），
   欄位不變，這裡一行都不用改。

   紀律與 arcade.js 相同：全部 fillRect 程序化繪製，不載任何圖檔或字型。
   ========================================================================== */
(function () {
"use strict";

const C = Pixel.C;

/* ------------------------------------------------------------------ 版面
   16:10 左右的監視器畫面。上下各一條 HUD，中間是場景。 */
const L = {
  vw:168, vh:106,
  hudH:9,          // 上：攝影機編號 / 區域 / 時間 / REC
  footH:11,        // 下：偵測結果標籤列
  horizon:60,      // 牆與地板的交界
  mach:{x:56, y:26, w:54, h:36},   // 機台（底部壓在 horizon 下方一點，像站在地上）
};
const sceneTop = L.hudH;
const sceneBot = L.vh - L.footH;

/* 危險區：機台正前方的黃黑警示帶。上窄下寬做出俯視透視。 */
const ZONE = {y:64, h:17, topL:46, topR:122, botL:36, botR:134};

const clamp01 = v => Math.max(0, Math.min(1, v));
const lerp = (a, b, t) => a + (b - a) * t;
const pad = (n, w) => String(Math.max(0, Math.round(n))).padStart(w, "0");

const V = {
  host:null, cv:null, ctx:null, P:null, px:3,
  state:null, t:0, last:0, raf:0, active:false, ready:false,
  ro:null, reduce:false, video:null, videoReady:false,
  videos:{}, videoReadyKinds:new Set(), videoKind:"normal",
};

let rect, box, text, textR, textW;

/* ====================================================================== 場景 */

/* 牆面：工業室內的暗色板牆，接縫與一盞頂燈。頂燈讓畫面有個光源方向，
   不然整片平塗會像色卡。 */
function drawWall() {
  rect(0, sceneTop, L.vw, L.horizon - sceneTop, C.wall);
  for (let x = 8; x < L.vw; x += 26) rect(x, sceneTop, 1, L.horizon - sceneTop, C.wallLo);
  rect(0, L.horizon - 3, L.vw, 3, C.wallHi);
  // 頂燈與往下擴散的光暈（用兩層梯形色塊近似）
  rect(78, sceneTop, 14, 2, C.steelHi);
  for (let i = 0; i < 7; i++) rect(76 - i * 1.6, sceneTop + 2 + i * 2, 18 + i * 3.2, 2, C.wallHi);
}

/* 地板：往下逐漸變亮的橫向條紋，模擬俯視角的透視。 */
function drawFloor() {
  rect(0, L.horizon, L.vw, sceneBot - L.horizon, C.floor);
  for (let i = 0; i < 6; i++) {
    const y = L.horizon + 2 + i * i * 1.1;
    if (y > sceneBot - 1) break;
    rect(0, y, L.vw, 1, i % 2 ? C.floorLo : C.floorHi);
  }
}

/* 黃黑斜紋警示帶。逐列畫，每列的條紋相位往右移一格就成了斜紋；
   一列只畫十來個 rect，不是逐點填 —— 逐點在 60fps 下會燒掉一顆核心。 */
function drawHazardZone(active, blink) {
  const on = active ? (blink ? C.hazard : C.hazardAlt) : C.hazardAlt;
  const off = active ? C.ink : C.steelLo;
  const period = 8, run = 4;
  for (let i = 0; i < ZONE.h; i++) {
    const t = i / ZONE.h, y = ZONE.y + i;
    const x0 = lerp(ZONE.topL, ZONE.botL, t), x1 = lerp(ZONE.topR, ZONE.botR, t);
    rect(x0, y, x1 - x0, 1, off);
    let start = Math.floor(x0) - (((Math.floor(x0) + i) % period) + period) % period;
    for (let x = start; x < x1; x += period) {
      const a = Math.max(x0, x), b = Math.min(x1, x + run);
      if (b > a) rect(a, y, b - a, 1, on);
    }
  }
}

/* 機台：正面視角，比產線圖上的大得多，看得到主軸與控制面板。
   狀態全部來自 snapshot，沒有寫死的燈號。 */
function drawMachine(m, mid, blink) {
  const g = L.mach;
  const down = !m || m.online === false;
  const band = m ? m.worst_band : "normal";
  const rate = m ? m.production_rate_uph || 0 : 0;

  // 機腳與地面陰影
  rect(g.x + 4, g.y + g.h, g.w - 8, 4, C.steelLo);
  rect(g.x - 2, g.y + g.h + 4, g.w + 4, 2, C.floorLo);

  box(g.x, g.y, g.w, g.h, down ? C.steelLo : C.steel);
  rect(g.x, g.y, g.w, 2, down ? C.steel : C.steelHi);
  rect(g.x, g.y + 3, g.w, 3, down ? C.steelLo : C.accentMach);
  rect(g.x, g.y + g.h - 4, g.w, 4, C.steelLo);

  // 觀察窗與轉動中的主軸。高度留一點給底下的機台標籤，不然文字會壓到窗框。
  const wx = g.x + 4, wy = g.y + 9, ww = 24, wh = 17;
  rect(wx, wy, ww, wh, C.ink);
  rect(wx, wy, ww, 1, C.steelLo);
  if (!down && rate > 0.5) {
    const cx = wx + ww / 2, cy = wy + wh / 2;
    const col = band === "critical" ? C.lampBad : C.boxDoneHi;
    const f = Math.floor(V.t * 14) % 2;
    if (f) { rect(cx - 7, cy - 1, 15, 2, col); rect(cx - 1, cy - 7, 2, 15, col); }
    else {
      for (let i = -5; i <= 5; i++) {
        rect(cx + i - 1, cy + i - 1, 2, 2, col);
        rect(cx + i - 1, cy - i - 1, 2, 2, col);
      }
    }
    rect(cx - 1, cy - 1, 2, 2, C.white);
  } else {
    rect(wx + 8, wy + 9, 9, 2, C.steelLo);
  }

  // 控制面板：狀態燈 + 產出跑馬燈
  const px0 = g.x + 32, py0 = g.y + 10;
  rect(px0, py0, 18, 15, C.steelLo);
  const lamp = down ? (m && m.state === "maintenance" ? (blink ? C.lampWarn : C.lampOff) : C.lampOff)
    : band === "critical" ? (blink ? C.lampBad : C.lampOff)
      : band === "warning" ? C.lampWarn
        : m && m.state === "derated" ? C.lampWarn : C.lampOk;
  box(px0 + 2, py0 + 2, 5, 5, lamp);
  for (let i = 0; i < 4; i++) {
    const lit = !down && rate > 0.5 && (Math.floor(V.t * 6) % 4) === i;
    rect(px0 + 2 + i * 4, py0 + 9, 2, 3, lit ? C.lampOk : C.steel);
  }

  // 煙囪
  rect(g.x + g.w - 12, g.y - 5, 7, 5, C.steelLo);
}

/* 機台銘牌畫在警示帶之後，否則會被斜紋蓋掉一半。 */
function drawMachineLabel(m, mid) {
  const g = L.mach;
  const down = !m || m.online === false;
  const rate = m ? m.production_rate_uph || 0 : 0;
  const y = g.y + g.h - 10;
  rect(g.x + 2, y - 1, g.w - 4, 7, C.ink);
  text(mid || "", g.x + 4, y, C.white, 1);
  textR(down ? (m && m.state === "maintenance" ? "MAINT" : "STOP") : pad(rate, 3) + "U/H",
    g.x + g.w - 4, y, down ? C.lampWarn : C.crateHi, 1);
}

function smoke(x, y, bad, intensity) {
  const count = 4 + Math.round(5 * clamp01(intensity));
  for (let i = 0; i < count; i++) {
    const ph = (V.t * 0.8 + i / count) % 1;
    const s = 2 + Math.floor(ph * 4);
    rect(x + Math.sin((ph + i) * 5) * 5, y - ph * 24, s, s, bad ? C.smokeBad : C.smoke);
  }
}

/* ------------------------------------------------------------------ 人物
   高 22 個虛擬像素（s=1）。安全帽是這裡最重要的一格：ppe_compliant=false
   就換成頭髮，輪廓會矮一截、顏色完全不同，投影機上也分得出來。 */
function guard(x, foot, s, opt) {
  const y = foot - 22 * s;
  const u = (dx, dy, dw, dh, col) => rect(x + dx * s, y + dy * s, dw * s, dh * s, col);
  const f = opt.frame ? 1 : 0;

  if (opt.helmet) {
    u(1, 0, 8, 4, C.helmet);
    u(0, 4, 10, 1, C.helmet);
    u(2, 5, 6, 4, C.skin);
  } else {
    u(2, 2, 6, 3, C.hair);
    u(2, 5, 6, 4, C.skin);
  }
  u(opt.flip ? 3 : 5, 6, 1, 1, C.ink);              // 眼睛
  u(1, 9, 8, 8, opt.vest ? C.vest : C.suit);        // 上身
  u(1, 12, 8, 1, C.white);                          // 反光條
  u(0, 9, 1, 5, C.skin); u(9, 9, 1, 5, C.skin);     // 手臂
  if (f) { u(1, 17, 2, 5, C.ink); u(6, 17, 2, 5, C.ink); }
  else { u(2, 17, 2, 5, C.ink); u(5, 17, 2, 5, C.ink); }
}

/* 跌倒：同一個人躺著。這是 fall_detected 唯一的視覺表現，不另外加特效。 */
function guardFallen(x, foot, s, opt) {
  const y = foot - 10 * s;
  const u = (dx, dy, dw, dh, col) => rect(x + dx * s, y + dy * s, dw * s, dh * s, col);
  if (opt.helmet) { u(0, 1, 4, 8, C.helmet); u(4, 2, 1, 6, C.helmet); }
  else u(0, 2, 4, 6, C.hair);
  u(4, 3, 4, 6, C.skin);
  u(8, 1, 8, 8, opt.vest ? C.vest : C.suit);
  u(12, 1, 1, 8, C.white);
  u(16, 2, 5, 2, C.ink); u(16, 6, 5, 2, C.ink);
}

/* CV 偵測框：四角括號 + 上緣標籤。刻意不畫完整矩形 —— 四角括號是
   偵測器疊圖的通用語彙，也不會把人的輪廓蓋掉。 */
function detBox(x, y, w, h, col, label, below) {
  const c = Math.max(3, Math.round(Math.min(w, h) / 3));
  [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]]
    .forEach(([bx, by, dx, dy]) => {
      rect(dx > 0 ? bx : bx - c, dy > 0 ? by : by - 1, c, 1, col);
      rect(dx > 0 ? bx : bx - 1, dy > 0 ? by : by - c, 1, c, col);
    });
  if (!label) return;
  const w0 = textW(label, 1) + 2;
  // 標籤預設在框上方；站在機台前的人頭頂就是機台，那裡放字會壓到機台銘牌，
  // 這時改放腳下 —— 地面是空的。
  const ly = below ? y + h + 2 : y - 7;
  const lx = Math.max(1, Math.min(x, L.vw - w0 - 1));
  rect(lx, ly, w0, 6, col);
  text(label, lx + 1, ly + 1, col === C.boxBad ? C.white : C.ink, 1);
}

/* ------------------------------------------------------------------ HUD */
function drawHUD(cam, snap) {
  rect(0, 0, L.vw, L.hudH, C.hud);
  rect(0, L.hudH - 1, L.vw, 1, C.steelLo);
  text(cam.camera_id || "CAM", 3, 2, C.white, 1);
  text("ZONE " + (cam.zone_id || "-"), 3 + textW(cam.camera_id || "CAM", 1) + 5, 2, C.hudDim, 1);
  // 錄影指示燈：一秒閃一次，跟資料無關，純粹是監視器的語彙
  const rec = Math.floor(V.t * 1.6) % 2 === 0;
  const recW = textW("REC", 1);
  text("REC", L.vw - 3 - recW, 2, rec ? C.lampBad : C.hudDim, 1);
  rect(L.vw - 7 - recW, 3, 3, 3, rec ? C.lampBad : C.lampOff);
  textR("T+" + pad(snap ? snap.sim_minutes : 0, 3), L.vw - 11 - recW, 2, C.white, 1);
}

/* 下緣標籤列：每一格就是 CameraObservation 的一個欄位。
   有事的格子反白成紅底，沒事的維持暗色 —— 紅色在這裡只表示異常。 */
function drawFooter(cam) {
  const y = L.vh - L.footH;
  rect(0, y, L.vw, L.footH, C.hud);
  rect(0, y, L.vw, 1, C.steelLo);
  const tags = [
    ["PERSON " + pad(cam.person_count || 0, 1), (cam.person_count || 0) > 0, false],
    ["HAZARD ZONE", !!cam.person_in_hazard_zone, true],
    ["PPE INCOMPLETE", cam.ppe_compliant === false, true],
    ["SMOKE", !!cam.smoke_detected, true],
    ["FALL", !!cam.fall_detected, true],
  ];
  let x = 3;
  tags.forEach(([label, on, danger]) => {
    if (!on && danger) return;                       // 沒發生的危險項不佔位置
    const w = textW(label, 1) + 3;
    if (x + w > L.vw - 32) return;
    if (danger) { rect(x - 1, y + 2, w + 1, 7, C.hazard); text(label, x, y + 3, C.ink, 1); }
    else text(label, x, y + 3, C.hudDim, 1);
    x += w + 4;
  });
  textR("CONF " + (cam.confidence != null ? cam.confidence.toFixed(2) : "-.--"),
    L.vw - 3, y + 3, C.hudDim, 1);
}

/* 畫面四角的取景括號，讓它讀起來像監視器輸出而不是插圖 */
function drawFraming() {
  const c = 6, m = 2, y0 = sceneTop + m, y1 = sceneBot - m, x0 = m, x1 = L.vw - m;
  [[x0, y0, 1, 1], [x1, y0, -1, 1], [x0, y1, 1, -1], [x1, y1, -1, -1]]
    .forEach(([bx, by, dx, dy]) => {
      rect(dx > 0 ? bx : bx - c, dy > 0 ? by : by - 1, c, 1, C.hudDim);
      rect(dx > 0 ? bx : bx - 1, dy > 0 ? by : by - c, 1, c, C.hudDim);
    });
}

function noSignal() {
  rect(0, 0, L.vw, L.vh, C.hud);
  const blink = Math.floor(V.t * 1.2) % 2 === 0;
  text("NO CAMERA FEED", L.vw / 2 - textW("NO CAMERA FEED", 1) / 2, L.vh / 2 - 3,
    blink ? C.hudDim : C.steelLo, 1);
}

function desiredVideoKind(cam) {
  return cam && (cam.person_in_hazard_zone || cam.ppe_compliant === false) ? "hazard" : "normal";
}

function pauseVideos() { Object.values(V.videos).forEach(v => v.pause()); }

/* 兩段片都在本機預載。事件切換時只換 canvas 的來源，不會重新下載；若事件片
   尚未解碼完成則短暫沿用正常片，避免 Demo 現場閃黑。 */
function selectVideo(cam) {
  const wanted = desiredVideoKind(cam);
  const kind = V.videoReadyKinds.has(wanted) ? wanted
    : V.videoReadyKinds.has("normal") ? "normal" : wanted;
  const next = V.videos[kind];
  if (!next) { V.videoReady = false; return; }
  if (V.video !== next) {
    if (V.video) V.video.pause();
    V.video = next;
    V.videoKind = kind;
  }
  V.videoReady = V.videoReadyKinds.has(kind);
  if (V.videoReady && V.active && !V.reduce && V.video.paused)
    V.video.play().catch(() => {});
}

function drawHazardVideoOverlay(cam) {
  const p = V.px, blink = Math.floor(V.t * 3) % 2 === 0;
  const pts = [[72,42],[116,42],[132,88],[60,88]];
  V.ctx.save();
  V.ctx.beginPath();
  pts.forEach(([x,y],i) => i ? V.ctx.lineTo(x*p,y*p) : V.ctx.moveTo(x*p,y*p));
  V.ctx.closePath();
  V.ctx.fillStyle = blink ? "rgba(11,117,110,.26)" : "rgba(22,48,68,.23)";
  V.ctx.strokeStyle = C.white;
  V.ctx.lineWidth = Math.max(1, p);
  V.ctx.setLineDash([4*p,3*p]);
  V.ctx.fill(); V.ctx.stroke();
  V.ctx.restore();

  const conf = cam.confidence != null ? cam.confidence.toFixed(2) : "";
  const label = cam.ppe_compliant === false ? "NO HI-VIS " + conf : "IN ZONE " + conf;
  // 事件片中間偏右的作業員；其餘人員保持為場景背景，不虛構額外辨識數量。
  detBox(96, 31, 17, 49, C.boxBad, label, true);
  rect(3, 88, 132, 7, C.hud);
  text("RESTRICTED AREA / MACHINE RUNNING", 5, 89, C.white, 1);
}

/* 公開素材只是視覺底片，不宣稱是 Vision Agent 的輸入。使用 cover 裁切，讓
   16:9 影片填滿既有 168:106 監視器比例；上層 HUD 與事件標籤仍吃即時資料。 */
function drawDemoFootage(cam, snap) {
  const v = V.video, cw = V.cv.width, ch = V.cv.height;
  const scale = Math.max(cw / v.videoWidth, ch / v.videoHeight);
  const sw = cw / scale, sh = ch / scale;
  const sx = (v.videoWidth - sw) / 2, sy = (v.videoHeight - sh) / 2;
  V.ctx.drawImage(v, sx, sy, sw, sh, 0, 0, cw, ch);

  // CCTV 冷色調與讀字遮罩；不遮掉輸送線的動態。
  V.ctx.fillStyle = "rgba(10,30,40,.16)";
  V.ctx.fillRect(0, 0, cw, ch);

  const danger = !!(cam.person_in_hazard_zone || cam.smoke_detected || cam.fall_detected);
  const col = danger ? C.boxBad : C.boxOk;
  if (V.videoKind === "hazard") drawHazardVideoOverlay(cam);
  else {
    // 對輸送區畫設備 ROI；這是介面示意框，不虛構影片中不存在的人員框。
    detBox(11, 43, 145, 42, col, "PROCESS ROI " + (cam.confidence != null ? cam.confidence.toFixed(2) : ""), false);
  }
  rect(3, sceneTop + 3, 96, 7, C.hud);
  text("DEMO FOOTAGE / NOT LIVE", 5, sceneTop + 4, C.white, 1);
  rect(3, sceneTop + 12, 108, 7, C.hud);
  text(V.videoKind === "hazard" ? "EVENT-MATCHED STOCK CLIP" : "SYNTHETIC VISION EVENTS",
    5, sceneTop + 13, C.hudDim, 1);
  drawFraming();
  drawHUD(cam, snap);
  drawFooter(cam);
}

/* ==================================================================== 主畫面 */

/* 人員站位。有人闖入危險區時，第一個人站進斜紋帶內、靠近機台；
   其他人留在帶子外。位置固定是刻意的 —— 這是監視器的定點視角，
   讓人在畫面上亂走只會讓「有沒有越線」更難判讀。 */
const SLOTS_SAFE = [
  {x:132, foot:88, s:1.05}, {x:16, foot:84, s:1}, {x:150, foot:78, s:.85}, {x:4, foot:74, s:.8},
];
const SLOTS_HAZARD = [
  {x:96, foot:80, s:.95}, {x:16, foot:86, s:1}, {x:148, foot:78, s:.85}, {x:4, foot:74, s:.8},
];

function draw() {
  if (!V.ready) return;
  V.ctx.clearRect(0, 0, V.cv.width, V.cv.height);

  const st = V.state, snap = st ? st.snapshot : null;
  const cam = snap ? (snap.cameras || [])[0] : null;
  if (!cam) { noSignal(); return; }

  selectVideo(cam);
  if (V.videoReady && V.video && V.video.videoWidth) {
    drawDemoFootage(cam, snap);
    return;
  }

  const machine = snap.machines ? snap.machines[cam.machine_id] : null;
  const intruding = !!cam.person_in_hazard_zone;
  const blink = Math.floor(V.t * 3) % 2 === 0;
  const conf = cam.confidence != null ? cam.confidence.toFixed(2) : "";

  drawWall();
  drawFloor();
  drawMachine(machine, cam.machine_id, blink);
  drawHazardZone(intruding, blink);
  drawMachineLabel(machine, cam.machine_id);

  // 冒煙：影像判定有煙，或機台訊號已進入 critical
  if (cam.smoke_detected || (machine && machine.online !== false && machine.worst_band === "critical"))
    smoke(L.mach.x + L.mach.w - 11, L.mach.y - 7, !!cam.smoke_detected, cam.smoke_detected ? 1 : .5);

  // 人員：第一個人承載 PPE 與跌倒的判定，其餘是背景人力
  const n = Math.min(4, Math.max(0, cam.person_count || 0));
  const slots = intruding ? SLOTS_HAZARD : SLOTS_SAFE;
  for (let i = 0; i < n; i++) {
    const sl = slots[i];
    const lead = i === 0;
    const helmet = lead ? cam.ppe_compliant !== false : true;
    const fallen = lead && !!cam.fall_detected;
    const opt = {helmet, vest:true, frame:Math.floor(V.t * 4 + i) % 2, flip:sl.x > L.vw / 2};

    if (fallen) guardFallen(sl.x, sl.foot, sl.s, opt);
    else guard(sl.x, sl.foot, sl.s, opt);

    // 偵測框永遠要畫 —— 它是「模型看到了什麼」的唯一證據，不能被閃爍藏起來。
    const alert = lead && (intruding || cam.ppe_compliant === false || fallen);
    const label = !lead ? "" : fallen ? "FALL " + conf
      : cam.ppe_compliant === false ? "NO PPE " + conf
        : intruding ? "IN ZONE " + conf : "PERSON " + conf;
    const w = (fallen ? 21 : 10) * sl.s, h = (fallen ? 10 : 22) * sl.s;
    const top = sl.foot - h - 1;
    // 框頂還在機台範圍內 → 標籤改放腳下，免得壓到機台銘牌
    const overMachine = top - 7 < L.mach.y + L.mach.h
      && sl.x + w > L.mach.x && sl.x < L.mach.x + L.mach.w + 34;
    detBox(sl.x - 1, top, w + 2, h + 2, alert ? C.boxBad : C.boxOk, label, overMachine);
  }

  drawFraming();
  drawHUD(cam, snap);
  drawFooter(cam);
}

/* ====================================================================== 迴圈 */
function step(ts) {
  V.raf = 0;
  if (!V.active) return;
  const dt = Math.min(0.1, (ts - V.last) / 1000 || 0.033);
  V.last = ts; V.t += dt;
  draw();
  V.raf = requestAnimationFrame(step);
}
function start() {
  if (V.raf || !V.active || !V.ready) return;
  if (V.reduce) { draw(); return; }
  V.last = performance.now();
  V.raf = requestAnimationFrame(step);
}
function stop() { if (V.raf) { cancelAnimationFrame(V.raf); V.raf = 0; } }

function resize() {
  if (!V.host || !V.ready) return;
  const cssW = V.host.clientWidth || 420;
  const dpr = window.devicePixelRatio || 1;
  V.px = (cssW * dpr) / L.vw;
  V.cv.width = Math.round(L.vw * V.px);
  V.cv.height = Math.round(L.vh * V.px);
  V.cv.style.width = "100%";
  V.cv.style.height = "auto";
  V.P.setScale(V.px);
  draw();
}

/* ======================================================================= API */
window.CamView = {
  init(host) {
    if (!host || !window.Pixel) return false;
    V.host = host;
    host.innerHTML = "";
    V.cv = document.createElement("canvas");
    V.cv.setAttribute("role", "img");
    V.cv.setAttribute("aria-label", "CAM-01 Demo 工廠監視畫面；底片非即時現場，事件標籤由合成資料驅動");
    host.appendChild(V.cv);
    V.ctx = V.cv.getContext("2d", {alpha:false});
    V.ctx.imageSmoothingEnabled = false;
    V.P = Pixel.create(V.ctx);
    ({rect, box, text, textR, textW} = V.P);
    V.ready = true;

    const sources = {
      normal:[
        ["/static/factory-cctv-demo.webm?v=20260813-4", "video/webm"],
        ["/static/factory-cctv-demo.mp4?v=20260813-4", "video/mp4"],
      ],
      hazard:[
        ["/static/factory-cctv-hazard-demo.webm?v=20260813-4", "video/webm"],
        ["/static/factory-cctv-hazard-demo.mp4?v=20260813-4", "video/mp4"],
      ],
    };
    Object.entries(sources).forEach(([kind, files]) => {
      const video = document.createElement("video");
      files.forEach(([src, type]) => {
        const source = document.createElement("source");
        source.src = src; source.type = type; video.appendChild(source);
      });
      video.muted = true; video.loop = true;
      video.playsInline = true; video.preload = "auto";
      video.setAttribute("aria-hidden", "true");
      video.addEventListener("canplay", () => {
        V.videoReadyKinds.add(kind);
        const cam = V.state && V.state.snapshot ? (V.state.snapshot.cameras || [])[0] : null;
        selectVideo(cam); draw();
      });
      video.addEventListener("error", () => { V.videoReadyKinds.delete(kind); draw(); });
      V.videos[kind] = video;
      video.load();
    });
    V.video = V.videos.normal;

    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    V.reduce = mq.matches;
    mq.addEventListener("change", e => {
      V.reduce = e.matches;
      if (e.matches) pauseVideos(); else if (V.video) V.video.play().catch(() => {});
      stop(); start();
    });

    if (window.ResizeObserver) {
      V.ro = new ResizeObserver(() => resize());
      V.ro.observe(host);
    } else window.addEventListener("resize", resize);

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { stop(); pauseVideos(); }
      else { if (V.videoReady && !V.reduce) V.video.play().catch(() => {}); start(); }
    });

    resize();
    V.active = true;
    if (V.videoReady && !V.reduce) V.video.play().catch(() => {});
    start();
    return true;
  },
  sync(state) {
    V.state = state;
    if (!V.active || V.reduce) draw();
  },
  setActive(on) {
    V.active = !!on;
    if (V.active) {
      if (V.videoReady && !V.reduce) V.video.play().catch(() => {});
      start();
    } else {
      pauseVideos();
      stop();
    }
  },
  get ready() { return V.ready; },
};
})();
