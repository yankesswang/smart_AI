# 演算法規格

這份文件說明 Factory Guardian AI 每一個決策環節「實際上算了什麼」：輸入、公式、常數、
以及為什麼是這個設計。所有常數都可在原始碼對照，行號以 `檔案:符號` 標示。

README 的〈幾個關鍵設計決定〉講的是**為什麼**；這份文件講的是**怎麼算**。

---

## 目錄

1. [資料流與閉環](#1-資料流與閉環)
2. [Digital Twin：訊號生成模型](#2-digital-twin訊號生成模型)
3. [Monitoring：平滑、健康度、趨勢、觸發](#3-monitoring平滑健康度趨勢觸發)
4. [Diagnosis：交機基準 + 手冊徵兆 + 鑑別規則](#4-diagnosis交機基準--手冊徵兆--鑑別規則)
5. [信念孿生體：規劃模型的建立](#5-信念孿生體規劃模型的建立)
6. [Safety：政策閘門與預測型規則](#6-safety政策閘門與預測型規則)
7. [Optimizer：固定尺規多準則排名](#7-optimizer固定尺規多準則排名)
8. [Verification：反事實驗證](#8-verification反事實驗證)
9. [TabFM：時序預測](#9-tabfm時序預測)
10. [常數總表](#10-常數總表)

---

## 1. 資料流與閉環

```
Digital Twin ──讀值──> Monitoring ──異常事件──> Diagnosis
                                                    │
                                          信心不足 → 繼續觀察
                                                    │ 信心足夠
                                                    v
                                            信念孿生體 fork
                                                    │
                                    Production 在信念模型上乾跑數個方案
                                                    │
                                              Safety 逐案審查
                                                    │
                                            Optimizer 加權排名
                                                    │
                                        （需要時）人工核准
                                                    │
                                          在真實孿生體上執行
                                                    │
                                            Verification 反事實比對
                                                    │
                                          未達預期 → 重新規劃（至多 2 次）
```

關鍵分界線：**Diagnosis 之後的所有規劃都跑在「信念模型」上，不是真實孿生體**。
這確保診斷錯誤會真的傳導成規劃錯誤，而不是被 Ground Truth 掩蓋。

---

## 2. Digital Twin：訊號生成模型

`twin/engine.py`、`twin/topology.py`、`twin/faults.py`

### 2.1 訊號規格

每個訊號有標稱值、警告/危險門檻與正規化尺度（`twin/topology.py`）：

| 訊號 | 標稱 | warning | critical | scale |
|---|---|---|---|---|
| temperature | 62.0 °C | 70.0 | 80.0 | 10.0 |
| vibration | 2.4 mm/s | 4.0 | 7.0 | 3.0 |
| current | 10.2 A | 8.0 / 12.0 | 14.0 | 2.0 |
| rpm_pct | 99.0 % | 95.0（低） | 85.0（低） | 10.0 |

`scale` 的選法有明確意義：**讓「剛好踩到 critical 門檻」的偏離量正規化後等於 1.0**。
因此 `deviation = |value − nominal| / scale` 是跨訊號可比的無因次量。

### 2.2 故障演進

故障以 `progress` 參數驅動，訊號為標稱值加上線性偏移：

```
signal(t) = nominal + deltas[signal] × progress(t)
```

`progress` 依 `fault_ramp_ticks` 線性爬升。`deltas` 是各故障的特徵向量
（`twin/faults.py`），例如軸承劣化以振動為主、冷卻失效以溫度為主。

> **重要**：`FAULTS[].deltas` 是模擬器**生成**訊號用的參數，屬於 Ground Truth。
> Diagnosis Agent 不得存取（見 §4.0）。

---

## 3. Monitoring：平滑、健康度、趨勢、觸發

`agents/monitoring.py`

### 3.1 滑動視窗平滑

視窗長度 `WINDOW = 6` tick。平滑值為**線性加權平均**（越新權重越高）：

```
smoothed = Σ(vᵢ × wᵢ) / Σwᵢ ,  wᵢ = i + 1  (i 由舊到新，0-indexed)
```

不用簡單平均是因為要壓雜訊但不能延遲太多；不用 EWMA 是因為固定視窗的
斜率計算比較直接。

### 3.2 健康度（Equipment Health Score）

**由 Agent 從觀測值自行計算，不是讀模擬器的內部狀態**——這是可信度的前提。

```
S = Σ HEALTH_WEIGHTS[signal] × min(2.0, deviation(smoothed[signal]))
health = clamp(100 × (1 − S / HEALTH_FULL_SCALE), 0, 100)
```

權重（`twin/topology.py:HEALTH_WEIGHTS`）：振動 0.35、溫度 0.30、電流 0.20、轉速 0.15。
振動與溫度最能代表機械劣化，故權重最高。

`min(2.0, ·)` 讓單一訊號的貢獻封頂，避免一個爆表訊號淹沒其他訊號。
`HEALTH_FULL_SCALE = 1.6` 使健康度在**明顯超過** critical 時才逼近 0，
而非剛踩線就歸零。

### 3.3 趨勢

以最小平方法求視窗內斜率，再除以 `scale` 正規化（單位：scale/tick）：

```
slope = Σ(xᵢ − x̄)(yᵢ − ȳ) / Σ(xᵢ − x̄)²
trend = slope / scale
```

`rpm_pct` 方向相反（越低越糟），計算時取負號。`worst_trend` 取所有訊號中
最強的「往壞的方向走」的斜率，至少需 3 點才計算。

### 3.4 故障風險

```
failure_risk = clamp((100 − health)/100 + max(0, trend) × 1.6, 0, 1)
```

同時考慮**現況**與**惡化速度**。係數 1.6 讓「健康度尚可但快速惡化」也能觸發警示——
這正是早期偵測的價值所在。

### 3.5 觸發

依讀值所在 band 決定嚴重度：任一訊號 CRITICAL → `Severity.CRITICAL`；
有 WARNING 或健康度低於門檻 → `Severity.WARNING`。

---

## 4. Diagnosis：交機基準 + 手冊徵兆 + 鑑別規則

`agents/diagnosis.py`、`knowledge/symptom_spec.py`、`knowledge/commissioning.py`

### 4.0 為什麼不用餘弦相似度

早期版本把 `FAULTS[].deltas / scale` 當作「感測器指紋」讓 Agent 比對。
這在數學上站不住腳：模擬器生成訊號用的是**同一份** `deltas`，
故觀測向量 ≈ `deltas × progress / scale`，與指紋**共線**。
而餘弦相似度對純量免疫，因此正確答案的餘弦**恆等於 1.0**——那是查表，不是診斷。

現行版本的所有判讀依據都來自「工程師手上真的會有的文件」。
耦合已切斷這件事寫成測試保護：

- `test_manual_symptom_ranges_are_decoupled_from_simulator_deltas`
- `test_diagnosis_never_imports_simulator_fault_parameters`

### 4.1 步驟一：以交機基準算偏離量

同型號設備的實際基準值不同（組裝公差、地基剛性、環境、安裝水平度）。
套用通用門檻會**同時造成漏檢與誤報**，這是真實預測性維護的核心問題。

```
excess = |value − baseline| − tolerance
deviation_ratio = 0                if excess ≤ 0        # 落在允收帶內
                = excess / tolerance if excess > 0        # 超出幾個帶寬

delta = sign(value − baseline) × deviation_ratio
```

允收帶（tolerance）內算 0，超出才開始累積。量測散布大的機器本來就需要
更大的變化才算異常。無交機記錄時退回 `(value − nominal) / scale`。

### 4.2 步驟二：手冊徵兆區間符合度（權重 0.38）

手冊寫的是**區間**不是點值。`SymptomRange.score()`：

```
offset = |delta − center| / half_width                        # 0（正中心）~ 1（邊緣）

區間內：  score = 1.0 − 0.28 × offset²                        → 1.0 ~ 0.72
區間外：  score = 0.72 × exp(−0.5 × (excess / half_width)²)   → 自 0.72 起高斯衰減
```

區間內用 **offset 的平方**，使中心附近較平坦、接近邊緣才明顯掉分。
區間外的衰減以 **0.72 為起點**（即區間邊緣的分數），確保函數在邊界連續——
剛好落在邊界內與剛好落在邊界外不會出現跳變。

兩個設計要點：

- **區間內不是一律 1.0**。這些區間刻意設得寬且互相重疊（現實就是如此），
  若一律滿分則三個候選同時貼在 1.0，這一項失去鑑別力。給中心一點優勢，
  可在「都符合」時仍分得出「誰更典型」。
- **區間外用高斯衰減而非歸零**。手冊區間是經驗值，略微超出不該直接判死。

各訊號依手冊標註權重加權平均。

### 4.3 步驟三：鑑別診斷規則（權重 0.44，最高）

三種故障的徵兆區間**大幅重疊**——現實中它們都會溫升。
真正能分辨的是**比值**，這也是工程師的實際判準：

```
ratio = Δnumerator / Δdenominator
區間內：1.0
區間外：exp(−0.5 × (excess / half_width)²)
```

例如 ΔVib/ΔTemp 高 → 偏向機械性劣化；ΔRPM/ΔCurrent 的關係則分辨負載異常。

**分母訊號沒動時該規則不適用**（回傳 `None`），不計入加權平均——
不能因為「溫度沒變」就把一條溫度相關的規則算成 0 分。
全部規則都不適用時回傳 0.5（中性），避免「無資訊」變成「懲罰」。

### 4.4 綜合排名

```
combined = 0.38 × manual + 0.44 × differential + 0.10 × prior + 0.08 × docs
```

| 項目 | 權重 | 來源 |
|---|---|---|
| 鑑別診斷規則 | **0.44** | `symptom_spec.py` 比值規則 |
| 手冊徵兆區間 | 0.38 | `symptom_spec.py` 區間 |
| 歷史先驗 | 0.10 | 該機台過去案例，近期加權 |
| RAG 文件支持 | 0.08 | 檢索文字相似度 |

RAG 權重最低是刻意的：文字相似度容易被措辭主導（見 §4.6）。

### 4.5 信心度

先 softmax（溫度 `0.16`），再**依訊號強度往均勻分布拉**：

```
sharp[i]  = exp((combinedᵢ − max) / 0.16) / Σexp(·)
certainty = clamp(strength / STRENGTH_FULL, 0, 1)          # STRENGTH_FULL = 1.2
conf[i]   = uniform + (sharp[i] − uniform) × certainty      # uniform = 1/n
```

訊號還微弱時不該給 90% 信心。典型信心度因此落在 **51~81%** 而非逼近 100%——
**那個較低的數字才是誠實的**。

偏離量低於 `NO_FAULT_STRENGTH = 0.45` 時判定為「無設備故障徵兆」。

Orchestrator 在信心低於 `min_confidence = 0.65` 時會**先繼續觀察**
（`confirm_diagnosis`，至多 `max_confirm_ticks = 10` tick），不急著動設備。

### 4.6 檢索查詢的措辭

檢索查詢是 Agent 自己寫的問題，**問錯了召回再準也沒意義**。

早期版本只看正規化偏離量就寫「明顯上升」，導致一個 vibration 2.9 mm/s
（低於警告門檻、band 仍是 normal）的典型冷卻失效，查詢裡寫成「vibration 明顯上升」，
幾乎照抄軸承手冊的措辭，於是文字相似度把軸承劣化推到 1.0，**壓過正確答案**。

現在措辭必須忠實於訊號的 band：normal band 一律描述為「維持正常範圍」。

---

## 5. 信念孿生體：規劃模型的建立

`twin/engine.py:fork_as_belief`

方案投影若直接跑在帶 Ground Truth 的孿生體上，等於讓 Production Agent 偷看答案，
診斷正確與否就不影響結果。所以規劃模型這樣建：

1. `fork()` 複製孿生體
2. 拿掉真實故障標籤，換上 **Diagnosis 推論出來的**故障
3. 用**觀測到的健康度**反推嚴重程度（`estimate_progress_for`，二分搜尋）
4. 訊號直接跳到該信念下的穩態，避免規劃模型一開始就有假的暫態

反推用的 `health_at(progress)` 與 §3.2 的健康度公式一致：

```
health_at(p) = clamp(100 × (1 − Σ w × min(2, deviation(nominal + deltas × p)) / 1.6), 0, 100)
```

二分搜尋區間 `[0, 2.5]`。**診斷錯了投影就會錯**，再由 Verification 抓出來觸發重試。

---

## 6. Safety：政策閘門與預測型規則

`agents/safety.py`、`policy/engine.py`

Safety 是**政策閘門不是建議者**：`BLOCK` 是硬限制，方案直接出局。

### 6.1 規則表

規則分兩處：`policy/engine.py:SAFETY_RULES` 是主規則表，逐條對方案審查；
`agents/safety.py` 另有三條專門檢查**接手機台**（承接轉移產能的那一台）。

主規則表（`policy/engine.py:SAFETY_RULES`）：

| 規則 | 說明 | 裁決 | 依據 |
|---|---|---|---|
| SR-01 | 人員進入運轉中危險區 | BLOCK | SOP-SF-01 |
| SR-02 | 振動超過危險門檻仍全速運轉 | BLOCK | MAN-A-3.2 |
| **SR-02P** | **方案預測將使振動進入危險區** | BLOCK | MAN-A-3.2 |
| SR-03 | 高溫仍維持運轉 | BLOCK | SOP-SF-01 |
| **SR-03P** | **方案預測將使溫度進入起火風險區** | BLOCK | SOP-SF-01 |
| SR-04 | 偵測到煙霧仍維持運轉 | BLOCK | SOP-SF-01 |
| SR-05 | 偵測到人員跌倒 | BLOCK | SOP-SF-01 |
| SR-06 | 維修同時維持運轉違反 LOTO | BLOCK | SOP-MT-04 |
| SR-07 | Safety Override 禁止 | BLOCK | SOP-SF-01 |
| **SR-13** | **方案預測將使設備劣化到不可接受** | BLOCK | MAN-A-5.3 |
| SR-08 | PPE 不完整 | APPROVAL | SOP-SF-01 |
| SR-09 | 維修需 LOTO 與人工核准 | APPROVAL | SOP-SF-01 |

接手機台檢查（`agents/safety.py`）——避免「把負載轉移到另一台快壞的機器」：

| 規則 | 條件 | 裁決 |
|---|---|---|
| SR-10 | 接手機台振動 > 7.0 mm/s | BLOCK |
| SR-11 | 接手機台振動 > 4.0 mm/s | APPROVAL |
| SR-12 | 接手機台溫度 > 80.0 °C | BLOCK |

### 6.2 預測型規則（`-P` 與 SR-13）

早期偵測的價值在訊號還微弱時就介入——但那時候「維持全速運轉」在**當下並不違規**。
所以預測型規則拿**方案乾跑出來的訊號峰值**判斷，而非當下讀值。

實際 Demo 中它會這樣擋下 PLAN-A：

> 模擬顯示本方案會讓 M-A 振動由目前 5.65 mm/s 升至 14.18 mm/s，超過危險門檻 7 mm/s。

---

## 7. Optimizer：固定尺規多準則排名

`optimizer.py`。**純計算，無 LLM 參與。**

### 7.1 流程

1. **硬限制**：Safety `BLOCK` → `feasible = False`
2. 抽出六準則原始值（方向統一為越大越好）
3. 依**固定尺規**換算 0~1
4. 加權求和

### 7.2 固定尺規（不用 min–max）

min–max 是相對比較：候選在某準則差距很小時，正規化仍會把最好的拉到 1、
最差的壓到 0，**無中生有製造鑑別力**——一個 0.15 的實質差距可整碗端走 0.30 的權重。

```
safety      = clamp(raw, 0, 1)                              # 已是絕對尺度
equipment   = clamp(raw, 0, 1)                              # 1 − 殘餘風險
production  = clamp(raw / 100.0, 0, 1)
delivery    = max(0, 1 − min(raw, 240) / 240)               # 延遲 4 小時 → 0 分
recovery    = max(0, 1 − min(raw, 120) / 120)               # 復原 2 小時 → 0 分
cost        = max(0, 1 − min(raw, 300000) / 300000)         # 30 萬 NTD → 0 分
```

固定尺規讓分數有絕對意義：0.83 分在不同情境、不同執行間都代表同一件事。

### 7.3 權重

```
safety 0.30 | delivery 0.22 | production 0.20 | recovery 0.12 | cost 0.08 | equipment 0.08
```

（使用前會正規化為總和 1.0）

### 7.4 安全裕度轉分

```
BLOCK             → 0.0     （硬限制，方案已出局）
APPROVAL_REQUIRED → 0.9     （只扣 0.1）
其他              → max(0.8, 1 − 0.02 × findings 數)
無 Safety 資訊    → 0.5
```

**`APPROVAL_REQUIRED` 只扣 0.1** 是刻意的：它代表「需要人簽名」這個治理程序，
不是「這個方案比較危險」。早期版本扣到 0.4（裕度 0.6），結果系統會為了避開核准流程
而偏好「不用人簽名但其實比較糟」的方案——那是把治理成本誤當成安全風險。

### 7.5 未復原懲罰

```
recovery_raw = proj.recovery_min + (0 if proj.recovered else 60.0)
```

「整段都沒回到門檻」和「剛好在最後一刻回來」不能拿同樣的復原分數，
否則「放著不管」會和真正的復原方案並列。

---

## 8. Verification：反事實驗證

`agents/verification.py`

設備劣化是進行式。拿 30 分鐘後的結果去比事故剛發生時的達成率，**任何方案都必定變差**，
那個比較沒有意義。真正該回答的是兩個問題：

1. **實際結果有沒有達到這個方案自己的預測？**（預測可信嗎）
2. **有沒有贏過「什麼都不做」？**（介入有價值嗎）

容差：

```
PRODUCTION_TOLERANCE_PCT = 12.0
DELAY_TOLERANCE_MIN      = 15.0
HEALTH_TOLERANCE         = 8.0
```

反事實比較**只在「什麼都不做」本身合法時才成立**（`counterfactual_valid`）——
工安事件本來就是用產能換安全，那不是失敗，那正是系統該做的事。

未達預期時 Orchestrator 觸發重新規劃，至多 `max_attempts = 2` 次。

---

## 9. TabFM：時序預測

`prediction/service.py`

### 9.1 為什麼用表格模型做時序

TabFM 是**表格基礎模型**，不是原生序列模型。本模組把時序問題轉成監督式表格迴歸，
藉此利用 in-context learning——**不需要為每台機台訓練專屬模型**。

### 9.2 特徵工程

每列 10 個特徵：

```
tick, value, lag_1, lag_3, rolling_mean_3, rolling_mean_6,
rolling_std_6, slope_3, factory_health, production_pct
```

訓練對為 `(features[t] → value[t+1])`，取最近 `context_window`（預設 60）筆。
至少需 8 個時間點。

**資料來源限定 Agent 可見的遙測歷史**，不含模擬器 Ground Truth 或 `fault_progress`。

### 9.3 遞迴預測

每次預測值餵回去當下一列的 `lag`/`value`，跑滿 horizon。

### 9.4 信賴區間

```
residual = max(0.15, |最後一次變化|, pstdev(最近 6 次變化))
spread   = residual × √index × 1.64        # index 為往前第幾步
```

`√index` 反映不確定性隨步數累積；`1.64` 對應單尾 95%（雙尾 90%）。

health 目標另有門檻 75.0，並計算風險機率與 `threshold_crossing_tick`：

```
risk = 1 / (1 + exp((value − 75.0) / max(1, spread)))
```

### 9.5 Runtime 與降級

| 模式 | 行為 |
|---|---|
| `auto` | TabFM 可用則用，否則降級 Ridge 並標示原因 |
| `tabfm` | 明確指定；未安裝直接拋錯，**不偷偷降級** |
| `ridge` | 零依賴確定性基線 |

`auto` 模式下若 TabFM **推論中途**失敗，會即時切換 Ridge 並在回應標註。

Ridge 基線為手刻實作（標準化 → Gram 矩陣 → 高斯消去，矩陣僅 11×11），
無額外 ML 依賴，供本機 Demo、CI 與模型服務故障時使用。

### 9.6 效能設計

- **權重快取**：`release.load()` 約 8 秒，adapter 由 `ForecastService` 共用，
  每個 process 只付一次。
- **`batch_size=None`**：一次前向全部 ensemble member 而非逐一迴圈，
  predict 由 1.17s 降至 0.68s，輸出不變（bfloat16 累加順序造成 ~2e-4 差異）。
- **fit 指紋**：以訓練資料內容 hash 避免遞迴步驟間重複 fit。
  adapter 跨 request 共用，故指紋取**內容**而非僅形狀，以免後續機台沿用前次的 fit。

> `release.load()` 預設載入 **classification** checkpoint，
> regression 必須明確指定 `model_type="regression"`，否則每列回傳 10 個值而失敗。

### 9.7 閉環用 Ridge，工作台用 TabFM

Monitoring 的預測式告警**固定使用 Ridge**（`monitoring.py: FORECAST_MODEL`），
不走 `auto`。原因不是效能，而是 TabFM 遞迴外推時會回歸 context 均值：
單步預測準確（誤差 +1.2），但走滿 12 步後會預測劣化中的機台自行好轉，
而那正是預測告警唯一有價值的時間窗。

`FACTORY_GUARDIAN_TABFM_DEVICE=cuda` 可讓工作台的 TabFM 走 GPU
（實測 7.7 s → 0.50 s），預設 `cpu`；`pyproject.toml` 仍釘 CPU-only torch，
GPU 為操作者自行安裝後以環境變數啟用，不列為專案依賴。

> 完整實驗數據、四個假說的驗證與推翻過程，見
> [`forecast-model-evaluation.md`](forecast-model-evaluation.md)。

### 9.8 授權

TabFM 程式碼為 Apache-2.0，但 v1.0.0 預訓練權重為 `tabfm-non-commercial-v1.0`，
**僅限非商業、非 production 使用**。正式商用需替換為具適當授權的自有 checkpoint。

---

## 10. 常數總表

### 訊號與健康度（`twin/topology.py`）

| 常數 | 值 | 用途 |
|---|---|---|
| `HEALTH_WEIGHTS` | vib .35 / temp .30 / cur .20 / rpm .15 | 健康度加權 |
| `HEALTH_FULL_SCALE` | 1.6 | 健康度「全壞」尺度 |
| `UNIT_MARGIN_NTD` | 185.0 | 單位產品邊際貢獻 |

### Monitoring（`agents/monitoring.py`）

| 常數 | 值 |
|---|---|
| `WINDOW` | 6 tick |
| 風險趨勢係數 | 1.6 |

### Diagnosis（`agents/diagnosis.py`）

| 常數 | 值 |
|---|---|
| `W_DIFFERENTIAL` | 0.44 |
| `W_MANUAL` | 0.38 |
| `W_PRIOR` | 0.10 |
| `W_DOCS` | 0.08 |
| `SOFTMAX_TEMPERATURE` | 0.16 |
| `STRENGTH_FULL` | 1.2 |
| `NO_FAULT_STRENGTH` | 0.45 |

### Orchestrator（`orchestrator.py`）

| 常數 | 值 |
|---|---|
| `min_confidence` | 0.65 |
| `max_confirm_ticks` | 10 |
| `max_attempts` | 2 |

### Optimizer（`optimizer.py`）

| 常數 | 值 |
|---|---|
| 權重 | safety .30 / delivery .22 / production .20 / recovery .12 / cost .08 / equipment .08 |
| `PRODUCTION_FULL_PCT` | 100.0 |
| `DELAY_WORST_MIN` | 240.0 |
| `RECOVERY_WORST_MIN` | 120.0 |
| `COST_WORST_NTD` | 300,000 |
| `NEVER_RECOVERED_PENALTY_MIN` | 60.0 |

### Verification（`agents/verification.py`）

| 常數 | 值 |
|---|---|
| `PRODUCTION_TOLERANCE_PCT` | 12.0 |
| `DELAY_TOLERANCE_MIN` | 15.0 |
| `HEALTH_TOLERANCE` | 8.0 |

### Prediction（`prediction/service.py`）

| 常數 | 值 |
|---|---|
| 特徵數 | 10 |
| `context_window` 預設 | 60（最少 8） |
| health 風險門檻 | 75.0 |
| 區間係數 | √index × 1.64 |
| `n_estimators` | 4 |
| `max_num_rows` | 100 |

---

## 附註：合成資料聲明

`knowledge/` 下的手冊、交機記錄、維修歷史與 `twin/` 的感測器規格
**全部為競賽用合成資料**，不代表任何真實設備商規格或真實驗收紀錄。
