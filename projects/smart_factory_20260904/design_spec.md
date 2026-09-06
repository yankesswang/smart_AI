<!-- ppt-master-schema: design-spec/v1 -->
# Factory Guardian AI 競賽提案 - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | smart_factory_20260904 |
| Canvas Format | PPT 16:9 (1280×720) |
| Page Count | 15 |
| Primary Language | zh-Hant-TW |
| Target Audience | 2026 中華電信智慧創新應用大賽評審：熟悉智慧製造題型、看過大量預測性維護提案，會直接追問「資料是不是假的」與「憑什麼說你比較好」 |
| Communication Intent | 先以工安那一列的取捨立場奪下注意力並定義評判標準；再證明八階段閉環的每個環節都有反制作弊的架構設計；接著以兩份外部公開資料集驗證回答「資料是假的嗎」；最後交代 ROI 與電信通路，爭取技術成熟度（40%）與商業價值（40%）兩項高分 |
| Desired Audience Outcome | 評審能複述「工安是硬限制不是加權項」這個立場，相信診斷方法在別人的資料上也成立，並理解 ROI 的最敏感參數已被主動標出 |
| Core Message / Ask / Action | 異常發生時 AI 不只警告，而是自動走完偵測到驗證恢復的八階段閉環——而且每一個可能作弊的環節都被架構擋掉了 |
| Delivery Context | 主要為評審現場簡報，有主講，約 10–12 分鐘；次要為評審會後獨立翻閱與提案書撰寫的依據 |
| Artifact Afterlife | 競賽評分依據、提案書素材、後續對製造業客戶說明的基礎版本 |
| Reading Mode | balanced |
| Content Strategy | 平衡：以 README、競賽提案規格、商業案例與兩份外部驗證文件的實測數字為準，重新編排為結論先行的評審敘事；所有假設參數沿用來源文件標註的「實測／推導／假設」分級，不上調亦不隱藏。程式識別字（函式名、測試檔名、內部欄位名）一律改寫為業務語言，僅保留可稽核的安全規則編號（SR-xx）與公開資料集名稱 |
| Design Style | 亮場工程報告（swiss-minimal 的網格紀律 × data-journalism 的證據密度）：白底承載大量 KPI 與對照表，工業藍為系統色，hazard red 全簡報只用於工安與危害，驗證綠只用於通過 |
| AI Image Acquisition Path | api — 2026-09-06 依使用者要求補圖，Path A（OpenAI gpt-image-1）|
| Generation Mode | continuous |
| Spec Refinement | disabled |
| Speaker Notes | enabled — final Stage-2 proactive policy（委託決策：現場有主講，需逐頁講稿） |
| Custom Animations | disabled — final Stage-2 proactive policy |
| Narration Audio | disabled — final Stage-2 proactive policy |
| Created Date | 2026-09-04 |

## II. Canvas Specification

| Property | Value |
| --- | --- |
| Format | PPT 16:9 |
| Dimensions | 1280 × 720 |
| viewBox | `0 0 1280 720` |
| Margins | 上下 56，左右 72 |
| Content Area | x 72–1208，y 56–664（可用寬 1136、高 608） |

## III. Visual Theme

### Theme Style

- **Mode**: custom
- **Mode References**: pyramid, narrative
- **Mode Behavior**: 以 pyramid 為主骨架，第二頁即交出三組對照數字，其後每頁標題都是可被追問的判斷句。可信度章（P05–P08）改用 narrative 的張力結構——每頁先講一個「如果不這樣做就會被作弊」的風險，再給出架構如何堵住它，讓評審跟著懷疑再跟著解除懷疑；證據章回到 pyramid 的先結論後數據。
- **Visual style**: custom
- **Visual Style References**: swiss-minimal, data-journalism
- **Visual Style Behavior**: swiss-minimal 負責嚴格欄位網格、寬鬆頁邊與髮絲線分隔——容器一律直角、無陰影無漸層，區塊邊界由 1px 分隔線與底色階差打出；data-journalism 負責證據密度——多欄微型圖表、側欄註解、每個數字下方一律帶來源行（實測／推導／假設）。色彩紀律極嚴：工業藍承載系統與結構，hazard red 全簡報只出現在工安與危害語意，驗證綠只出現在「通過」，其餘一律灰階。
- **Theme**: 工程報告——每一頁都像同一份可重跑報告的不同章節；跨頁復現的母題是「數字 + 其來源分級標記」的並置
- **Tone**: 冷靜、可查證、主動自曝限制；不使用行銷語彙

### Color Scheme

| Role | HEX | Purpose |
| --- | --- | --- |
| Background | #FFFFFF | 全域亮底，承載密集表格與留白 |
| Secondary background | #F1F4F8 | 次級區塊底，區隔證據區與敘事區 |
| Primary | #0F4C81 | 工業藍：系統、結構、閉環階段與主標強調 |
| Accent | #E2231A | hazard red：只給工安、危害與被否決的方案 |
| Secondary accent | #1B7F5A | 只給驗證通過與正向達成 |
| Body text | #16202B | 正文與主要標籤 |
| Secondary text | #5A6B7D | 來源分級行、註解、次要標籤 |
| Divider | #D5DDE5 | 髮絲線、表格分隔與區塊邊界 |
| Surface | #FAFBFC | 面板底，用於證據卡與表格斑馬列 |
| Grid | #E8EDF2 | 比分隔線更淡的網格與軸線 |
| Positive | #1B7F5A | 正向指標（產能達成、健康度、驗證通過） |
| Warning | #E8890C | 張力指標（以產能換安全的代價） |
| Negative | #E2231A | 危害、二次損壞、對照組劣化 |

## IV. Typography System

### Font Plan

| Role | Character (Reference) | Primary | English if non-English | Fallback tail |
| --- | --- | --- | --- | --- |
| Title | 中性無襯線／高重量，工程報告的克制性格 | Microsoft JhengHei | Segoe UI | sans-serif |
| Body | 中性無襯線／高易讀，密集表格與投影皆可 | Microsoft JhengHei | Segoe UI | sans-serif |
| Data | 等寬／KPI 數值、規則編號與指標欄位對齊 | Consolas | Consolas | monospace |

- **Title stack**: Microsoft JhengHei, Segoe UI, sans-serif
- **Body stack**: Microsoft JhengHei, Segoe UI, sans-serif
- **Data stack**: Consolas, monospace
- **Role rationale**: Data 為新增家族角色——本簡報有五個頁面以 KPI 對照為主體（P02、P09、P11、P12、P13），數值需在欄位間垂直對齊且與敘事正文在字寬上分離，等寬字體是唯一能同時滿足兩者的選擇。

### Font Size Hierarchy

| Purpose | Anchor Size (px) |
| --- | ---: |
| Body | 24 |
| Title | 42 |
| Subtitle | 32 |
| Lead | 30 |
| Display | 88 |
| Annotation | 18 |
| Footnote | 16 |
| Data | 22 |

## V. Layout Principles

### Deck-wide Direction

- **Hierarchy direction**: 判斷句在頂部單行，數字在中段主場，來源分級行在底部；視線由上而下三階，數字永遠比敘述先被看見
- **Composition tendency**: 主結構為頂部判斷句 + 下方證據主場的橫向分層；閉環總覽頁改為橫貫全寬的八階段軌道；可信度章採左風險／右反制的對峙雙欄
- **Cross-page continuity**: 八階段軌道母題在 P04 建立完整形態，之後可信度章各頁只高亮其所屬階段並淡化其餘；每個引用數字下方一律附「實測／推導／假設」分級行，此標記在全簡報一致復現
- **Spacing posture**: variable by page rhythm——證據頁 dense，工安論點頁與收束頁 breathing
- **Spacing anchors**: 頁邊距 72、區塊間距 32、欄間距 40、圓角 0、正文行高 38

## VI. Icon Usage Specification

- **Primary bundled library**: chunk-filled

| Icon Path | Suitable Scenarios |
| --- | --- |
| chunk-filled/factory | 工廠、產線、全廠規模 |
| chunk-filled/triangle-exclamation | 異常、告警、危害 |
| chunk-filled/gauge-high | 健康度、產能達成率、KPI |
| chunk-filled/temperature-high | 溫度訊號、設備劣化 |
| chunk-filled/waveform | 振動訊號、聲學偵測 |
| chunk-filled/magnifying-glass | 根因診斷、指紋比對 |
| chunk-filled/route | 八階段閉環、影響分析 |
| chunk-filled/sliders | 多準則加權排名 |
| chunk-filled/shield-check | Safety Agent、硬規則 |
| chunk-filled/traffic-cone | 工安、危險區、硬限制 |
| chunk-filled/badge-check | 人工核准、簽核 |
| chunk-filled/person-walking | 人員闖入、危險區曝露 |
| chunk-filled/play | 執行、模擬乾跑 |
| chunk-filled/circle-checkmark | 執行後驗證通過 |
| chunk-filled/clipboard | 工單、SOP、手冊 |
| chunk-filled/wrench | 維修、派工 |
| chunk-filled/robot | Agent、自主決策 |
| chunk-filled/chart-bar | Benchmark、對照組 |
| chunk-filled/coin | ROI、效益、成本 |
| chunk-filled/stopwatch | 偵測延遲、診斷工時 |
| chunk-filled/video-camera | CAM-01 工安監視、VLM |

## VII. Visualization Reference List

| Page | Family | Template | Usage |
| --- | --- | --- | --- |
| P10 | table | metric_table | 三組對照在七項設備故障 KPI 上的完整比較，同 seed 同情境同總時長 |
| P11 | chart | horizontal_bar_chart | 三組對照的人員危險區曝露分鐘數，讓 88 分鐘與 1 分鐘的差距成為畫面主體 |
| P12 | chart | column_chart | 感測器指紋法與四個對照方法在 AI4I 2020 上的 Top-1 根因歸因正確率 |
| P13 | chart | grouped_bar_chart | 四個泵浦個體上本專案 AUC 與官方 baseline AUC 的逐一比較 |
| P14 | chart | pareto_chart | 七個效益項的年度金額降冪與累積占比，暴露二次損壞避免的主導地位 |

## VIII. Image Resource List

| Filename | Dimensions | Ratio | Purpose | Type | Image pattern | Crop Policy | Acquire Via | Status | Reference | text_policy | page_role |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| guardian_hardware_state.png | 900×828 | 1.09 | P05 左欄：孿生體當下看到的三台機台狀態、健康度條與遙測數值 | Screenshot | 窄欄直立，與右側閉環畫面共用一條上緣基線；不裁切，健康度條與 CRITICAL 標記都是論點 | no-crop | user | Existing | 由執行中的服務擷取；證明遙測與狀態不是靜態圖 | none | local |
| guardian_agent_loop.png | 1800×842 | 2.14 | P05 右欄：八階段閉環的即時進度，含被擋下與等待簽名的狀態 | Screenshot | 寬欄橫幅置於右側主場；不裁切，步驟卡的進行中／已完成／被擋下三種狀態都要看得見 | no-crop | user | Existing | 由執行中的服務擷取；支撐「閉環是跑得動的，不是流程圖」 | none | hero_page |
| fg_cover_hero.png | 1024×1536 | 0.67 | P01 封面右欄：產線、紅色警示的加工機、危險區與站在區外的人員 | Illustration | 直立右欄，與左側鉤子句共用上緣；不裁切 | no-crop | ai | Generated | OpenAI gpt-image-1，2026-09-06 | none | local |
| guardian_cam01.png | 1042×660 | 1.58 | P05 右下小圖：CAM-01 工安畫面（危險區 0 人） | Screenshot | 縮圖並列於閉環圖下方 | no-crop | user | Existing | 由執行中的服務擷取 | none | local |
| guardian_plan_matrix.png | 2104×422 | 4.99 | P05 左下：方案排名理由與 ±20% 權重穩健性掃描 | Screenshot | 寬幅橫條 | no-crop | user | Existing | 由執行中的服務擷取 | none | local |
| guardian_verify.png | 1042×810 | 1.29 | P05 右下：執行後驗證三項 PASS 與 VERIFIED 鋼印 | Screenshot | 小方塊 | no-crop | user | Existing | 由執行中的服務擷取 | none | local |
| fg_hazard_zone.png | 1536×1024 | 1.50 | P11 右欄：運轉中機台旁的危險區與監視攝影機 | Illustration | 與左側長條圖並置 | no-crop | ai | Generated | OpenAI gpt-image-1，2026-09-06 | none | local |
| fg_landing_5g.png | 1536×1024 | 1.50 | P15 右下：工廠 → 5G 小基站 → 邊緣機櫃 → 雲 | Illustration | 與通路帶、收尾句並置 | no-crop | ai | Generated | OpenAI gpt-image-1，2026-09-06 | none | local |

## IX. Content Outline

### Part 1: 判斷

#### Slide 01 - 封面：88 分鐘

- **Audience move**: 預期看到又一個預測性維護提案 → 意識到這份提案要談的是取捨立場
- **Relationships**: 一個數字（88 分鐘）與其代價敘述為 link；與產品定位為 contrast
- **Cover impact**: 綁定鉤子——「Baseline A 的產能最漂亮，代價是讓人在運轉的機台旁邊站了 88 分鐘。」以此句為視覺主體，Factory Guardian AI 名稱與副標退為次階
- **Composition**: 白場左側巨型數字 88 分鐘與其代價句佔據主場，右側以極淡的八階段軌道剪影暗示後續結構；競賽資訊壓在底部單行
- **Title**: Factory Guardian AI — Agentic AI 智慧工廠自主營運與風險管理平台
- **Core message**: 產能最漂亮的那一組，代價是讓人站在危險區裡 88 分鐘
- **Content**:
  - 主鉤子句：Baseline A 的產能最漂亮，代價是讓人在運轉的機台旁邊站了 88 分鐘
  - 產品定位一行：偵測 → 診斷 → 影響分析 → 方案規劃 → 安全檢查 → 人工核准 → 執行派工 → 驗證恢復
  - 參賽資訊：2026 中華電信智慧創新應用大賽／智慧製造／競賽 MVP
- **Fact IDs**: hazard-zone 情境 Baseline A 曝露 88 min

#### Slide 02 - 結論：三組數字先給評審

- **Audience move**: 不知道這套系統能做到什麼 → 拿到三個可在後續頁面被驗證的對照
- **Relationships**: 三組對照（產能達成 / 最大交期延遲 / 危險區曝露）為 membership，各自為現況與 Guardian 的 contrast
- **Composition**: 三組「現況 → Guardian」的箭頭對照橫向並列佔上半，下方單行判斷句收束
- **Title**: 同一組孿生體、同一個 seed，三組數字的差距
- **Core message**: 產能達成率、交期延遲與工安曝露同時改善，而且三個數字都來自同一次可重跑的 benchmark
- **Content**:
  - 產能達成率：41.3% → 96.8%（設備故障情境，bearing／cooling／motor 平均）
  - 最大交期延遲：1,385 min → 0 min
  - 人員危險區曝露：88 min → 1 min（hazard-zone 情境）
  - 來源分級行：全部為實測，跑在相同 seed、相同情境、相同總時長的孿生體上
- **Fact IDs**: benchmark 設備故障情境與 hazard-zone 情境實測

### Part 2: 問題與系統

#### Slide 03 - 判斷：警告不等於處理

- **Audience move**: 認為告警系統已經夠用 → 看見告警與處理之間那段 90 分鐘的人工缺口
- **Relationships**: 三個現況痛點（告警逃逸 / 人工判根因耗時 / 知識集中在少數人）為 membership，共同 link 到停機與二次損壞
- **Composition**: 左側三行現況痛點縱列，右側以一條中斷的流程線標出「告警之後沒有人接手」的缺口
- **Title**: 現行流程在告警之後，還有 90 分鐘是人工的
- **Core message**: 缺的不是更靈敏的告警，是告警之後能自己走完診斷、規劃、安全檢查與驗證的閉環
- **Content**:
  - 現行流程人工判定根因需 90 分鐘：到場 15 ＋ 現場量測 30 ＋ 查手冊與歷史工單 30 ＋ 與生產確認 15（假設）
  - 維修知識分散在少數老師傅身上，工單完整度僅約 60%（假設，以 WorkOrder.completeness() 欄位口徑）
  - 目標客戶：有高價旋轉設備、停機成本高的中大型製造廠（電子組裝、精密加工、化工、食品）
  - 缺的那一層：偵測之後自動完成診斷、影響分析、方案規劃、安全檢查、人工核准、執行派工與驗證恢復
- **Fact IDs**: 商業案例 §3 假設參數全表

#### Slide 04 - 八階段閉環：六個 Agent 與唯一知道真相的地方

- **Audience move**: 只知道「有個 AI 系統」 → 拿到可指認的八個階段與各自的負責 Agent
- **Relationships**: 八個階段為 order；六個 Agent 與其階段為 link；Digital Twin 與所有階段為 parent（唯一持有 ground truth）
- **Composition**: 橫貫全寬的八階段軌道母題在此建立完整形態，各節下方掛其負責 Agent；Digital Twin 以底層橫幅承載全部階段
- **Title**: Detect → Diagnose → Impact → Plan → Safety → Approve → Execute → Verify
- **Core message**: Orchestrator 協調八個階段並在驗證不通過時重新規劃；Digital Twin 是唯一知道真相的地方，Agent 看不到它
- **Content**:
  - 八階段：偵測 → 診斷 → 影響分析 → 方案規劃 → 安全檢查 → 人工核准 → 執行派工 → 驗證恢復
  - 六個 Agent：Monitoring／Diagnosis／Production（影響與規劃）／Safety／Verification，加上 Policy Engine 與 Simulator API
  - 決策層的排名是加權多準則計算，不是 LLM
  - Digital Twin：拓撲、故障模型、模擬引擎、情境；Ground Truth 只存在 _MachineRuntime.fault，snapshot() 不輸出
  - 沒有 OpenAI 金鑰也能跑完整 Demo：LLM 敘述退回確定性離線敘述器，LLM 的角色僅止於敘述

### Part 3: 可信度

#### Slide 05 - 戰情中心：這個閉環是跑得動的

- **Audience move**: 把八階段閉環當成一張流程圖 → 看見它是一個正在推進、會停下來等人簽名的執行中系統
- **Relationships**: 機台狀態與閉環進度為 link（左邊的異常驅動右邊的階段推進）；步驟卡的三種狀態（進行中 / 已完成 / 被擋下）為 contrast
- **Composition**: 判斷句在上，左窄欄機台狀態、右寬欄閉環進度並置，底部單行說明可展開的推理
- **Title**: 這個閉環是跑得動的，不是流程圖
- **Core message**: 前一頁的八個階段不是投影片上的框，是一個正在推進並且會在高風險處停下來的執行中系統
- **Content**:
  - 左：孿生體現在看到的機台狀態與遙測，含健康度條與 CRITICAL 標記
  - 右：八階段閉環即時進度——進行中反白、被擋下轉紅、等人簽名停住
  - 每一格步驟卡都能展開推理：看到什麼 → 怎麼算 → 為什麼不是別的 → 結論
  - 畫面上每一個數字都取自後端算過的結果，前端不補敘述
- **Images**: guardian_hardware_state.png 與 guardian_agent_loop.png 並置於同一條上緣基線，左窄右寬，兩者共同構成一個畫面而非兩張獨立截圖

#### Slide 06 - 三個可信度原則，每一個都有測試守著

- **Audience move**: 準備追問「數字是不是編的」 → 發現三個最容易作弊的地方都被架構堵住
- **Relationships**: 三個原則為 membership，各自 link 到一個具名測試
- **Composition**: 三欄等重並列，每欄上為原則、中為落實方式、下為守著它的測試檔名（等寬字體）
- **Title**: 競賽 Demo 最容易被質疑的是「數字是不是編的」，這套系統用架構回答
- **Core message**: 合成資料明確標示、Agent 看不到答案、執行後回到模擬器驗證——三件事各自有一個會失敗的測試守著
- **Content**:
  - 合成資料明確標示：所有 Sensor／Orders／Manual／History 都標記 synthetic，CLI 與 Dashboard 常駐顯示（test_api.py::test_dashboard_page_renders）
  - Agent 看不到答案：Ground Truth 只存在 _MachineRuntime.fault，snapshot() 不輸出；規劃用的是診斷信念模型（test_twin.py::test_snapshot_never_exposes_ground_truth）
  - 執行後回到模擬器驗證：執行真的改變孿生體狀態，Verification Agent 在真實孿生體上量 KPI，不通過就重新規劃（test_orchestrator.py::test_full_loop_reaches_a_verified_execution）
  - 全套 102 個測試離線 4 秒跑完

#### Slide 07 - 規劃跑在診斷信念模型上，不是跑在答案上

- **Audience move**: 以為方案模擬理所當然 → 理解若模擬用了真值，整個 Demo 就失去意義
- **Relationships**: 錯誤作法與正確作法為 contrast；診斷錯誤 → 投影錯誤 → Verification 抓出 → 重新規劃為 order
- **Composition**: 左側標出「若直接在帶著 Ground Truth 的孿生體上乾跑會發生什麼」，右側為 fork_as_belief 的正確路徑；兩者以一條垂直分隔線對峙
- **Title**: 診斷錯了，投影就會錯——這是刻意留下的
- **Core message**: 方案投影若在帶著真值的孿生體上乾跑，等於讓 Production Agent 偷看答案，診斷正確與否就不再影響結果
- **Content**:
  - fork_as_belief() 拿掉真實故障標籤，換上 Diagnosis Agent 推論出來的故障，並用觀測到的健康度反推嚴重程度
  - 診斷錯誤會讓投影錯誤，由 Verification Agent 在真實孿生體上抓出來並觸發重新規劃
  - 診斷排名由感測器指紋餘弦相似度（0.75）主導，加上歷史先驗（0.15）與文件支持度（0.10）；RAG 提供可引用的 Evidence，不決定答案
  - 信心度被訊號強度壓抑：訊號微弱時不給高信心，Orchestrator 會先繼續觀察（confirm_diagnosis）而不急著動設備

#### Slide 08 - Safety 看的是方案會把設備帶到哪裡

- **Audience move**: 以為安全檢查就是看當下數值 → 理解早期介入時當下數值根本不違規
- **Relationships**: 現況型規則與預測型規則為 contrast；預測峰值 → 硬限制否決為 order
- **Composition**: 上半為一條由當下值升向預測峰值的訊號線，危險門檻以 hazard red 橫線標出交點；下半引用 Safety Agent 的裁決原文
- **Title**: 早期介入時，「維持全速運轉」在當下並不違規
- **Core message**: Safety Agent 除了現況規則還有預測型規則，用方案乾跑出來的訊號峰值判斷，才擋得住早期介入場景
- **Content**:
  - 預測型規則 SR-02P／SR-03P／SR-13：拿方案乾跑出來的訊號峰值來判斷
  - 實際 Demo 中擋下 PLAN-A 的裁決原文：模擬顯示本方案會讓 M-A 振動由目前 5.65 mm/s 升至 14.18 mm/s，超過危險門檻 7 mm/s
  - 裁決理由：在振動持續上升的情況下維持產出，等同於為了產量接受旋轉件破損風險
  - 被 BLOCK 的方案在 Dashboard 上紅底刪除線並列出理由，不從畫面上消失
- **Fact IDs**: bearing-degradation Demo 步驟 7

#### Slide 09 - 排名用固定尺規，不用 min–max

- **Audience move**: 把排名當成技術細節 → 理解正規化選擇會無中生有地製造鑑別力
- **Relationships**: 兩個排名設計決定（固定尺規 / 核准只扣 0.1）為 membership，各自 link 到一個被避免的失效行為
- **Composition**: 左右兩個決定各佔一半，每邊上為決定、下為「不這樣做會發生什麼」，中央留白
- **Title**: min–max 會把 0.15 的實質差距，端走 0.30 的權重
- **Core message**: 分數必須有絕對意義，跨情境、跨執行都代表同一件事；治理程序不該被當成安全缺陷來扣分
- **Content**:
  - min–max 是相對比較：候選方案差距很小時，正規化仍會把最好的拉到 1、最差的壓到 0，無中生有地製造鑑別力
  - 改用固定尺規後，分數有絕對意義，跨情境跨執行都代表同一件事
  - 「需要人工核准」只扣 0.1 分：它是治理程序，不是安全缺陷
  - 早期版本扣到 0.4 的後果：系統會為了避開核准流程而偏好「不用人簽名但其實比較糟」的方案
  - 驗證比的是反事實不是事故前狀態：實際結果有沒有達到方案自己的預測？有沒有贏過什麼都不做？

### Part 4: 證據

#### Slide 10 - 三組對照：同 seed、同情境、同總時長

- **Audience move**: 只看到單一亮眼數字 → 拿到完整 KPI 對照，可自行判斷改善來自哪裡
- **Relationships**: 三組對照為 membership，各自在七項 KPI 上取值；Baseline A 與 Baseline B 為 contrast；二次損壞列與其餘 KPI 為 contrast
- **Composition**: 表格佔據主場，判斷句退為頂部單行；Guardian 欄以工業藍反白，Baseline A 的二次損壞格以 hazard red 標出
- **Title**: 設備故障情境（bearing／cooling／motor 平均）的完整 KPI 對照
- **Core message**: Baseline A 保住不了設備，Baseline B 保住設備但賠掉產能，Guardian 兩者都拿到且驗證通過
- **Content**:
  - 偵測延遲：7.3 min / 3.7 min / 3.7 min
  - 根因診斷正確率：— / — / 100%
  - 產能達成率：41.3% / 72.1% / 96.8%
  - 最大交期延遲：1,385 min / 3.5 min / 0 min
  - 設備最終健康度：27.3 / 100 / 100
  - 二次損壞：發生 / 未發生 / 未發生
  - 執行後驗證：— / — / 通過
  - 來源分級行：實測，factory-guardian benchmark 實際執行結果，非預錄
- **Visualization**: fault-kpi-matrix（三組對照 × 七項設備故障 KPI 的實測值矩陣）
- **Native-ready**: fault-kpi-matrix=yes
- **Fact IDs**: benchmark 設備故障情境實測結果表

#### Slide 11 - 工安是硬限制，不是加權項

- **Audience move**: 用產能達成率排序三組方案 → 改用曝露分鐘數重新排序，接受評判標準被改寫
- **Relationships**: 三組對照的曝露分鐘為 contrast；產能代價與風險削減為 overlap（同一個取捨的兩面）
- **Composition**: 單一橫條圖佔據主場，88 min 那條以 hazard red 壓倒性延伸；下方單行判斷句，頁面留白高於其他證據頁
- **Title**: 用 25% 的產能，換 87 分鐘的風險曝露
- **Core message**: 這一列是整個提案最重要的論點——當產能與工安衝突時，工安不是可被加權犧牲的一項
- **Content**:
  - 人員危險區曝露：Baseline A 88 min / Baseline B 1 min / Guardian 1 min
  - 產能達成率：Baseline A 99.0% / Baseline B 67.5% / Guardian 74.3%
  - 判斷：Baseline A 的產能最漂亮，代價是讓人在運轉的機台旁邊站了 88 分鐘
  - CAM-01 工安畫面刻意不放實景照片：照片是死的，人員離開危險區後它不會變，會成為畫面上唯一與資料對不上的東西
- **Visualization**: hazard-exposure（三組對照的人員危險區曝露分鐘數）
- **Native-ready**: hazard-exposure=yes
- **Fact IDs**: hazard-zone 情境實測

#### Slide 12 - 外部驗證一：把同一套方法搬到別人的資料上

- **Audience move**: 懷疑指紋法只在自家孿生體上成立 → 看見它在公開資料集上也成立，且輸的地方被寫出來
- **Relationships**: 五個方法的 Top-1 正確率為 contrast；「贏過現行流程」與「輸給 LR／RF」為 overlap（同一次驗證的兩個結論）
- **Composition**: 直條圖佔主場，本專案那根以工業藍標出，兩根贏過它的以灰階但不縮小；下方兩行分別寫贏在哪與輸在哪
- **Title**: 方法在別人的資料上成立，但它不是最強的分類器——兩件事都寫在這裡
- **Core message**: 把感測器指紋餘弦法原封不動搬到 UCI AI4I 2020 上，顯著優於代表現行流程的單一訊號門檻規則，但輸給同特徵下的 LogisticRegression 與 RandomForest
- **Content**:
  - 根因歸因 Top-1 0.724 / Top-3 0.985（隨機為 0.250 / 0.750）
  - 單一訊號門檻規則（代表現行流程）：Top-1 0.458
  - LogisticRegression：0.964；RandomForest：0.939
  - 誠實邊界：AI4I 2020 本身是合成資料集不是真實工廠量測；這份資料沒有振動訊號；指紋的來源在這裡和專案裡不一樣
  - 為什麼要做這件事：研究文件明列的失分警訊是「只報模型準確率，沒有和現行流程或簡單 baseline 比較」
- **Visualization**: ai4i-top1（五個方法在 AI4I 2020 上的 Top-1 根因歸因正確率）
- **Native-ready**: ai4i-top1=yes
- **Fact IDs**: 外部驗證文件實測（UCI AI4I 2020）

#### Slide 13 - 外部驗證二：真實工業錄音，八格全部優於官方 baseline

- **Audience move**: 認為外部驗證只做了合成資料 → 看見另一份是真實工業錄音且逐項比對官方基準
- **Relationships**: 四個泵浦個體為 membership，各自為本專案與官方 baseline 的 contrast
- **Composition**: 分組直條圖佔主場，四組各兩根；平均那組以加粗與工業藍標出；下方單行寫明資料集與 split
- **Title**: DCASE2020 pump：四個個體、兩個指標，八格全部優於官方 baseline
- **Core message**: 聲學偵測這一側用的是真實工業錄音，不是合成音訊，而且與官方 baseline 逐格比較
- **Content**:
  - 資料集：DCASE2020 Task2 pump development set，test split（400 正常 + 456 異常）
  - 平均 AUC：本專案 0.9030 vs 官方 baseline 0.7259（ΔAUC +0.1771）
  - 平均 pAUC：本專案 0.7855 vs 官方 baseline 0.6000（ΔpAUC +0.1855）
  - 逐個體 AUC：id 00 0.8845／id 02 0.8641／id 04 0.9743／id 06 0.8893
  - 方法：log-mel 頻譜 + 每頻帶兩個時間統計量，Local Outlier Factor 只用正常音訊擬合
  - 訓練集只有正常音訊是特徵不是限制：真實產線拿不到足量的故障錄音
- **Visualization**: dcase-auc（四個泵浦個體上本專案與官方 baseline 的 AUC）
- **Native-ready**: dcase-auc=yes
- **Fact IDs**: 聲學驗證文件實測（DCASE2020 Task2 pump）

### Part 5: 商業與落地

#### Slide 14 - ROI：效益集中在一項，而最敏感的參數是這個

- **Audience move**: 預期看到漂亮的 ROI 總數 → 看見效益結構的集中度與最敏感假設被主動標出
- **Relationships**: 七個效益項為 membership，降冪排列；二次損壞避免與其餘六項為 contrast；ROI 結果與最敏感參數為 link
- **Composition**: 帕累托圖佔主場，二次損壞避免那根壓倒性領先並附累積占比曲線；右側直欄列出四個財務結果與一行敏感度警語
- **Title**: 68.4% 的效益來自二次損壞避免，所以那個假設值得被追問
- **Core message**: 第一年 ROI +67.9%、回收期 4.4 個月，但效益高度集中於單一項目，且最敏感的單一參數是現況告警逃逸率 0.25
- **Content**:
  - 年度效益合計 4,659,540 元（全廠 6 線、基準情境）
  - 二次損壞避免 3,185,388 元（68.4%）；避免停機／降載產出損失 1,115,402 元（23.9%）；交期延遲罰則與趕工 304,021 元（6.5%）；維修工時節省 50,117 元（1.1%）
  - 第一年 ROI +67.9%；穩態年度 ROI +178.2%；簡單回收期 4.4 個月；三年 NPV（折現率 8%）+6,591,337 元
  - 最敏感的單一參數：現況告警逃逸率 0.25（假設）；效益實現率 0.8（假設，涵蓋感測器安裝品質、門檻校準期、人員採用率與模型退化）
  - 每項效益都追溯到一個實測 KPI 欄位（production_loss_ntd、secondary_damage、max_order_delay_min 等）
- **Visualization**: roi-benefit-pareto（七個效益項的年度金額降冪與累積占比；PowerPoint 原生 pareto 物件無法同時承載每柱資料標籤與累積占比線，故此物件走 shape-first 可編輯圖形路徑）
- **Native-ready**: roi-benefit-pareto=no
- **Fact IDs**: 商業案例 §1 一頁摘要、§4.1 年度效益拆解

#### Slide 15 - 落地：中華電信把它變成可複製的方案

- **Audience move**: 認同技術與效益 → 接受這是中華電信具備既有基礎設施優勢的生意
- **Relationships**: 四個角色（使用者／決策者／採購者／通路）為 membership；收費方式四層為 order；與收束判斷為 parent
- **Closing impact**: 綁定收束——工安是硬限制不是加權項，這個立場才是這套系統值得被信任的原因。構圖為 P04 八階段軌道母題的收束變體
- **Composition**: 上半橫向並列買方角色與收費四層，下半以單行判斷句收束並回扣封面的 88 分鐘
- **Title**: 5G 專網、MEC、hicloud 與 OT 資安，把單廠導入變成可複製方案
- **Core message**: 買方明確、預算科目明確、通路是中華電信既有的基礎設施——而讓客戶願意簽下去的，是工安那條硬限制
- **Content**:
  - 使用者：設備維護技師、產線班長；決策者：廠務／設備維護主管、工安主管
  - 採購者：數位轉型辦公室或 IT／OT 團隊，預算多掛在「智慧製造」或「工安改善」項下
  - 收費方式：平台 SaaS（廠／線／機台）＋ AI Usage（Camera／Agent／Inference）＋ System Integration（一次性）＋ Managed Service（維運）
  - 通路：中華電信整合 5G 專網、MEC、hicloud、OT 資安與維運服務，形成可複製方案
  - 收束：工安是硬限制，不是加權項——這是整套系統最值得被信任的地方
- **Fact IDs**: 商業案例 §1 一頁摘要

## X. Speaker Notes Requirements

- **Generation**: enabled
- **Filename**: match each SVG filename under `notes/`
- **Content**: 每頁講稿以該頁最終 SVG 上的可見內容為依據，補充頁面未寫出的推導與轉場；所有 KPI 與財務數字沿用來源文件原值，不重新計算或四捨五入；引用假設參數時講稿必須同時說出其分級（實測／推導／假設）；P11 的講稿明確指示主講主動說出「輸給 LogisticRegression 與 RandomForest」，P13 的講稿明確指示主動說出告警逃逸率是最敏感假設
- **Total duration**: 10–12 分鐘，平均每頁 45–50 秒，P04 與 P09 各可延長至 90 秒
- **Notes style**: 工程簡報式——先給該頁的判斷句，再給一個可被追問的支撐點，最後一句轉場
- **Presentation purpose**: 先以工安取捨立場定義評判標準，再證明八階段閉環的反制作弊架構，接著以兩份外部公開資料集回答「資料是假的嗎」，最後交代 ROI 與電信通路
