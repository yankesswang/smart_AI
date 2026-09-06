# Factory Guardian AI

**Agentic AI 智慧工廠自主營運與風險管理平台**
2026 中華電信智慧創新應用大賽｜智慧製造｜競賽 MVP

> 本 repo 另含姊妹專案 AgentGate —— AI Agent 可稽核治理層（2026 競賽・智慧生活組）。
> `agentgate serve` → http://127.0.0.1:8600；`agentgate benchmark` 跑 142 條情境 × 8 條 baseline × 8 指標；
> `agentgate agent-run injection` 讓真的 gpt-4o-mini 客服 Agent 讀一份夾帶指令的 PDF、
> 發出越權工具呼叫、被 G0 攔下；`agentgate business-case` 與 `agentgate external-eval`（AgentDojo）
> 另見 docs/。

> 當設備、產線或工安異常發生時，AI 不只警告，而是自動完成
> **偵測 → 診斷 → 影響分析 → 方案規劃 → 安全檢查 → 人工核准 → 執行/派工 → 驗證恢復**。

本專案依 [`docs/factory_guardian/Factory_Guardian_AI_競賽提案與Demo規格.docx`](docs/) 實作，
是一套可執行、可量測、可稽核的系統，不是簡報。

---

## 60 秒上手

```bash
pip install -e ".[dev]"

factory-guardian demo bearing-degradation     # 舞台 Demo：完整閉環 + 互動核准
factory-guardian benchmark                    # 三組對照組 KPI 比較
factory-guardian serve                        # Web Dashboard → http://127.0.0.1:8000
pytest -q                                     # 986 個測試（含 AgentGate 姊妹專案），離線約 42 秒跑完
```

不需要 OpenAI 金鑰也能跑完整 Demo：沒有金鑰時 LLM 敘述會自動退回**確定性離線敘述器**。
有金鑰（`.env` 的 `OPENAI_API_KEY`）則會用 LLM 產生更自然的說明文字 —— 但 LLM 的角色僅止於此。

---

## 決賽簡報（兩個版本，內容相同）

| 檔案 | 形態 | 適合 |
|---|---|---|
| [`docs/factory_guardian/Factory_Guardian_AI_競賽簡報.pptx`](docs/) | **原版**。每一頁都是獨立繪製的自由版面，圖表與表格是向量圖形 | 直接放映；視覺完全依設計稿，不會被 PowerPoint 版面規則干擾 |
| [`docs/factory_guardian/Factory_Guardian_AI_競賽簡報_版型版.pptx`](docs/) | **版型版**。1 個投影片母片 ＋ 11 種版面配置 ＋ 59 個版面配置區；圖表與表格是**原生 PowerPoint 物件**（可在 PowerPoint 內改資料） | 要在 PowerPoint 裡續編、換色、抽換頁面或交給別人維護 |

兩份都是 15 頁、講稿逐頁嵌入、數字與 `benchmark.json` / `business.json` 同源。
版型版的來源專案在 [`projects/smart_factory_core_20260906/`](projects/)，
版面系統取自 ppt-master 的 `presentation_core`（結構性版型，不帶品牌識別），
配色與字級改用本專案的 `spec_lock.md` 錨點。

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
| Data | [`twin/`](factory_guardian/twin/) | 拓撲、故障模型、模擬引擎、情境、**干擾與現實落差** |
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

### 6. 一個故障可以有多個指紋

單一原型指紋在雙側故障（例如馬達過載 vs 失載、冷卻不足 vs 過度冷卻）上會失去方向：
兩種相反的偏離平均起來，餘弦自然指不到正確答案 —— 這是在
[`docs/factory_guardian/external_validation.md`](docs/factory_guardian/external_validation.md) 的外部資料上實測發現的結構性弱點
（PWF recall 只有 0.388）。`FaultSignature` 因此支援 `alt_prototypes`，比對時對所有原型取最大餘弦；
`motor_overload`（失載）與 `cooling_failure`（冷卻過度）各補了一個手冊來源的第二原型，
`bearing_degradation` 手冊上只有一個方向，行為逐位元不變。外部資料上重跑後 Top-1 從
0.724 升到 **0.821**（詳見該文件 §11）。AI4I 沒有振動訊號，覆蓋不到 `bearing_degradation`
本身——這個缺口由 [`docs/factory_guardian/cwru_validation.md`](docs/factory_guardian/cwru_validation.md)（真實加速規量測的軸承振動
資料集）補上：內圈／滾珠／外圈三類歸因 Top-1 0.994，但同尺寸內的數字幾乎飽和、
偵測任務單一 RMS 門檻的 AUC 還贏過指紋法（1.000 vs 0.982）；真正有資訊量的跨嚴重度轉移測試
（拿壞得明顯的訓練、測剛開始壞的）Top-1 掉到 0.335 ＝ 隨機，因此**不宣稱驗證了早期偵測能力**。
[`docs/factory_guardian/acoustic_validation.md`](docs/factory_guardian/acoustic_validation.md)（DCASE2020 pump，真實工業錄音）
則驗證聲學偵測器：平均 AUC 0.903 vs 官方 baseline 0.726。三條線各自誠實地寫出輸的地方，
不是只報贏的數字。

### 7. 排名要禁得起「權重是你自己訂的」這句追問

六個準則的權重是工程師訂的常數，不是學出來的。所以每次排名之後會再跑一次**權重穩健性掃描**
（[`optimizer.py::robustness_scan`](factory_guardian/optimizer.py)）：固定種子、確定性網格
（每個準則 ±20%）加隨機抽樣，回報推薦方案在多少比例的擾動下不變、第一二名的最小分數差、
以及哪個準則的權重臨界值最容易翻盤排名。純計算，LLM 不參與。

同一份改動也把方案模板從「寫死的參數」改成**方案參數搜尋**：PLAN-B 原本降速比例寫死 0.6，
現在改成掃描 **5 個 derate 變體（0.4 / 0.5 / 0.6 / 0.7 / 0.8）**，每個都在信念模型上乾跑並過一次
Safety 預測規則，只有分數最高且未被 BLOCK 的變體代表家族進入排名。PLAN-C / PLAN-D 同樣掃描
「延後 0 / 10 / 20 分鐘再停機」（先讓機台把手上這批做完）—— 但延後變體目前**不會被選為代表**，
原因有兩層：一是延後窗內機台仍在全速運轉，乾跑出來的訊號峰值一樣要過 SR-02P / SR-03P 預測型規則，
早期偵測的情境下延後越久越容易被預測型規則 BLOCK；二是即使沒被 BLOCK，執行層目前的動作字彙
還沒有「N 分鐘後再停機」，讓一個做不到的變體代表家族，會被 Verification Agent 抓到預測與實際對不上。
掃描過的變體與分數全部留在 `RecoveryPlan.variants` 與稽核軌跡裡，不是算完就丟。

### 8. 監測不只看「現在多壞」，也看「還有多久會踩線」

瞬時斜率回答不了現場真正在意的問題——兩台機器同樣掉到 85 分健康度，一台 12 分鐘後踩線、
一台 50 分鐘後才踩線，短期風險並不一樣。Monitoring Agent 因此新增 **TTT（time-to-threshold，
到達門檻的剩餘時間）預測**（[`prediction/threshold.py`](factory_guardian/prediction/threshold.py)）：
只讀 Agent 自己累積的可觀測歷史（和 `/api/state.history` 同一種東西，不讀孿生體的故障標籤），
優先用時序模型換算成分鐘、拿不到結果時退回線性外推。TTT 進了 `failure_risk` 的閉環，
但只是加權項（最多 +0.35），不是主導項——它是外推值，不該蓋過已經量到的健康度與趨勢。
Safety Agent 讀 `last_ttt` 當佐證證據，但裁決仍然由規則引擎做。

### 9. 觀察窗與誤報，兩個要付代價才買得到的東西

動設備前的**觀察窗**（`Orchestrator.confirm_diagnosis`，等於 `MonitoringAgent.WINDOW`）要求
系統多看一個完整視窗，才能分辨「訊號跳到新穩態」和「才剛開始的劣化」——這幾分鐘不是保守，
是資訊在視窗跑完之前根本不存在。代價是實測的：關掉觀察窗，MTTD 從 10.7 分鐘降到 5.0 分鐘、
產能達成率從 94.6% 升到 96.9%（見下方 Benchmark）。

第二道機制是**誤報棄權**（`Orchestrator._abstain_reason()`）：診斷自己說沒有設備故障徵兆，
或異常已經停在一個新穩態（一個完整視窗內健康度掉不到 3 分、最壞趨勢低於門檻），
系統會主動放棄處置——告警照發、監控照跑，但不改變任何設備狀態。兩個判準都只在**沒有工安風險**
時成立；人在危險區裡的時候，「再看看」不是一個選項。棄權不是萬能：`fp-warm-up` 這個誤報情境裡
系統依然誤動作了 3 次（詳見下方 Benchmark），因為分辨它需要比目前的觀察窗再多看 4 分鐘，
而那 4 分鐘對真實故障是有代價的。

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
| 3 | **Confirm** 續觀察 6 min | 動設備前要求「證據夠」且「還在惡化」——\n觀察窗 = 監測視窗長度，少於它就分不出「新穩態」和「才剛開始的劣化」 |
| 4 | **Diagnose** 軸承劣化 88% | 指紋比對 + 歷史先驗 + 手冊/SOP/案例 Evidence |
| 5 | **Impact** ORD-A001 交期風險 | Knowledge Graph 追出受影響訂單 |
| 6 | **Plan** 4 個方案 | 每個方案在信念模型上乾跑 30 分鐘 |
| 7 | **Safety** PLAN-A BLOCK | 預測振動將達 14.18 mm/s → 硬限制否決 |
| 8 | **Rank** 推薦 PLAN-D | 加權多準則計算，非 LLM |
| 9 | **Approve** 人工核准 | 停機/維修屬高風險動作 |
| 10 | **Execute** 轉單 + 停機 + 維修 + 工單 | Simulator 真的改變狀態 |
| 11 | **Verify** 全部 PASS | 產能達成 97.5%、交期延遲 0、無二次損壞 |

---

## Benchmark：四組對照組

```bash
factory-guardian benchmark --out benchmark.json
```

跑在**相同 seed、相同情境、相同總時長**的孿生體上，所以 KPI 直接可比。
以下是實際執行結果（非預錄）。每一個對照組的定義、每一個情境的注入參數、
以及為什麼 Baseline C 才是該比的對象，逐項寫在 [`docs/factory_guardian/benchmark_notes.md`](docs/factory_guardian/benchmark_notes.md)。

| 對照組 | 它代表什麼 |
|---|---|
| **A**｜固定門檻告警 | 感測器踩到**危險**門檻就告警，之後什麼都不做。「告警完全沒被接住」的極端 |
| **B**｜偵測即停機 | 偵測到就停機維修，技師零等待。「告警完全被接住」的極端 |
| **C**｜**現行流程** | 告警 → **人工判定根因 90 分鐘**（機台照跑、照劣化）→ 停機維修 → 復機。不轉單 |
| **G**｜Factory Guardian | 跨 Machine / Production / Safety 的完整閉環，執行後回到孿生體驗證 |

**為什麼加 Baseline C**：A 和 B 都是理想化的極端，沒有工廠長那樣。
拿 41% 對 95% 當開場，第一個追問就是「哪家工廠是這樣運作的？」。
C 把現行流程真正花時間的那一段放進模擬 —— 人工判定根因的 90 分鐘
（來源是 [`docs/factory_guardian/business_case.md`](docs/factory_guardian/business_case.md) §3 假設參數表，ROI 模型引用的是同一個數字）。

### 設備故障情境（bearing / cooling / motor 平均）

| KPI | Baseline A | Baseline B | **Baseline C**<br>現行流程 | **Factory Guardian** |
|---|---:|---:|---:|---:|
| 偵測延遲 | 7.3 min | 4.7 min | 7.3 min | **4.7 min** |
| 診斷確認（MTTD） | — | — | 40.0 min | **10.7 min** |
| 產能達成率 | 41.3% | 72.1% | 54.2% | **94.6%** |
| 最大交期延遲 | 1,385 min | 3.6 min | 24.5 min | **0 min** |
| 設備最終健康度 | 27.3 | 100 | 100 | **100** |
| 二次損壞 | **發生** | 未發生 | **發生** | 未發生 |
| 單位產出能耗 | 0.75 kWh/件 | 0.44 | 0.63 | **0.33** |
| 人工介入 | 0 | 1 | 1 | **0** |
| 執行後驗證 | — | — | — | **通過** |

Baseline C 最刺眼的一格不是產能，是**時間**：帳面上的 90 分鐘判定流程，
三個情境裡一次都沒跑完 —— 平均在第 40 分鐘就被打斷，因為**故障自己揭曉了**
（軸承咬死／馬達燒毀，機台自己停下來）。人工判定的價值在這裡是負的。
Guardian 的 10.7 分鐘裡有 6 分鐘是**刻意**的觀察窗：把它關掉，MTTD 回到 5.0 分鐘、
產能回到 96.9%（實測）—— 那 2.3 個百分點買到的是下面兩節的東西。

### 診斷正確率：不再是循環論證的 100%

原本的 100% 有一個誠實問題：Diagnosis Agent 比對的手冊指紋，和 Simulator 生成訊號用的
`FaultModel.deltas` 是**同一組數字**。手冊怎麼寫，機台就怎麼壞 —— 那不是量測，那是同義反覆。

所以孿生體加了可設定的**現實落差**（[`twin/disturbances.py`](factory_guardian/twin/disturbances.py)）：
`bearing-atypical` 的軸承劣化只表現出手冊 15% 的振動、160% 的溫升；
`cooling-with-stuck-vibration` 的振動感測器卡在 3.8 mm/s。
`fork_as_belief()` 會把這些落差**全部清掉** —— 規劃永遠跑在手冊物理上，因為 Agent 只知道手冊。

| 診斷 KPI（6 個設備故障情境） | Factory Guardian |
|---|---:|
| **初次**診斷正確率（偵測當下的第一次比對） | **83.3%**（5/6） |
| **最終**診斷正確率（觀察 + 必要時重規劃之後） | **100%** |
| 平均診斷信心度 | 0.77 |
| 重規劃次數 | 0 |

只看兩個現實落差情境，初次正確率是 **50%**、信心度掉到 **0.66**。
`bearing-atypical` 的稽核軌跡把過程攤開來：偵測當下感測器指紋餘弦是
**冷卻失效 0.900 對軸承 0.765**，第一個答案是錯的；信心度只有 0.50，
低於動設備門檻，於是閉環繼續觀察 —— 十分鐘後聲學指紋與趨勢把答案糾正回軸承。
**那 16 分鐘的 MTTD 買到的就是這個。**

> **這裡有一件沒做到的事，直接寫出來：** 目前沒有任何一個情境走到
> 「驗證失敗 → 重新規劃」。在這些落差下，方案排名仍然選中「轉單 + 停機維修」，
> 而停機維修對診斷錯誤是**容錯**的 —— 機台停下來就不會再劣化。
> 重試路徑本身有測試獨立守著（`test_orchestrator.py::test_a_failed_verification_triggers_a_replan_and_the_recovery_is_timed`，
> 直接讓第一次驗證失敗），KPI（重規劃次數、驗證失敗後恢復時間）也已經接上報表；
> 但**它在 Benchmark 上的值目前是 0 與 —**，我們不會把它寫成已經發生的事。

### False Positive：4 個「機台完全健康」的干擾情境

規格 §10 列了 False Positive，但如果每個情境都真的有故障，那一格永遠是空的 ——
不是零誤報，是沒有機會誤報。所以加了四個 Ground Truth 為空的干擾：
**感測器單點尖峰、換料重啟突波、換規格負載切換、冷機暖機過衝**。

| 誤報 KPI（4 個無故障情境） | Baseline A | Baseline B | Baseline C | **Factory Guardian** |
|---|---:|---:|---:|---:|
| 誤報情境數 | 4 / 4 | 2 / 4 | 4 / 4 | **2 / 4** |
| 誤報率 | 0.80 次/小時 | 0.40 | 0.80 | **0.40** |
| **誤報後動到設備** | 0 | **4** | 0 | **3** |
| 主動棄權（只告警、不動設備） | 0 | 0 | 0 | **1** |
| 技師出動（每趟 90 分鐘） | 0 | 2 | **4** | **0** |
| 產能達成率 | 95.4% | 79.1% | 95.4% | 93.7% |

兩件事值得說清楚：

1. **誤報的成本不在告警，在誤動作。** 這四個情境裡機台完全健康，
   所以「偵測即停機」白停了 4 次、產能掉到 79.1%；現行流程沒有停機，
   但派了 4 趟技師、每趟 90 分鐘。Guardian 少報一半（門檻要求越界在最近 3 個取樣裡出現 2 次，
   單點尖峰與啟動突波因此被擋掉），並且在負載切換那一格**主動棄權**：
   > 觀察滿 6 個取樣後，健康度僅變化 +0.5 分、最壞趨勢 +0.049/tick ——
   > 訊號停在新的穩態而非持續惡化，比較可能是製程或負載改變。僅告警並持續監控，不動設備。

2. **Guardian 在暖機那一格誤動作了 3 次（轉單 + 停機 + 維修），這一格我們不打算辯護。**
   溫度連續 8 分鐘高於危險門檻並持續上升，在那個當下它和早期冷卻失效沒有任何
   可觀測的差別。要分辨它需要再多看 4 分鐘 —— 而那 4 分鐘對真實故障是有代價的。
   我們選了 6 分鐘的觀察窗，代價與收益都攤在上面兩張表裡。

### 工安情境（hazard-zone）

| KPI | Baseline A | Baseline B | Baseline C | **Factory Guardian** |
|---|---:|---:|---:|---:|
| 產能達成率 | 99.0% | 67.6% | 99.0% | 74.3% |
| **人員危險區曝露** | **88 min** | 1 min | **88 min** | **1 min** |

Baseline C 在這一列和 Baseline A 一模一樣，這是刻意的：
**現行流程沒有一雙一直盯著危險區的眼睛。** 它的工安控制是程序性的
（SOP、圍籬、教育訓練、定期巡檢），不是偵測性的。把 AI 攝影機送給對照組，
等於假設現況已經有了我們要新增的那個能力 —— 那個 Benchmark 就不誠實了。

這一列是整個提案最重要的論點：A 與 C 的產能最漂亮，代價是讓人在運轉的機台旁邊站了 88 分鐘。
Guardian 用 25% 的產能換 87 分鐘的風險曝露 —— **工安是硬限制，不是加權項**。

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
（11 情境 × 4 模式），不是快取也不是預錄，並把工安與誤報那兩列的論點寫成標題級的大字。
情境分三族分開彙總：設備故障、無故障干擾（誤報）、現實落差（初次 vs 最終診斷）——
混在一起平均，誤報率會被有故障的情境稀釋，產能會被沒事發生的情境拉高。

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

## TabFM 時序預測

Dashboard 的「預測」分頁會把現有遙測歷史轉為 supervised temporal table：
`value / lag_1 / lag_3 / rolling mean / rolling std / slope / factory KPI`，再預測指定機台的
health、temperature、vibration、current 或 rpm。資料來源仍是 Agent 可見的 `/api/state.history`，
不會讀取 Digital Twin 的故障標籤或 `fault_progress`。

Runtime 採 adapter 設計：

- `auto`：TabFM 可用時走官方 `TabFMRegressor`，否則明確降級為 Ridge 時序基線。
- `tabfm`：官方 [google-research/tabfm](https://github.com/google-research/tabfm)；目前需 Python 3.11+，依官方方式從原始碼安裝 backend。
- `ridge`：無額外 ML dependency 的確定性基線，供本機 Demo、CI 與模型服務故障時使用。

```bash
# 另建 Python 3.11+ 的模型環境；CPU 可選 JAX 或 PyTorch backend
git clone https://github.com/google-research/tabfm.git
cd tabfm
pip install -e '.[pytorch]'

# Factory Guardian 會 lazy-load，不安裝也不影響監控與 Agent 閉環
export FACTORY_GUARDIAN_TABFM_BACKEND=pytorch
```

> TabFM 程式碼是 Apache-2.0，但官方 v1.0.0 預訓練權重為
> `tabfm-non-commercial-v1.0`，只允許非商業、非 production 使用。正式商用部署需替換為
> 具合適授權的自有 checkpoint／模型服務；API 與前端不需改動。

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
| `GET /api/prediction/models` | 可用模型、runtime 狀態、授權提示與預測目標 |
| `POST /api/prediction/forecast` | 指定機台、目標、horizon 與模型執行時序預測 |

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
| `FACTORY_GUARDIAN_TABFM_BACKEND` | `pytorch` | 官方 TabFM adapter 使用 `pytorch` 或 `jax` backend |

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
├── optimizer.py        # 加權多準則排名（固定尺規）＋ 權重穩健性掃描
├── orchestrator.py     # 八階段閉環 + 重試
├── episode.py          # Episode 執行與 KPI 量測
├── benchmark.py        # 四組對照組（A/B/C/Guardian）× 11 情境比較
├── cli.py              # 命令列介面
├── twin/               # Digital Twin：topology / faults / engine / scenarios / energy
│   └── disturbances.py #   干擾與現實落差（拆掉「手冊怎麼寫機台就怎麼壞」的循環論證）
├── knowledge/          # Manual / SOP / History + TF-IDF 檢索
├── agents/             # monitoring / diagnosis / production / safety / maintenance / verification / vision
├── policy/             # Policy Engine + Safety 規則
├── prediction/         # TabFM/Ridge 時序預測 adapter
│   └── threshold.py    #   TTT（到達門檻剩餘時間）估計，Monitoring Agent 用它算 failure_risk
├── validation/         # 外部資料驗證：獨立於 agents/ 之外的等價實作，不 import 對方
│   ├── fingerprint.py  #   感測器指紋餘弦法的獨立實作（AI4I 驗證用）
│   └── cwru.py         #   CWRU 軸承振動資料集驗證（真實加速規量測）
├── deployment/         # Edge/Cloud 分層、頻寬預算、MEC 斷網續跑模擬
├── business/           # ROI 模型、假設參數、定價、競品比較
├── stage/              # 決賽舞台 Demo 劇本與導播
└── api/                # FastAPI + Dashboard
tests/                  # 986 個測試（含 AgentGate 姊妹專案）
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
