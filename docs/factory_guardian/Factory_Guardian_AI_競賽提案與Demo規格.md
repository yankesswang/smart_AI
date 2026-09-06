__FACTORY GUARDIAN AI__

__Agentic AI 智慧工廠自主營運與風險管理平台__

2026 中華電信智慧創新應用大賽｜智慧製造｜競賽提案與 Demo 規格整合版

| 核心主張<br>把「設備維護、產線異常處理、工安風險」從三套分離系統整合成一條可觀測、可決策、可執行、可驗證的 Agentic AI 閉環。競賽 MVP 不假裝接真實工廠，而以可控制的 Factory Digital Twin 驗證整套架構。 |
| --- |

| 整合範圍 | 一句話 |
| --- | --- |
| Machine｜設備 | 監測健康、預測故障、診斷根因、產生維修工單 |
| Production｜生產 | 分析訂單與產線影響，產生替代排程與復原方案 |
| People｜工安 | 以 VLM + Sensor 判斷人員、環境與設備安全風險 |

文件版本：2026\-08\-09

> **本文件為 8/9 規格，數字已於 2026-09-06 依實測同步；提案書見 [`Factory_Guardian_AI_提案書.md`](Factory_Guardian_AI_提案書.md)。**
> 本文件保留原始規格結構做為技術規格參考；15 頁正式提案內容、量化成果與商業模式以提案書為準。

# __1. 競賽定位與提案摘要__

本案對應「智慧製造」：以 AI 與數據平台整合機聯網、產線戰情中心、AI 視覺檢測、工安與預測維護。Factory Guardian AI 將這些能力整合為單一 Agentic AI 工廠營運平台。

| 一句話定位<br>當設備、產線或工安異常發生時，AI 不只警告，而是自動完成「偵測 → 診斷 → 影響分析 → 方案規劃 → 安全檢查 → 人工核准 → 執行/派工 → 驗證恢復」。 |
| --- |

__1.1 為什麼要整合三個 Agent__

| 原始能力 | 單獨做的限制 | 整合後價值 |
| --- | --- | --- |
| Predictive Maintenance | 只知道機台可能壞 | 連結訂單、產能與維修決策 |
| Factory Copilot | 只處理排程，不一定理解設備原因 | 可依故障根因與替代機台重新規劃 |
| Industrial Safety | 容易變成單純 CV 告警 | Safety Agent 成為所有行動的政策閘門 |

__1.2 競賽 MVP 的可信度原則__

- 不宣稱已串接真實工廠、PLC 或 MES；所有 Synthetic / Simulation Data 明確標示。
- 建立可重複的 Factory Digital Twin，讓 Sensor、故障、訂單與機台狀態真的隨事件改變。
- Agent 不能直接讀取 Ground Truth；只能根據 Sensor、Manual、歷史案例與規則推論。
- 執行後必須回到 Simulator 驗證結果，而不是停在「LLM 建議」。

# __2. 問題定義__

多數工廠的設備維護、生產管理與工安監控彼此分離。當設備異常時，工程師通常需要跨 Sensor、機台告警、Manual、維修紀錄、MES、工安規範與排程人工判斷，導致診斷時間長、協作成本高，且不同目標容易衝突。

| 面向 | 現況 | Factory Guardian AI |
| --- | --- | --- |
| 設備 | 異常後人工查 Error Code、手冊與維修紀錄 | 自動診斷根因、預估風險、建立工單 |
| 生產 | 停機後才人工重排訂單與替代機台 | 即時計算訂單、產能、交期與替代方案 |
| 工安 | Camera / Sensor 各自告警 | Safety Agent 將人員與環境風險納入每個決策 |
| 治理 | AI 建議與實際操作斷裂 | Policy + Human Approval + Audit + Verify |

| 核心衝突<br>工廠的最佳決策不是「讓機器繼續跑」，而是在 Safety、Production、Cost、Recovery Time 之間找到可執行的最佳方案。 |
| --- |

# __3. 系統目標與範圍__

__3.1 核心目標__

- 縮短異常發現與診斷時間。
- 將設備故障影響映射到產線、訂單與交期。
- 讓工安規則成為任何 Agent 行動的硬限制。
- 以多方案比較取代單一 LLM 建議。
- 所有執行行動都必須有 Verification Loop。

__3.2 明確不做__

| 不做項目 | 原因 |
| --- | --- |
| 不直接控制真實 PLC / CNC | 競賽階段沒有工廠權限與安全認證 |
| 不宣稱故障模型可直接泛化所有設備 | 實際 Threshold 與模型需由設備商/工廠校準 |
| 不做完整 MES / SCADA | MVP 只模擬必要資料與 API |
| 不以 LLM 直接下控制指令 | 高風險操作由 Policy / Rule / Human Approval 限制 |

# __4. 功能規格（Functional Spec）__

__4.1 Monitoring & Predictive Maintenance__

- 即時監控 Temperature、Vibration、Current、RPM、Machine State、Production Rate。
- 建立 Equipment Health Score、異常趨勢與 Failure Risk。
- 以 Time Series / Rule / Anomaly Detection 觸發事件。

__4.2 Diagnosis Agent__

- 整合 Sensor、Error Code、Demo Equipment Manual、Maintenance History、SOP。
- 輸出 Root Cause 候選、信心度與 Evidence。
- 不得讀取 Simulator 的真實故障標籤。

__4.3 Production Impact & Planning Agent__

- 建立 Machine → Line → Product → Order 的依賴關係。
- 估算 Downtime、Order Delay、Production Loss、替代機台容量。
- 至少產生 2–3 個 Plan，使用可驗證的規則/最佳化引擎比較。

__4.4 Industrial Safety Guardian__

- VLM / CV：PPE、Hazard Zone、跌倒/危險動作等。
- Environmental Sensor：高溫、煙霧、有害氣體等。
- Safety Policy：高風險條件下阻止「為了產量繼續運轉」。

__4.5 Maintenance & Work Order__

- 自動產生設備、問題、Evidence、Priority、Required Skill、Suggested Parts、Estimated Repair。
- 對高風險維修動作保留人工核准與稽核。

# __5. 多 Agent 與系統架構__

| Detect | Diagnose | Impact | Plan | Safety | Approve | Execute | Verify |
| --- | --- | --- | --- | --- | --- | --- | --- |

| Agent | 主要責任 | 主要輸入 | 主要輸出 |
| --- | --- | --- | --- |
| Monitoring Agent | 監測/異常觸發 | Sensor、Machine State、Camera | Anomaly Event |
| Diagnosis Agent | 根因分析 | Sensor、Manual、History | Root Cause + Evidence |
| Production Agent | 影響與調度 | Orders、Capacity、Machine State | Plans + KPI |
| Safety Agent | 風險與政策檢查 | VLM、Sensor、SOP | PASS / BLOCK / Approval |
| Maintenance Agent | 維修任務 | Diagnosis、Parts、SOP | Work Order |
| Orchestrator | 協調流程與重試 | 所有 Agent 狀態 | Execution + Verification |

__5.1 技術分層__

| Layer | 元件 |
| --- | --- |
| Data | Digital Twin、MQTT / OPC-UA、Camera、Orders、Manual、History |
| AI | Time Series、VLM、LLM、RAG、Knowledge Graph、Optimization |
| Agent | Monitoring / Diagnosis / Production / Safety / Maintenance / Orchestrator |
| Governance | Policy Engine、Permission、Human Approval、Audit Log |
| Action | Simulator API、Work Order、Schedule Update、Alert、Dashboard |

# __6. 無真實工廠時的 Demo 設計__

| Demo 定位<br>不是「用假資料展示 AI」，而是建立一座可控制、可注入故障、可執行動作、可量化驗證的 Factory Digital Twin。 |
| --- |

__6.1 Digital Twin MVP__

| 設備 | 角色 | 核心欄位 |
| --- | --- | --- |
| Machine A | 主要加工機台 | Temperature / Vibration / Current / RPM / Health / Rate |
| Machine B | 替代加工機台 | Capacity / Current Load / Rate / State |
| Machine C | 後段包裝機台 | Queue / Rate / State |

基本依賴：Machine A / B → Machine C → Order。當 A 異常時，Production Agent 才能真正計算轉移 B 後的產能與交期。

__6.2 事件與 Fault Injection__

| Scenario | Sensor Pattern | Ground Truth |
| --- | --- | --- |
| Bearing Degradation | Vibration ↑、Temperature ↑、Current 小幅 ↑ | 軸承劣化 |
| Cooling Failure | Temperature ↑↑、Vibration 正常、Current 正常 | 冷卻失效 |
| Motor Overload | Current ↑↑、Temperature ↑、RPM ↓ | 馬達過載 |
| Hazard Zone | Camera 偵測人員進入運轉設備危險區 | 工安事件 |

Fault Injection 只控制 Simulator 的狀態與 Sensor 生成規則，不把故障標籤傳給 Agent。如此才能對 Diagnosis Accuracy 做真實評估。

# __7. Demo 資料規格__

__7.1 Sensor Specification（競賽 Demo 假設值）__

| Signal | Normal | Warning | Critical |
| --- | --- | --- | --- |
| Temperature | < 70°C | 70–80°C | > 80°C |
| Vibration | < 4 mm/s | 4–7 mm/s | > 7 mm/s |
| Current | 8–12 A | 12–14 A | > 14 A |
| RPM | 95–100% target | 85–95% | < 85% |

| 重要標示<br>以上 Threshold 僅作為競賽 Digital Twin 規格，不宣稱代表任何特定品牌或真實設備。實際導入需依設備商 Specification、歷史資料與工廠工程師校準。 |
| --- |

__7.2 其他 Demo Data__

| 資料 | MVP 規模 | 來源/方式 |
| --- | --- | --- |
| Maintenance History | 20–50 筆 | 自建 Synthetic cases；標示 synthetic |
| Equipment Manual / SOP | 3–5 份 | 自建 Demo Manual，對應三種故障 |
| Orders | 10–20 筆 | MES-like synthetic orders |
| Safety Video | 3–5 段 | 預錄自建場景或授權測試素材 |
| Fault Ground Truth | 每 scenario 已知 | Simulator 內部標籤，不暴露給 Agent |

# __8. 決賽 Demo 主 сценарio：Machine A 軸承劣化__

> **本節數字全部來自 `benchmark.json` 的 `bearing-degradation` 情境（2026-09-06 實測，seed 20260809），
> 不再是規格階段的假設值。** 對照組定義見 [`benchmark_notes.md`](benchmark_notes.md)，
> 四組對照組跑在相同種子、相同情境、相同總時長的孿生體上。

| Step | 舞台畫面 | 系統動作 | 實測依據（`bearing-degradation`） |
| --- | --- | --- | --- |
| 0. Normal | Factory Health 100、Production 99% | Machine A/B/C 正常運作 | README Demo 腳本 Step 0 |
| 1. Inject | 按下 Bearing Degradation | Simulator 逐步提高 Vibration / Temperature | t=2 起 ramp 10（`twin/scenarios.py`） |
| 2. Detect | Monitoring Agent 觸發 WARNING（threshold + trend + health 三重觸發） | 偵測延遲 **4.0 min** | Guardian `detection_latency_min=4.0`（Baseline A/C 為 8.0 min） |
| 3. Confirm | 續觀察 6 個 tick（觀察窗） | 分辨「新穩態」與「才剛開始的劣化」 | `MonitoringAgent.WINDOW`；診斷確認（MTTD）**10.0 min** |
| 4. Diagnose | 軸承劣化 **88%** + Evidence | 讀 Sensor / Manual / History，指紋餘弦 0.75 ＋ 歷史先驗 0.15 ＋ 文件支持 0.10 | Guardian `diagnosis_confidence=0.88` |
| 5. Impact | Order A001 delay risk ↑ | Production Agent 計算影響 | `twin/topology.py` 依賴圖 |
| 6. Plan | PLAN-A 維持全速／PLAN-B 降速（5 個 derate 變體）／PLAN-C 停機維修／PLAN-D 轉單至 Machine B | 每個家族在信念模型上乾跑並掃描參數，比較 Safety / Delay / Cost / Recovery | `agents/production.py` 方案參數搜尋 |
| 7. Safety | PLAN-A BLOCK：預測振動由 5.65 mm/s 升至 14.18 mm/s，超過危險門檻 7 mm/s | Safety Agent 預測型規則 SR-02P 否決 | README「關鍵設計決定 2」 |
| 8. Approve | Human Approval | 核准轉單至 Machine B + 維修 A（PLAN-D） | Policy Engine 高風險動作規則 |
| 9. Execute | Order A001: A → B；A → Maintenance | Simulator 真正改變狀態 | `episode.py` |
| 10. Verify | 產能達成 **95.4%**（Guardian）vs **39.5%**（Baseline A，同種子同時長）；交期延遲 **0 min** vs **1,385 min**；設備健康度 **100** vs **0.7** 且**發生二次損壞** | Verification Agent 在真實孿生體上重新量測 | `benchmark.json` bearing-degradation 四模式對照 |

| 舞台重點<br>三個原始功能不是三段 Demo，而是同一個事故裡的三個面向：設備為什麼壞、訂單怎麼救、這個方案安不安全。 |
| --- |

> **與 8/9 版的差異**：原表 Step 2/3/9 用的「Health 92→61」「82%」「62%→94%」「42→8 min」是規格階段
> 尚未實測前的假設佔位值，2026-09-06 六條工程工作流（觀察窗、誤報棄權、方案參數搜尋、權重穩健性
> 掃描、TTT 預測、現實落差）完成後已用 `benchmark.json` 的真實模擬輸出取代。

# __9. 決策與方案比較規格__

| Plan | Production Impact | Safety | Recovery | 結果（2026-09-06 實測，bearing-degradation） |
| --- | --- | --- | --- | --- |
| PLAN-A｜繼續全速 | 最低 | High | 0 min | Safety Agent BLOCK（SR-02P：預測振動 14.18 mm/s > 7 mm/s） |
| PLAN-B｜降速運轉（5 個 derate 變體 0.4–0.8）| 中 | 中 | 延後至換班 | 掃描後取最高分且未被 BLOCK 的變體代表家族 |
| PLAN-C｜立即／延後停機維修（0/10/20 min 延後變體）| High | Low | 約 16 min（recovery_min 實測） | 延後變體若在延後窗內預測仍超過危險門檻，會被 SR-02P/SR-03P 預測型規則 BLOCK，執行層目前也尚不支援延後停機動作，故只有「立即」變體可代表家族 |
| PLAN-D｜轉單至 Machine B + 停機處置 A | Low | Low | 約 16 min | **推薦**（六準則加權分數最高且未被 BLOCK） |

排名結果附**權重穩健性掃描**：推薦方案在權重 ±20% 擾動下是否維持不變、第一與第二名的分數差、
最敏感準則與臨界權重（`optimizer.py::robustness_scan`），回答「權重是你自己訂的，換一組會不會換人」。
LLM 負責理解事件、整理 Evidence 與協調 Agent；最終 Plan 排名由規則引擎（固定尺規加權）計算，
LLM 不參與排名與信心度。

__9.1 Human\-in\-the\-loop 規則__

| Action | MVP Policy |
| --- | --- |
| 產生告警 / 工單 | 可自動 |
| 更新模擬排程 | 低風險可自動或單鍵核准 |
| 停止機台 / 修改控制狀態 | 人工核准 |
| Safety Override | 禁止 |

# __10. 驗證 KPI 與 Benchmark__

| 類別 | KPI | 比較方式 |
| --- | --- | --- |
| Diagnosis | Detection Latency / Diagnosis Accuracy / False Positive | Ground Truth vs Agent |
| Maintenance | Mean Time To Diagnose / Work Order Completeness | Baseline manual-rule vs Agent |
| Production | Production Loss / Order Delay / Recovery Time | No-agent baseline vs Agent plan |
| Safety | Hazard Detection / Unsafe Plan Block Rate | 測試影片 + Policy test cases |
| Agent | Task Completion / Tool Success / Human Intervention / Decision Latency | 每次 scenario trace |

__10.1 建議至少做的對照組（2026-09-06：已擴充為四組，見 `factory-guardian benchmark`）__

- Baseline A：固定門檻告警，打在原始瞬時讀值上，之後什麼都不做。「告警完全沒被接住」的極端。
- Baseline B：偵測即停機，技師零等待。「告警完全被接住」的極端。
- **Baseline C（現行流程，2026-09-06 新增）**：同 A 的固定門檻偵測（無攝影機）→
  告警後**人工判定根因 90 分鐘**（機台照跑、照劣化；若判定期間發生二次損壞則立刻結束判定、
  直接進維修）→ 停機維修 → 復機，**不轉單**。90 分鐘取自
  [`business_case.md`](business_case.md) §3 假設參數表，ROI 模型與 Benchmark 引用同一個數字。
  A 與 B 都是理想化的極端，沒有工廠長那樣運作；C 才是評審會問「哪家工廠是這樣」時能站得住的對照組。
- Factory Guardian：跨 Machine / Production / Safety 做完整閉環，執行後回到孿生體驗證。

**量化成果應以 Guardian vs Baseline C 為主要對照**，A／B 只用來標定兩個理論極端（見提案書第 11 頁）。

# __11. MVP Scope 與開發優先序__

> **2026-09-06 完成度標記**：以下逐項標示 done／partial／not done，誠實對應「團隊與路線圖」頁。

| 層級 | 必做內容 | 狀態 |
| --- | --- | --- |
| P0｜一定要有 | 3 台虛擬設備、3 Sensor、3 故障、1 條 Order dependency、Fault Injection、完整 Agent Loop、KPI Verification | **done**（`factory_guardian/twin/`、`episode.py`；986 個測試涵蓋） |
| P1｜加分 | 1 個 Safety Camera / VLM 情境 | **partial** —— CAM-01 的 `CameraObservation`（人員數、危險區、PPE、煙霧、跌倒、confidence）已完整實作並驅動 Safety 裁決與 Dashboard 畫面，但 VLM 後端目前是**腳本化 / 合成觀測**，尚未接上會讀真實影像的模型（`agents/vision.py` 的介面已預留） |
| P1｜加分 | Knowledge Graph | **done**（`twin/topology.py` 依賴圖 ＋ `knowledge/` Manual/SOP/History TF-IDF 檢索） |
| P1｜加分 | Plan Optimization | **done，且超出原規劃** —— 固定尺規加權排名（`optimizer.py`）之外，2026-09-06 新增**方案參數搜尋**（PLAN-B 五個 derate 變體、PLAN-C/D 停機延後變體）與**權重穩健性掃描**（`robustness_scan`） |
| P1｜加分 | Audit Trace | **done**（`audit.py` JSONL，一次閉環 27 筆／21,575 bytes 實測，見 `deployment/budget.py`） |
| P2｜Bonus | ESP32 + 小馬達 + 溫度/震動 Sensor 的桌上型 Mini Machine | **not done** —— 單人參賽時間有限，優先把 Digital Twin 閉環與外部資料驗證做完整；桌上實體機列入賽後路線圖 |

**超出原始 P0/P1/P2 規劃、2026-09-06 前已完成的加分項**（詳見提案書第 15 頁）：
Baseline C 現行流程對照組、TTT（到達門檻剩餘時間）預測進閉環、
三條外部資料驗證（AI4I 2020 根因歸因、DCASE2020 聲學偵測、CWRU 軸承振動）、
TabFM／Ridge 時序預測、商業案例三情境敏感度與破口分析。

__11.1 建議技術棧__

| 需求 | 建議工具 |
| --- | --- |
| Simulator / API | Python FastAPI + 狀態機 / discrete simulation |
| Telemetry | MQTT（Mosquitto）或簡化 REST/WebSocket；可選 OPC-UA 模擬 |
| Time Series | Python + rule/anomaly model |
| VLM / CV | 現有 VLM 或 YOLO 類模型；MVP 僅 1–2 種事件 |
| RAG | Manual / SOP / Maintenance History |
| Agent | 多 Agent orchestration + tool calling |
| Dashboard | Web Dashboard + 即時 KPI / Agent trace |

# __12. 與中華電信的業務連結與商轉__

本案不是把中華電信當作單純雲端供應商，而是把 5G 專網、Edge、Cloud、資安與企業 ICT 整合成 Smart Factory AI Managed Service。

| CHT 能力 | 在 Factory Guardian 的角色 |
| --- | --- |
| 5G 專網 | 設備、Camera、AGV 與 Edge 的低延遲連線 |
| Edge Computing | VLM / Sensor AI 的現場即時推論 |
| Cloud | Factory Data、Knowledge Base、Agent Platform 與集中管理 |
| 資安 / SOC | OT / IoT / API / Agent / Data 安全治理 |
| 企業 ICT / SI | 協助串接 MES / SCADA / PLC 與多據點導入 |

__12.1 商業模式__

- 平台 SaaS：依 Factory / Production Line / Machine 計費。
- AI Usage：依 Camera、Agent、Inference 使用量計費。
- System Integration：MES / PLC / IoT / Camera 串接。
- Managed Service：7×24 Monitoring、AI Operations、Security 與維運分析。

# __13. 從競賽 Demo 到真實工廠的導入路線__

| 階段 | 替換/新增項目 | Agent Layer 是否重做 |
| --- | --- | --- |
| 競賽 MVP | Simulator + Synthetic MES + Demo Manual | 否 |
| PoC 工廠 | 真實 OPC-UA / MQTT Sensor + 歷史維修資料 | 微調工具與模型 |
| Pilot Line | MES / SCADA / CMMS + 權限與 Safety Review | 保留架構，強化治理 |
| Production | 多產線、多設備商、HA / SOC / SLA | 擴充，不改核心閉環 |

| 產品化論述<br>競賽要證明的不是「我們已經懂所有工廠」，而是證明 Hardware-agnostic Agentic Factory Operations Architecture 可以在可控 Digital Twin 中完成端到端閉環，並有清楚的真實資料替換介面。 |
| --- |

# __14. 主要風險與回答策略__

| 評審可能追問 | 建議回答 |
| --- | --- |
| 資料是假的嗎？ | Sensor/Orders/History 為 Synthetic，但事件、Agent tool call、排程切換、狀態更新與 KPI 驗證都是真實執行；所有 synthetic data 明確標示。 |
| 為什麼診斷可信？ | Simulator 有 Ground Truth，Agent 看不到標籤，可量測 Accuracy / Latency / False Positive。 |
| 能控制真機嗎？ | 競賽不直接控制真 PLC；真實導入以 OPC-UA/MQTT/MES Adapter 替換 Simulator，且高風險操作需人工核准。 |
| LLM 幻覺怎麼辦？ | LLM 不直接決定安全控制；Evidence、Rule/Optimization、Policy、Human Approval、Verify 共同約束。 |
| 為什麼不是一般 Dashboard？ | 作品核心是 Detect→Diagnose→Impact→Plan→Safety→Execute→Verify，且執行會真的改變 Digital Twin 狀態。 |

# __15. 最終競賽訊息__

| 主題名稱<br>Factory Guardian AI｜Agentic AI 智慧工廠自主營運與風險管理平台 |
| --- |

| 核心公式<br>Machine × Production × People → 一條完整的 Agentic AI 閉環 |
| --- |

設備 Agent 回答「為什麼壞」；Production Agent 回答「怎麼讓訂單不中斷」；Safety Agent 回答「這個方案是否安全」；Orchestrator 則負責把分析變成可執行、可核准、可驗證的工廠營運流程。

競賽 Demo 的重點不是追求工廠規模，而是用最小但可信的 Digital Twin，證明這條閉環真的能運作。

