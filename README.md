# Factory Guardian AI

**Agentic AI 智慧工廠自主營運與風險管理平台**
2026 中華電信智慧創新應用大賽｜智慧製造｜競賽 MVP

> 當設備、產線或工安異常發生時，AI 不只警告，而是自動完成
> **偵測 → 診斷 → 影響分析 → 方案規劃 → 安全檢查 → 人工核准 → 執行/派工 → 驗證恢復**。

本專案依 [`docs/Factory_Guardian_AI_競賽提案與Demo規格.docx`](docs/) 實作，
是一套可執行、可量測、可稽核的系統，不是簡報。

---

## 60 秒上手

```bash
pip install -e ".[dev]"

factory-guardian demo bearing-degradation     # 舞台 Demo：完整閉環 + 互動核准
factory-guardian benchmark                    # 三組對照組 KPI 比較
factory-guardian serve                        # Web Dashboard → http://127.0.0.1:8000
pytest -q                                     # 102 個測試，離線 4 秒跑完
```

不需要 OpenAI 金鑰也能跑完整 Demo：沒有金鑰時 LLM 敘述會自動退回**確定性離線敘述器**。
有金鑰（`.env` 的 `OPENAI_API_KEY`）則會用 LLM 產生更自然的說明文字 —— 但 LLM 的角色僅止於此。

---

## 三個可信度原則

競賽 Demo 最容易被質疑的是「數字是不是編的」。這套系統用架構回答：

| 原則 | 落實方式 | 驗證它的測試 |
|---|---|---|
| **合成資料明確標示** | 所有 Sensor / Orders / Manual / History 都標記 synthetic，CLI 與 Dashboard 常駐顯示 | `test_api.py::test_dashboard_page_renders` |
| **Agent 看不到答案** | Ground Truth 只存在 `_MachineRuntime.fault`，`snapshot()` 不輸出；規劃用的是**診斷信念模型** | `test_twin.py::test_snapshot_never_exposes_ground_truth`、`test_agents.py::test_diagnosis_never_reads_ground_truth` |
| **執行後回到模擬器驗證** | 執行真的改變孿生體狀態，Verification Agent 在**真實**孿生體上量 KPI，不通過就重新規劃 | `test_orchestrator.py::test_full_loop_reaches_a_verified_execution` |

---

## 系統架構

```
                    ┌──────────────── Orchestrator（協調 + 重試）────────────────┐
                    │                                                            │
   Detect ──▶ Diagnose ──▶ Impact ──▶ Plan ──▶ Safety ──▶ Approve ──▶ Execute ──▶ Verify
      │          │           │          │         │          │           │          │
 Monitoring  Diagnosis  Production  Production  Safety     Policy    Simulator  Verification
   Agent       Agent      Agent       Agent      Agent     Engine       API        Agent
      │          │           │          │         │          │           │          │
      └──────────┴───────────┴──────────┴─────────┴──────────┴───────────┴──────────┘
                                        │
                        Factory Digital Twin（唯一知道真相的地方）
                        Machine A / B → Machine C → Order
```

| 層 | 模組 | 說明 |
|---|---|---|
| Data | [`twin/`](factory_guardian/twin/) | 拓撲、故障模型、模擬引擎、情境 |
| Knowledge | [`knowledge/`](factory_guardian/knowledge/) | Manual / SOP / 維修紀錄 + CJK-aware TF-IDF 檢索 |
| Agent | [`agents/`](factory_guardian/agents/) | 六個 Agent + VLM 後端 |
| Governance | [`policy/`](factory_guardian/policy/)、[`audit.py`](factory_guardian/audit.py) | Safety 硬規則、動作權限、人工核准、JSONL 稽核 |
| Decision | [`optimizer.py`](factory_guardian/optimizer.py) | 加權多準則排名（**不是 LLM**） |
| Action | [`api/`](factory_guardian/api/)、[`cli.py`](factory_guardian/cli.py) | Simulator API、Dashboard、命令列 |

---

## 幾個關鍵設計決定

這些是實作過程中真正影響結果的地方，也是最經得起追問的部分。

### 1. 規劃跑在「診斷信念模型」上，不是跑在答案上

方案投影如果直接在帶著 Ground Truth 的孿生體上乾跑，等於讓 Production Agent 偷看答案，
診斷正確與否就不影響結果，整個 Demo 也就失去意義。

所以 [`fork_as_belief()`](factory_guardian/twin/engine.py) 會拿掉真實故障標籤，換上
Diagnosis Agent 推論出來的故障，並用**觀測到的健康度反推嚴重程度**。
診斷錯了，投影就會錯，然後由 Verification Agent 在真實孿生體上抓出來並觸發重新規劃。

### 2. Safety 看的是「方案會把設備帶到哪裡」，不只是「現在的數值」

早期偵測的價值在於訊號還很微弱時就介入 —— 但那時候「維持全速運轉」在當下並不違規。
所以 Safety Agent 除了現況規則，還有**預測型規則**（`SR-02P` / `SR-03P` / `SR-13`）：
拿方案乾跑出來的訊號峰值來判斷。實際 Demo 中它會這樣擋下 PLAN-A：

> 模擬顯示本方案會讓 M-A 振動由目前 5.65 mm/s 升至 14.18 mm/s，超過危險門檻 7 mm/s。
> 在振動持續上升的情況下維持產出，等同於為了產量接受旋轉件破損風險。

### 3. 排名用固定尺規，不用 min–max 正規化

min–max 是相對比較：候選方案在某個準則上差距很小時，正規化仍會把最好的拉到 1、最差的壓到 0，
**無中生有地製造鑑別力** —— 一個 0.15 的實質差距可以整碗端走 0.30 的權重。
改用固定尺規後，分數有絕對意義，跨情境、跨執行都代表同一件事。

同理，「需要人工核准」只扣 0.1 分。它是治理程序，不是安全缺陷；
早期版本扣到 0.4，結果系統會為了避開核准流程而偏好「不用人簽名但其實比較糟」的方案。

### 4. 驗證比的是反事實，不是事故前的狀態

設備劣化是進行式的。拿 30 分鐘後的結果去比事故剛發生時的產線達成率，**任何方案都必定變差**，
那個比較沒有意義。真正該回答的是兩個問題：

1. 實際結果有沒有達到這個方案自己的預測？（預測可信嗎）
2. 有沒有贏過「什麼都不做」？（介入有價值嗎）

而且反事實比較只在「什麼都不做」本身合法時才成立 —— 工安事件本來就是用產能換安全，
那不是失敗，那正是系統該做的事。

### 5. 診斷靠數值指紋，RAG 只提供證據

三份手冊都會提到同樣那四個訊號，純文字相似度的鑑別力很有限。
所以排名由**感測器指紋餘弦相似度（0.75）**主導，加上**歷史先驗（0.15）**與
**文件支持度（0.10）**。RAG 的價值在於產生可引用的 Evidence，而不是決定答案。

信心度另外會被訊號強度壓抑：訊號還微弱時，即使指紋指向某個故障也不該給 90% 信心。
Orchestrator 因此會在信心不足時**先繼續觀察**（`confirm_diagnosis`），不急著動設備。

---

## Demo 腳本（決賽舞台）

```bash
factory-guardian demo bearing-degradation
```

| Step | 畫面 | 系統動作 |
|---|---|---|
| 0 | Factory Health 100、Production 99% | Machine A/B/C 正常運作 |
| 1 | 注入 Bearing Degradation | Simulator 逐步提高 Vibration / Temperature |
| 2 | **Detect** T+3 min | Monitoring Agent 觸發 WARNING（threshold + trend + health） |
| 3 | **Confirm** 續觀察 2 min | 信心度未達 0.65，先累積證據不動設備 |
| 4 | **Diagnose** 軸承劣化 69% | 指紋比對 + 歷史先驗 + 手冊/SOP/案例 Evidence |
| 5 | **Impact** ORD-A001 交期風險 | Knowledge Graph 追出受影響訂單 |
| 6 | **Plan** 4 個方案 | 每個方案在信念模型上乾跑 30 分鐘 |
| 7 | **Safety** PLAN-A BLOCK | 預測振動將達 14.18 mm/s → 硬限制否決 |
| 8 | **Rank** 推薦 PLAN-D | 加權多準則計算，非 LLM |
| 9 | **Approve** 人工核准 | 停機/維修屬高風險動作 |
| 10 | **Execute** 轉單 + 停機 + 維修 + 工單 | Simulator 真的改變狀態 |
| 11 | **Verify** 全部 PASS | 產能達成 97.5%、交期延遲 0、無二次損壞 |

---

## Benchmark：三組對照組

```bash
factory-guardian benchmark --out benchmark.json
```

跑在**相同 seed、相同情境、相同總時長**的孿生體上，所以 KPI 直接可比。
以下是實際執行結果（非預錄）：

### 設備故障情境（bearing / cooling / motor 平均）

| KPI | Baseline A<br>固定門檻告警 | Baseline B<br>偵測即停機 | **Factory Guardian**<br>完整閉環 |
|---|---:|---:|---:|
| 偵測延遲 | 7.3 min | 3.7 min | **3.7 min** |
| 根因診斷正確率 | — | — | **100%** |
| 產能達成率 | 41.3% | 72.1% | **96.8%** |
| 最大交期延遲 | 1,385 min | 3.5 min | **0 min** |
| 設備最終健康度 | 27.3 | 100 | **100** |
| 二次損壞 | **發生** | 未發生 | 未發生 |
| 執行後驗證 | — | — | **通過** |

### 工安情境（hazard-zone）

| KPI | Baseline A | Baseline B | **Factory Guardian** |
|---|---:|---:|---:|
| 產能達成率 | 99.0% | 67.5% | 74.3% |
| **人員危險區曝露** | **88 min** | 1 min | **1 min** |

這一列是整個提案最重要的論點：Baseline A 的產能最漂亮，代價是讓人在運轉的機台旁邊站了 88 分鐘。
Guardian 用 25% 的產能換 87 分鐘的風險曝露 —— **工安是硬限制，不是加權項**。

---

## 前端

```bash
factory-guardian serve      # http://127.0.0.1:8000
```

兩個頁面，共用一套 HMI 設計系統（[`api/static/hmi.css`](factory_guardian/api/static/hmi.css)）。

### `/` — 產線戰情中心

即時 Dashboard，Demo 的主舞台。

- 四格巨型 KPI（健康度／產線達成率／交期延遲／工安狀態），異常時整格反白成紅底
- **2D 產線圖**，兩種視圖可切換，版面都由 `/api/topology` 的製程階段自動推導：
  - `ARCADE`（[`api/static/arcade.js`](factory_guardian/api/static/arcade.js)）——彩色像素風的產線模擬。
    機台是像素設備、輸送帶上跑箱子（帶速＝上游當下 U/HR）、HP 條＝健康度、
    燈號隨 `worst_band` 變色、過熱冒煙、CRITICAL 噴火花、維修中技師進場敲、
    危險區有人闖入會站到機台旁跳驚嘆號，走道上另有兩名巡線人員。
    全部用 `fillRect` 程序化繪製，不載任何圖檔或字型（含自製 3×5 點陣字）
  - `SCHEMATIC` ——原本的黑紅戰情示意圖，俯視佈局、輸送帶流量標數字、
    停機打斜線、CRITICAL 轉紅底、攝影機視角錐；正式截圖與列印用這個
  - 兩者吃同一份 `/api/state`；選擇記在 localStorage。點示意圖上的機台會捲到下方對應的遙測卡
- **CAM-01 工安監視畫面**（[`api/static/camview.js`](factory_guardian/api/static/camview.js)）——
  Safety Agent 的裁決寫著「Camera 偵測到人員進入運轉中危險區」，這塊就是那句話的畫面。
  刻意不放實景照片：照片是死的，人員離開危險區後它不會變，會成為畫面上唯一與資料對不上的東西。
  這裡每一格都綁在 `CameraObservation` 的欄位上——`person_count` 決定畫幾個人、
  `person_in_hazard_zone` 決定他站不站在黃黑警示帶裡（帶子轉紅閃爍）、`ppe_compliant`
  決定戴不戴安全帽、`smoke_detected` 決定冒不冒煙、`fall_detected` 決定躺不躺著，
  疊在人身上的 CV 偵測框標的是 `confidence`。VLM 後端換成真模型時這裡一行都不用改
- 三台機台的感測器遙測，含階梯式 sparkline、LED 節段健康度條
- **六格步驟卡可展開推理**：每一步點開後看得到「看到什麼 → 怎麼算 → 為什麼不是別的 → 結論」。
  診斷那格會把 88% 拆成算式（指紋餘弦 × 0.75 ＋ 歷史先驗 × 0.15 ＋ 文件支持 × 0.10），
  並列出落選候選各自的餘弦值；方案那格拆出六個準則的加權貢獻與落後幅度；
  工安那格逐條列出 `SR-xx` 規則與擋下的理由。全部取自後端算過的數字，前端不補敘述
- Agent 閉環八階段軌跡即時點亮（進行中反白、被擋下轉紅）
- 根因候選信心度條、LLM 敘述、可引用的 Evidence 清單
- 方案矩陣（含 Safety 裁決與加權分數；被 BLOCK 的方案紅底刪除線並列出理由）
- **人工核准對話框**：高風險動作會停在這裡等人按，上下有工業危險斜紋
- 執行後驗證逐項檢查 + `VERIFIED` 鋼印
- 稽核軌跡即時串流（SSE）

操作順序：選情境 → `INJECT FAULT` → `RUN AGENT LOOP` → 核准 → 看驗證結果。

`?nostream=1` 可停用即時串流，用來給提案書截圖或列印。

### `/benchmark` — 對照組報告頁

評審問「憑什麼說你比較好」時打開這頁。每次載入都會**真的重跑**完整 Benchmark
（5 情境 × 3 模式），不是快取也不是預錄，並把工安那一列的論點寫成標題級的大字。

### 設計語彙

走 **Tactical Telemetry（暗色 CRT 終端）**，刻意約束：

| 決定 | 理由 |
|---|---|
| 單一 hazard red 強調色 | 狀態分級靠「強度與反白」（正常暗前景 → 警告紅框 → 危險紅底反白），比紅黃綠三色更接近真實航太 HUD，暗色投影時也更好分辨 |
| terminal green 全站只用一次 | 只給「驗證通過」鋼印。它是整個系統最重要的正向訊號，不該和其他綠色競爭 |
| 沒有圓角、沒有陰影、沒有漸層 | 分隔線一律用 `grid gap:1px` + 對比底色打出髮絲線 |
| 字體全走系統堆疊 | 不下載 web font——競賽場地的網路不能賭 |
| 掃描線 + 雜訊疊層 | 讓畫面不要像剛生成的網頁 |

---

## 主要 API

| 端點 | 用途 |
|---|---|
| `GET /api/state` | 目前工廠快照 + 監測摘要 + KPI（**不含 Ground Truth**） |
| `POST /api/session/inject` | 注入故障情境 |
| `POST /api/session/tick` | 推進模擬 |
| `POST /api/session/run` | 執行完整 Agent 閉環 |
| `GET /api/stream` | SSE 事件串流 |
| `POST /api/session/approve` | 人工核准 / 退回 |
| `GET /api/topology` | 工廠拓撲與 Knowledge Graph |
| `GET /api/policy` | 動作權限表與 Safety 規則清單 |
| `GET /api/knowledge/search?q=` | 手冊 / SOP / 維修紀錄檢索 |
| `GET /api/audit` | 稽核軌跡 |
| `POST /api/benchmark` | 對照組比較 |

`/api/session/*` 就是規格中的 **Simulator API**。真實導入時由 OPC-UA / MQTT / MES Adapter 取代，
Agent 層完全不動 —— 這是「Hardware-agnostic Agentic Factory Operations Architecture」的具體介面。

---

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `OPENAI_API_KEY` | — | 沒設就用離線確定性敘述器 |
| `OPENAI_MODEL` | `gpt-4o-mini` | LLM 模型 |
| `FG_ALLOW_OFFLINE_LLM` | `1` | 設 `0` 可強制要求真實 LLM |
| `FG_REQUIRE_APPROVAL` | `1` | 高風險動作需人工核准 |
| `FG_AUDIT_DIR` | `runs/` | 稽核軌跡輸出目錄 |
| `FG_SEED` | `20260809` | 模擬亂數種子（決定性） |
| `FG_TICK_SECONDS` | `60` | 一個 tick 代表幾秒模擬時間 |
| `FG_NO_DOTENV` | — | 設 `1` 不讀 `.env`（測試用） |

---

## 明確不做（規格 §3.2）

- 不直接控制真實 PLC / CNC
- 不宣稱故障模型可泛化到所有設備 —— Threshold 需由設備商與工廠工程師校準
- 不做完整 MES / SCADA，只模擬必要資料與 API
- **不以 LLM 直接下控制指令**

---

## 專案結構

```
factory_guardian/
├── domain.py           # 領域模型（所有 Agent 共用的語彙）
├── config.py           # 環境設定
├── audit.py            # JSONL 稽核軌跡
├── llm.py              # LLM 介接（含離線確定性敘述器）
├── optimizer.py        # 加權多準則排名（固定尺規）
├── orchestrator.py     # 八階段閉環 + 重試
├── episode.py          # Episode 執行與 KPI 量測
├── benchmark.py        # 三組對照組比較
├── cli.py              # 命令列介面
├── twin/               # Digital Twin：topology / faults / engine / scenarios
├── knowledge/          # Manual / SOP / History + TF-IDF 檢索
├── agents/             # monitoring / diagnosis / production / safety / maintenance / verification / vision
├── policy/             # Policy Engine + Safety 規則
└── api/                # FastAPI + Dashboard
tests/                  # 102 個測試
```

---

## 從競賽 Demo 到真實工廠

| 階段 | 替換 / 新增 | Agent 層是否重做 |
|---|---|---|
| 競賽 MVP | Simulator + Synthetic MES + Demo Manual | 否 |
| PoC 工廠 | 真實 OPC-UA / MQTT Sensor + 歷史維修資料 | 微調工具與模型 |
| Pilot Line | MES / SCADA / CMMS + 權限與 Safety Review | 保留架構，強化治理 |
| Production | 多產線、多設備商、HA / SOC / SLA | 擴充，不改核心閉環 |

替換點都已經是明確的介面：`FactoryTwin`（→ OPC-UA/MQTT Adapter）、
`VisionBackend`（→ YOLO / 真實 VLM）、`KnowledgeBase`（→ 向量資料庫）、
`PolicyEngine`（→ 工廠實際 SOP 與權限系統）。

---

> **SYNTHETIC DEMO DATA** — 本平台之 Sensor / Orders / Maintenance History / Manual
> 皆為競賽用合成資料，不代表任何真實工廠或設備商規格。
> 事件、Agent tool call、排程切換、狀態更新與 KPI 驗證則都是真實執行。
