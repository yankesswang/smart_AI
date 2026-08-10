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
  skin:"#FCB08C", hair:"#3A2A1C", cap:"#E43B44", suit:"#2038EC", vest:"#FF7A1A",
  helmet:"#FCD800", tool:"#C8C8D8",
  smoke:"#B0B0C0", smokeBad:"#6C6C7C", spark:"#FCD800",
  hud:"#000000", hudDim:"#9C9CB4", hazard:"#F83800", hazardAlt:"#FCD800",
  accentMach:"#2E6BE6", accentPack:"#F08000",
  // 監視器專用：室內牆面與地板，以及 CV 疊圖的框線
  wall:"#26262E", wallHi:"#34343E", wallLo:"#1A1A22",
  floor:"#3A3A44", floorHi:"#4A4A56", floorLo:"#2A2A32",
  boxOk:"#58D854", boxBad:"#F83800",
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
