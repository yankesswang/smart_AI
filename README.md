# AegisMesh 天穹韌網

> 海地星空多網融合的 Agentic AI 通訊韌性數位孿生平台
> 2026 中華電信智慧創新應用大賽｜社會組

當光纖中斷、基地台壅塞或災害發生時，AI Agent 在數位孿生中推演替代方案，
再協調固網、5G、衛星與邊緣資源，確保關鍵醫療服務不中斷。

---

## 快速開始

```bash
pip install -e .
cp .env.example .env        # 填入 OPENAI_API_KEY（留空也能跑，見下方「離線退化」）

python -m aegismesh.cli serve                       # 即時戰情室 → http://127.0.0.1:8000
python -m aegismesh.cli topology                    # 看拓樸與基準狀態
python -m aegismesh.cli scenarios                   # 列出災害情境
python -m aegismesh.cli demo typhoon-fiber-cut      # 跑完整閉環（含人工核准閘門）
python -m aegismesh.cli episode typhoon-fiber-cut   # 整場事件推演：AI 配速 vs 人工經驗法則
python -m aegismesh.cli audit runs/audit-*.jsonl    # 驗證稽核軌跡雜湊鏈
pytest                                              # 90 項不變式測試
```

---

## 閉環：Observe → Plan → Simulate → Approve → Execute → Verify

| 階段 | 元件 | 誰做決定 |
|---|---|---|
| ① Observe | `TelemetryAgent` | 門檻式異常偵測（引擎量測值） |
| ② Assess | `ImpactAgent` | 業務／SLA 對照表（引擎） |
| ③ Plan | `PlanningAgent` → `optimizer` | **約束最佳化演算法** |
| ④ Simulate | `DigitalTwin.clone()` | **影子孿生推演** |
| ⑤ Approve | `PolicyAgent` → `policies.yaml` | **Policy-as-Code ＋ 人工閘門** |
| ⑥ Execute | `Orchestrator` | 唯一有權變更網路的元件 |
| ⑦ Verify | `VerificationAgent` | 實測 vs 預測比對，失敗自動重規劃 |

### LLM 的角色邊界（可信度的來源）

每個 Agent 都是兩層結構：

- **facts** — 由孿生引擎、最佳化器、政策引擎算出的決定性事實。可重現、可稽核。
- **narrative** — LLM 對這些事實的自然語言解釋。

**LLM 從頭到尾沒有權限決定路徑、頻寬或核准與否。** 它說錯話不會導致錯誤的網路變更。
`test_agent_facts_are_deterministic_across_runs` 就是在證明這件事：同一情境跑兩次，
引擎算出的事實與最終執行的計畫必須完全一致。

### 離線退化

沒有 `OPENAI_API_KEY`、金鑰失效、或 API 逾時時，整套閉環仍然跑得完，
只是敘述改由決定性樣板產生 —— **所有數值與決策不受影響**。
評審現場不會因為外部相依而開天窗。

---

## 即時戰情室（`aegismesh serve`）

`http://127.0.0.1:8000` 是六分鐘舞台 Demo 的主畫面，分成兩個分頁。

**① 災害當下的閉環** —— 事故發生那一刻的一次決定：

- **網路拓樸** — 鏈路顏色隨狀態變化（中斷紅色虛線、劣化琥珀色流動、正常依 WAN 類型上色），
  線寬隨使用率成長，讓「壅塞」在畫面上看得見。
- **即時指標** — 關鍵可用率 / SLA 達成率 / 月度成本 / 衛星配額還能撐幾小時，
  隨閉環進度更新並對照基準。
- **Agent 決策軌跡** — 每個 Agent 的敘述即時串流，含災害注入的 `tc/netem` 指令。
- **候選計畫表** — 四個策略的推演結果與政策裁決並列。
- **人工核准對話框** — 列出政策發現與將執行的動作，值班主管按下去才會執行。

**② 整場事件推演** —— 同一場災害沿時間軸跑兩次（`GET /api/episode/{id}`，純演算法、無 LLM）：

- **結論數字** — AegisMesh 讓關鍵醫療服務多正常運作幾小時。
- **逐時段車道** — 兩種策略每個時段各採用了哪個方案、關鍵服務可用率、配額還剩多少。
- **配額曲線** — 人工經驗法則的衛星配額在半途觸底，AegisMesh 剛好撐完全程。
  兩條線在前段幾乎重疊 —— **決策分岔的時刻與代價浮現的時刻相隔數小時**，這就是需要推演的理由。
- **誰讓出了頻寬** — 每個時段每項醫療服務拿到多少、對照該時段的完整需求。

### 同步閉環 ↔ 非同步 WebSocket 的橋接

人工核准本來就該**阻塞**，WebSocket 卻是非同步的。兩者用 worker thread 橋接：

```
orchestrator.run()  ← worker thread，approval_fn 在此阻塞
    │ emit(kind, payload) → loop.call_soon_threadsafe
    ▼
asyncio.Queue → WebSocket → 瀏覽器
    │
    └─ 按下「核准」→ threading.Event.set() → 解除 worker 阻塞
```

核准結果刻意用 **threading.Event 而非 asyncio.Future** 傳遞。早期版本走
`run_coroutine_threadsafe`，結果是瀏覽器在核准前關掉分頁時，worker thread 會卡滿
5 分鐘逾時才醒來——因為結果的「送達」依賴事件迴圈還活著。反覆重整就會累積殭屍執行緒。
`test_disconnect_before_approval_does_not_leak_worker_threads` 守住這一點。

---

## 數位孿生引擎

`aegismesh/twin/engine.py` 沒有任何 LLM，全部是確定性計算：

- **壅塞模型** — M/M/1 排隊近似。延遲 = `base·(1 + ρ/(1−ρ))`，超載部分視為丟包。
  這讓「使用率 80%」自動翻譯成「延遲變 5 倍」，是允入控制存在的理由。
- **路徑計算** — NetworkX k-shortest paths，自動排除中斷鏈路。
- **成本模型** — 固網 NT$0.08/GB、5G NT$0.55/GB、衛星 NT$4.20/GB，反映真實價差。
- **影子孿生** — `clone()` 完全隔離，讓「先推演、後執行」是真的而非口號。

### MVP 範圍（刻意壓在提案限制內）

12 節點 · 13 鏈路 · 6 業務 · 3 條 WAN 路徑（固網／5G／衛星）· 3 個災害情境 · 5 個 Agent

| 業務 | 優先級 | 延遲 SLO | 需求頻寬 | 臨床意義 |
|---|---|---|---|---|
| 急診生命徵象串流 | P0 | 120ms | 25M | 檢傷與急救決策依據 |
| ICU 生理監測 IoT | P0 | 150ms | 15M | 中央監視警報 |
| 遠距診療視訊 | P1 | 200ms | 60M | 可降級為關鍵影格＋語音 |
| 醫療影像 PACS | P2 | 800ms | 300M | 可延後續傳 |
| HIS 行政批價 | P3 | 400ms | 80M | 可容忍短暫降速 |
| 訪客 Wi-Fi | P4 | 800ms | 200M | 災害期間可完全停用 |

---

## 落地路徑：本機模擬 → containerlab

每個故障注入都能一對一翻譯成真實 Linux 指令，模型不是為了 Demo 而編造的：

```
$ ip link set dev eth0 down                                    # w-fiber 光纖中斷
$ tc qdisc replace dev eth0 root netem delay 25ms loss 0.25%   # w-5g 基地台壅塞
```

`Fault.netem_command()` 就是這個介面（`test_faults_translate_to_runnable_netem_commands` 守住它）。
下一階段把 `DigitalTwin` 換成 containerlab adapter 時，上層的 Agent、最佳化器、
政策引擎完全不需要改。

---

## 治理層：Policy-as-Code

規則寫在 `aegismesh/policy/policies.yaml`，程式只實作有限幾種 `kind`。
治理邏輯可被法遵人員閱讀，而不是散落在 prompt 裡。

| 規則 | 效果 | 內容 |
|---|---|---|
| POL-001 | **拒絕** | 關鍵業務可用率不得低於事故當下 |
| POL-002 | **拒絕** | 禁止優先級反轉（不得讓訪客有頻寬而批價系統掛零） |
| POL-003 | **拒絕** | P0 業務不得低於臨床最低頻寬 |
| POL-010 | 需核准 | 衛星鏈路啟用（成本為固網 50 倍以上） |
| POL-011/012 | 需核准 | 月成本上限 NT$90,000／增幅 3 倍上限 |
| POL-020 | 需核准 | 變更爆炸半徑上限 6 個動作 |
| POL-021 | 需核准 | 停用 P3 以內業務 |

**PolicyAgent 沒有裁量權**：`decision` 由規則引擎產生，LLM 只負責寫核准說明，
不可能把 `deny` 講成 `allow`。未經孿生推演的計畫一律拒絕（`POL-000`）。

### 稽核軌跡

雜湊鏈 JSONL（`runs/audit-*.jsonl`）。每筆含前一筆的 SHA-256，
竄改內容或刪除紀錄都會讓鏈結斷裂並被 `aegismesh.cli audit` 偵測出來。

---

## 執行結果（颱風致光纖中斷 ＋ 5G 壅塞）

| 指標 | 事故前 | 事故當下 | 復原後 |
|---|---:|---:|---:|
| 關鍵業務可用率 | 100% | **0%** | **100%** |
| 整體 SLA 達成率 | 100% | **0%** | **100%** |
| 月度通訊成本 | NT$19,829 | — | NT$148,165 |

三個情境的閉環結果都由 `test_closed_loop_recovers_critical_services` 守住。

---

## 專案結構

```
aegismesh/
├── domain.py           # 領域模型（Node/Link/Service/Plan/Fault）
├── config.py           # .env 設定載入
├── llm.py              # OpenAI 串接 ＋ 離線退化
├── optimizer.py        # 優先級允入控制 ＋ 迭代壅塞收斂
├── orchestrator.py     # 閉環主控 ＋ 人工核准閘門
├── audit.py            # 雜湊鏈稽核軌跡
├── cli.py              # 終端機 Demo 入口
├── api/
│   ├── server.py       # FastAPI ＋ WebSocket（同步閉環 ↔ 非同步橋接）
│   └── static/         # 戰情室單頁介面
├── twin/
│   ├── engine.py       # 數位孿生（壅塞、SLA、成本計算）
│   ├── topology.py     # 智慧醫院園區拓樸
│   └── scenarios.py    # 三個災害情境
├── policy/
│   ├── engine.py       # 規則評估引擎
│   └── policies.yaml   # Policy-as-Code
└── agents/             # 5 個 Agent（telemetry/impact/planning/governance/verification）
```

---

## 下一階段

- [x] FastAPI ＋ WebSocket 即時儀表板（六分鐘舞台視覺化）
- [ ] Prometheus exporter，接 Grafana
- [ ] containerlab adapter：把孿生換成真實流量與 tc/netem
- [ ] Neo4j 知識圖譜，取代目前的 NetworkX 記憶體圖
