/* ==========================================================================
   PIXEL — 兩個像素視圖（ARCADE 產線圖 / CAM 監視器）共用的底層
   --------------------------------------------------------------------------
   這裡只有「怎麼把色塊畫到 canvas 上」，沒有任何業務語意：
   3×5 點陣字、NES 調色盤，以及會四捨五入到整數裝置像素的 rect / box。

   為什麼要抽出來共用：字模是一張表，複製到第二個檔案之後就會開始各自漂移
   —— 有人補了一個標點，另一邊沒有，那一邊的畫面就少一個字。調色盤同理，
   兩個視圖的鋼灰色不一樣會馬上被看出來。

   跟 arcade.js 同一條紀律：不載任何圖檔或字型，離線打開一模一樣。
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

/* ---------------------------------------------------------- 冷鋼灰 / 工業青綠調色盤 */
const C = {
  ink:"#163044", white:"#F8FAF9",
  sky:"#D9E2E5", skyHi:"#EEF3F4", cloud:"#F8FAF9", cloudSh:"#C8D4D8",
  hill:"#9FB1B8", hillHi:"#C8D4D8",
  brick:"#667E88", brickHi:"#9FB1B8", brickLo:"#314C5D", mortar:"#163044",
  steel:"#9FB1B8", steelHi:"#D9E2E5", steelLo:"#516B78",
  belt:"#314C5D", beltHi:"#667E88", roller:"#C8D4D8",
  crate:"#667E88", crateHi:"#9FB1B8", crateLo:"#314C5D",
  boxDone:"#0B756E", boxDoneHi:"#77AAA6", boxDoneLo:"#075D58",
  lampOk:"#0B756E", lampWarn:"#516B78", lampBad:"#163044", lampOff:"#9FB1B8",
  hpOk:"#0B756E", hpMid:"#516B78", hpBad:"#163044", hpBg:"#D9E2E5",
  skin:"#C8D4D8", hair:"#163044", cap:"#0B756E", suit:"#314C5D", vest:"#667E88",
  helmet:"#9FB1B8", tool:"#D9E2E5",
  smoke:"#9FB1B8", smokeBad:"#516B78", spark:"#0B756E",
  hud:"#163044", hudDim:"#C8D4D8", hazard:"#0B756E", hazardAlt:"#9FB1B8",
  accentMach:"#0B756E", accentPack:"#516B78",
  // 監視器專用：室內牆面與地板，以及 CV 疊圖的框線
  wall:"#D9E2E5", wallHi:"#EEF3F4", wallLo:"#C8D4D8",
  floor:"#C8D4D8", floorHi:"#D9E2E5", floorLo:"#9FB1B8",
  boxOk:"#0B756E", boxBad:"#163044",
};

/* 建立一組綁在某個 2D context 上的繪圖工具。
   px = 一個虛擬像素對應幾個裝置像素，由呼叫端在 resize 時設定。 */
function create(ctx) {
  const st = { ctx, px: 3, cache: new Map() };
  const R = v => Math.round(v * st.px);

  function rect(x, y, w, h, col) {
    const x0 = R(x), y0 = R(y);
    st.ctx.fillStyle = col;
    st.ctx.fillRect(x0, y0, R(x + w) - x0, R(y + h) - y0);
  }

  /* 外框：先畫一圈墨色再填色，NES 精靈的輪廓感就是這樣來的 */
  function box(x, y, w, h, col) {
    rect(x - 1, y - 1, w + 2, h + 2, C.ink);
    rect(x, y, w, h, col);
  }

  function textW(str, s) { return (str.length * GW - 1) * (s || 1); }

  /* 文字先畫進離螢幕畫布再貼上。整場景每幀有上百個字，
     逐點 fillRect 會讓 CPU 白白燒在同樣的字串上。 */
  function text(str, x, y, col, s) {
    s = s || 1;
    const key = str + "|" + s + "|" + col + "|" + st.px;
    let cv = st.cache.get(key);
    if (!cv) {
      const w = Math.max(1, R(textW(str, s))), h = Math.max(1, R(GH * s));
      cv = document.createElement("canvas");
      cv.width = w; cv.height = h;
      const c = cv.getContext("2d");
      c.fillStyle = col;
      for (let i = 0; i < str.length; i++) {
        const g = GLYPH[str[i]] || GLYPH[str[i].toUpperCase()] || GLYPH[" "];
        for (let r = 0; r < GH; r++) {
          const bits = parseInt(g[r], 8);
          for (let b = 0; b < 3; b++) {
            if (!(bits & (4 >> b))) continue;
            const px0 = R((i * GW + b) * s), py0 = R(r * s);
            c.fillRect(px0, py0, R((i * GW + b + 1) * s) - px0, R((r + 1) * s) - py0);
          }
        }
      }
      if (st.cache.size > 160) st.cache.clear();
      st.cache.set(key, cv);
    }
    st.ctx.drawImage(cv, R(x), R(y));
  }

  function textR(str, xRight, y, col, s) { text(str, xRight - textW(str, s), y, col, s); }

  return {
    rect, box, text, textR, textW,
    get px() { return st.px; },
    setScale(px) { st.px = px; st.cache.clear(); },
    clearCache() { st.cache.clear(); },
  };
}

window.Pixel = { C, GW, GH, GLYPH, create };
})();
