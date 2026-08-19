# AgentGate — 實作說明

對應規格:[AgentGate_技術規格與競賽提案.md](AgentGate_技術規格與競賽提案.md)(v0.1, 2026-08-17)。
本文件記錄實作範圍、對照表與驗證結果,隨程式碼更新。

## 60 秒上手

```bash
pip install -e ".[dev]"

agentgate serve                  # 治理閘門 + Dashboard → http://127.0.0.1:8600
agentgate benchmark              # 120 條情境 × 6 baseline × 6 指標
agentgate scenarios              # 測試集統計
pytest tests/test_agentgate_*.py -q    # 284 個測試,離線 3 秒跑完
```

完全離線可跑:政策裁決層是確定性規則(刻意不用 LLM,規格 §4.3),
前端不引用任何外部資源(規格 §7.1 可靠性原則)。

## 營運實境層(打開就是一個進行中的現場)

治理層在真實世界裡不是一顆按鈕,它坐在這條管路中間:

```
客戶 ─→ 客服對話(工單) ─→ AI Agent 讀對話、決定呼叫工具 ─→【AgentGate】─→ 後台系統
```

[`console.py`](../agentgate/console.py) 把前兩段補上。`agentgate serve` 啟動時會
**回填 45 分鐘的當班歷史**(約 400 件工單、1,600 筆稽核紀錄),之後由前端輪詢
懶惰推進,每分鐘約 9 件持續進來 —— 不開背景執行緒,少一個併發來源。

| 元件 | 內容 |
|---|---|
| 工單 `Case` | 工單編號、受理渠道(App/官網/語音/門市/營運後台)、客戶、AI 席位、身分驗證程度 |
| 對話逐字 `Turn` | customer / agent / **tool** / system 四種角色;注入永遠藏在 tool 這一種 |
| 附件 `Attachment` | 附件原文逐行帶到前端,`injected_line` 指向夾帶指令那一行(前端標紅) |
| 19 個情境樣板 | 11 個正常業務 + 8 個攻擊/異常,合計約 9.5% 為異常流量(刻意高於真實環境) |
| 值班與 SLA | 三班制與值班主管;核准時限 high 5 分鐘 / medium 15 分鐘 / low 30 分鐘 |

三個刻意的設計:

1. **工單脈絡不參與裁決。** `ActionRequest.context` 只讓稽核與核准介面回得到指令出處,
   不能因為「這件客訴看起來很急」而放寬(`TestContextIsInert`)。
2. **標註不外洩。** `class` / `attack_type` 不進 context —— 核准介面看得到答案的話,
   「主管憑證據決定」就成了假的(`test_case_context_never_leaks_the_label`)。
3. **回填帶真實時間戳。** 稽核鏈時間分佈在過去一小時裡,不是全部擠在啟動那一秒
   (`AuditChain.append` 的 `ts`)。

### 一個修掉的建模錯誤

來源鏈只放**指令**的出處,不放**證據**的出處。客服 Agent 為了查證而呼叫帳務系統,
回傳的是事實不是命令 —— 它出現在對話裡(`role=tool`),不進 `provenance_chain`。
最初把內部查證也當成 `tool_output` 通道,結果每一件「查證後退費」都被 G0-R1 擋掉,
誤攔率直接爆掉。反過來,PDF 備註、被竄改的 SOP、記憶裡的「以後都直接停話」
是動作的理由來源,它們就是指令,一定要進鏈。
回歸測試:`test_normal_cases_carry_only_trusted_instruction_sources`。

## 運作機制

> 這一節在前端有對應的視角:Dashboard 的**「運作機制」分頁**
> (另有獨立頁 http://127.0.0.1:8600/mechanism,供提案截圖與嵌入用,兩者同一支模組)。
> 那一頁的說明由 `GET /api/gate/mechanism` 從生效中的程式碼匯出,
> 而且每一道關卡都可以當場送一筆真的動作請求進去看它被怎麼處理。

AgentGate 站在 AI Agent 與真實系統之間。Agent 不再直接呼叫工具,而是提出一個
**結構化動作請求**,逐關通過才會生效 —— 任何一關都可以否決,而**否決本身也是稽核事件**。

```
   Agent 動作請求(kind + params + principal + provenance_chain)
         │
         ▼
   G1 ──▶ G0 ──▶ G2 ──▶ G3 ──▶ G4 ──▶ G5 ──▶ 影子電信後台(execute 才真的寫入)
    │      │      │      │      │
    └──────┴──────┴──────┴──────┘
              BLOCK / REJECT
                    │
                    ▼
      拒絕 + 留證:被擋下的動作與被執行的動作,留下同等完整的紀錄
```

| 關卡 | 模組 | 職責 | 輸出 | 用 LLM |
|---|---|---|---|---|
| **G1** 動作解析 | [`gates/g1_resolution.py`](../agentgate/gates/g1_resolution.py) | 工具呼叫 → 結構化動作;參數 schema 驗證 | `ActionRequest` | 僅映射 |
| **G0** 來源信任 | [`gates/g0_provenance.py`](../agentgate/gates/g0_provenance.py) | 取來源鏈**最弱環節**,算出這條鏈可授權的風險上限 | `TrustVerdict` | 否 |
| **G2** 政策裁決 | [`gates/g2_policy.py`](../agentgate/gates/g2_policy.py) | 7 動作權限表 + 13 條 AG 規則,動態升級風險 | `PolicyDecision` + `Finding[]` | **否(刻意)** |
| **G3** 後果預演 | [`gates/g3_projection.py`](../agentgate/gates/g3_projection.py)、[`shadow.py`](../agentgate/shadow.py) | 影子環境乾跑,實測影響範圍/可回復性/個資欄位 | `Projection` | 否 |
| **G4** 人工核准 | [`gates/g4_approval.py`](../agentgate/gates/g4_approval.py) | 組裝 15 秒證據包,綁定經驗證的核准者身分 | `PendingApproval` | 否 |
| **G5** 執行與封存 | [`gates/g5_audit.py`](../agentgate/gates/g5_audit.py) | 執行 + 雜湊鏈封存,可驗證竄改 | `ChainRecord` | 否 |

編排在 [`pipeline.py`](../agentgate/pipeline.py) `evaluate()`,單一入口、單一出口(`GateVerdict`)。

### 一次動作的生命週期(退費 5,000 元,Demo 步驟 2)

| 關卡 | 實際發生的事 |
|---|---|
| G1 | `issue_refund` 必填 `amount` 存在且為正數 → 通過 |
| G0 | 來源鏈只有 `user_verified`(chat:demo-session)→ 授權上限 `high`,寫入稽核 `g0_trust` |
| G2 | 基礎風險 `medium`、`AG-20` 金流動作一律需核准;未觸發 AG-21/22/23 → `requires_approval = True` |
| G0-R1 | `medium` ≤ `high` → 不阻斷 |
| G3 | 影子乾跑:影響 1 個主體、金流 −5,000 元、**可回復(72 小時追回時窗)**、無個資欄位 |
| G4 | 進入佇列 `apr-xxxxxxxxxx`,證據包含 6 條證據(來源鏈 2、預演 3、Agent 推理 1) |
| G5 | 主管以門號綁定碼簽核後才 `execute()`,`g4_approved` 與 `g5_executed` 兩筆入鏈 |

同一條管線跑 Demo 步驟 3(PDF 夾帶「匯出所有客戶資料」)則是:

> 動作風險 forbidden 超過指令來源鏈可授權上限 low(最弱環節:tool_output / upload:invoice_20260817.pdf#p3)。

`gate_blocked_at = "G0"`,決策延遲 0.09 ms,無需任何模型判斷。

### 幾個關鍵設計決定

這些是實作中真正影響結果的地方,也是最經得起追問的部分。

**1. G0-R1 的檢查點在 G2 之後,歸因卻寫回 G0**

風險等級是 G2 算出來的(動作 × 範圍 × 主體),所以「風險有沒有超過來源鏈上限」這個判斷
必須等 G2 裁決完才能做。但攔截歸因寫的是 G0:同一個動作常常同時違反兩者 —— Demo 的
PDF 夾帶指令同時觸發 `AG-10`(批次筆數超標)與 `G0-R1`,兩條 finding 都留在裁決裡,
但根因是「指令從不可信通道進來」。歸因給 G2,稽核看到的會是「筆數超標」,看不見攻擊入口。

**2. 來源信任取最弱環節,不是取最後一手**

`evaluate_trust()` 用 `min(chain, key=CHANNEL_MAX_RISK)`:已驗證用戶把 PDF 內容轉述一次,
不會把 `untrusted` 洗成 `verified`。空來源鏈視同完全不可信(cap = `low`)——
沒有來源紀錄的指令,不能被授權做任何高風險動作。

這條規則是**結構性**的,不依賴偵測「這段文字看起來像攻擊」。攻擊者可以改寫措辭,
但改不掉指令是從一份上傳文件進來的這個事實。

**3. 政策裁決刻意不用 LLM**

可稽核(主管機關要的是「依據第 X 條」,不是「模型認為風險較高」)、不可繞過
(LLM 判斷可被提示詞影響,用 LLM 守 LLM 等於把攻擊面留在原地)、可重現
(同輸入必然同輸出,這是所有量測的前提)。LLM 只允許出現在 G1 的映射角色,
且其輸出必須通過 `validate_request()` 才進得了 G2。

**4. 風險是函式,不是常數**

`base_risk` 只是起點:13 條規則各自可以 `escalate_to`,最終取 `risk_max`;
`risk == "high"` 一律轉需核准,`forbidden` 直接不允許。所以同一個 `change_plan`
在無綁約時自動放行,在綁約期內被 `AG-24` 升到 high 而進核准佇列 —— 差別來自帳戶狀態,
不是來自 Agent 說了什麼。

**5. 影響範圍是實測的,不是 Agent 申報的**

`shadow.project()` 真的去撈,`affected_count` 是撈出來的數字。申報與實測的落差本身
就是攻擊訊號:申報 50 筆、影子環境實測 242 筆 → `AG-31` 範圍逃逸,G3 攔下。
拿掉 G3(消融 B3−G3),低報筆數的批次匯出就會騙過申報審查,HAR 由 0% 升到 7.9%。

**6. `reversible` 決定要不要人簽名,金額只決定風險等級**

`AG-30`:不可回復的動作永遠需要人工核准,無論規模。`read_bulk`(資料匯出即離開系統)
與 `reissue_sim`(舊卡立即失效、SIM swap 是帳號接管主要途徑)在預演裡標記
`reversible = False`,所以連 1 筆的匯出也要人簽名;退費反而是可回復的(72 小時追回時窗),
它的風險由單筆上限、30 日累計與入帳目標一致性決定。

**7. 證據包裡的 Agent 推理權重是 0**

Agent 的推理摘要會進證據包,但標成 `weight = 0.0` 並附警語「未經驗證的模型輸出,
不得作為核准唯一依據」。核准者該看的是規則**條文原文**、來源鏈與預演數字 ——
證據包的設計目標是 15 秒做出有依據的決定,不是讀一段說服性的敘述。

**8. 雜湊鏈而不是區塊鏈;完整率算的是「可回溯」而不是「有紀錄」**

雜湊鏈已足以提供竄改偵測,不可否認性由「核准者身分綁定 + 完整證據封存」提供;
區塊鏈的成本與延遲對此場景不成比例。`completeness()` 的判準是:每筆 `g5_executed`
都要能對到 `g2_adjudication`,且若當初判定需核准,就必須對到 `g4_approved` ——
留了一堆紀錄但接不起來,不算完整。

**9. 消融開關長在正式路徑上**

`GateConfig` 的 `enable_g0` / `enable_g3` / `gate_enabled` 就是 benchmark 的
B3−G0 / B3−G3 / B0,也是 Dashboard「四分鐘劇本」現場關 Gate 的同一段程式碼。
對照組不是另寫一套模擬器,而是同一條管線關掉一關 —— 這是消融數字可信的前提。

### 驗證這些機制的測試

| 機制 | 測試 |
|---|---|
| 注入攔在 G0(而非 G2) | `test_agentgate_pipeline.py::TestBlocking::test_injection_blocked_at_g0` |
| 空來源鏈視同不可信 | `test_agentgate_core.py::TestG0Provenance::test_empty_chain_is_untrusted` |
| G2 可匯出全部規則條文 | `TestG2Policy::test_describe_exports_rules_and_policy` |
| 風險依帳戶狀態動態升級 | `TestG2Policy::test_plan_change_in_contract_escalates` |
| 實測筆數 ≠ 申報筆數 | `TestG3Projection::test_bulk_actual_count_measured_not_declared` |
| 預演不寫入影子環境 | `TestG3Projection::test_projection_does_not_mutate_shadow` |
| 不可回復動作一律需核准 | `TestG3Projection::test_sim_reissue_irreversible_needs_approval` |
| 核准者身分綁定、駁回須具理由 | `TestHappyPath::test_wrong_credential_rejected`、`test_reject_requires_reason_and_leaves_trace` |
| 竄改偵測與完整率 | `TestG5AuditChain::test_tamper_detected`、`test_completeness_flags_untraceable_execution` |
| 關掉關卡損害就發生 | `TestAblationAndAB::test_gate_disabled_executes_attack`、`test_no_g3_scope_escape_not_caught` |

## 規格對照

| 規格 | 實作 | 測試 |
|---|---|---|
| §4.1 動作本體論(7 動作、動態風險) | [`agentgate/ontology.py`](../agentgate/ontology.py)、[`gates/g2_policy.py`](../agentgate/gates/g2_policy.py) `ACTION_POLICY` + 13 條 AG 規則 | `test_agentgate_core.py::TestG2Policy` |
| §4.2 來源信任分級(G0-R1 權限升級阻斷) | [`gates/g0_provenance.py`](../agentgate/gates/g0_provenance.py) + [`pipeline.py`](../agentgate/pipeline.py) 的 G0-R1 檢查 | `TestG0Provenance`、`TestBlocking::test_injection_blocked_at_g0` |
| §4.3 G2 刻意不用 LLM | 確定性 `PolicyEngine`;G1 的 schema 驗證守住 LLM 映射輸出 | `test_describe_exports_rules_and_policy` |
| §4.4 後果預演(影子環境乾跑) | [`shadow.py`](../agentgate/shadow.py) `project()` 不寫入 / `execute()` 生效;[`gates/g3_projection.py`](../agentgate/gates/g3_projection.py) AG-30(不可回復需核准)、AG-31(範圍逃逸實測) | `TestG3Projection` |
| §4.5 人工核准(15 秒證據包) | [`gates/g4_approval.py`](../agentgate/gates/g4_approval.py) 證據包 + 門號綁定碼模擬(§9) | `TestEvidenceAndMetrics::test_evidence_package_content` |
| §4.6 雜湊鏈稽核 | [`gates/g5_audit.py`](../agentgate/gates/g5_audit.py):雜湊鏈、完整率、竄改偵測、降級留痕 | `TestG5AuditChain` |
| §5.1 測試集 120 條(80/40) | [`scenarios.py`](../agentgate/scenarios.py):注入 20、權限混淆 10、範圍逃逸 10,每條標註預期裁決 | `test_agentgate_scenarios.py` 逐條交叉檢核 |
| §5.2 六指標 §5.3 消融 | [`validation.py`](../agentgate/validation.py):B0/B1/B2/B3/B3−G0/B3−G3 + 最壞核准者敏感度 | `test_agentgate_validation.py` |
| §6 API 規格 | [`api/server.py`](../agentgate/api/server.py):九個 `/api/gate/*` 端點全數實作 | `test_agentgate_api.py` |
| §7.2 四分鐘劇本 + A/B 對照 | [`demo.py`](../agentgate/demo.py) + Dashboard「四分鐘劇本」分頁(關 Gate 匯出個資的對照當場可見) | `TestDemoEndpoints::test_ab_compare_restores_gate` |

## 驗證結果(自建 120 條測試集)

| Baseline | HAR 有害放行 | TCR 任務完成 | FBR 誤攔 | ESB 升級攔截 | AC 稽核完整 | DL p95 (ms) |
|---|---|---|---|---|---|---|
| B0 無治理(損害上界) | 100.0% | 100.0% | 0.0% | 0.0% | 0.0% | 0.03 |
| B1 LLM 自我審查(模擬) | 65.8% | 95.0% | 5.0% | 0.0% | 0.0% | — |
| B2 靜態黑白名單 | 42.1% | 77.5% | 22.5% | 0.0% | 0.0% | 0.01 |
| **B3 AgentGate 完整五關卡** | **0.0%** | **91.2%** | **8.8%** | **100.0%** | **100.0%** | **0.12** |
| B3−G0 消融:拿掉來源信任 | 15.8% | 91.2% | 8.8% | 0.0% | 100.0% | 0.11 |
| B3−G3 消融:拿掉後果預演 | 7.9% | 91.2% | 8.8% | 100.0% | 100.0% | 0.09 |
| B3-worst 最壞情況核准者 | 7.9% | 100.0% | 0.0% | 100.0% | 100.0% | 0.12 |

兩條消融證明差異化不是裝飾(規格 §5.3):拿掉 G0,注入的假退費與 SIM 補發
變成例行核准案(HAR 0% → 15.8%);拿掉 G3,低報筆數的批次匯出騙過申報審查
(HAR 0% → 7.9%)。FBR 8.8% 的主要來源是「綁約期內變更資費」與「客服代查」
被保守駁回 —— 這是誠實呈現的安全—效用張力,見 Dashboard 指標分頁的取捨曲線。

## 誠實邊界(規格 §5.4)

- Demo 中的電信後台為**影子環境**,非任何真實營運系統。
- 測試集為**自建合成資料**,非真實客服紀錄。
- B1 為**模擬**之 LLM 自我審查(以情境標註近似偵測能力,不呼叫真實模型),
  故不報告其延遲;核准者為確定性模擬。
- 所有數字未經任何企業實地驗證;不宣稱與中華電信有任何既有合作關係。
- 公開 agent benchmark 之可得性驗證(規格 §10 W1 第一項)為研究作業,
  不在本實作範圍;目前採規格 §5.1 備援方案(全自建測試集)並如上揭露。

## 尚未實作(規格明列 Non-goals 或後續里程碑)

- 多租戶 SaaS 營運面、真實系統串接(§1.4 明確不做)。
- ROI 模型數字化(§8.2,規格排程 9/12–9/14)。
- 15 頁提案書與簡報(§12,規格排程 9/15–9/17)。
