# 感測器指紋診斷法：外部資料驗證

> **一句話結論**：把本專案的感測器指紋餘弦法原封不動搬到 UCI AI4I 2020 公開資料集上，
> 根因歸因 **Top-1 0.724 / Top-3 0.985**（隨機為 0.250 / 0.750），
> 顯著優於代表現行流程的單一訊號門檻規則（Top-1 0.458），
> 但**輸給**同樣特徵下的 LogisticRegression（0.964）與 RandomForest（0.939）。
> 方法在別人的資料上成立，但它不是最強的分類器 —— 兩件事都寫在這裡。

> **§8 的建議後來被採納了。** 診斷 Agent 現在支援「一個故障對多個指紋」，
> 同一份資料上 Top-1 從 0.724 升到 **0.821**、macro-F1 0.706 → **0.830**。
> §1–§10 記錄的是**單原型**版本（預設值，數字原地可重現），
> 採納後的重跑數字與程式改動記在 **§11**。
> 缺振動訊號那條缺口（§2.2）則由 [`docs/factory_guardian/cwru_validation.md`](cwru_validation.md) 補上。

重現指令：

```bash
python3 -m factory_guardian.validation --json runs/ai4i_validation.json                  # 單原型（§7）
python3 -m factory_guardian.validation --prototypes 2 --json runs/ai4i_validation_p2.json  # 多原型（§11.1）
```

程式在 [`factory_guardian/validation/`](../../factory_guardian/validation/)，
測試在 [`tests/test_validation.py`](../../tests/test_validation.py)（資料集不存在時自動 skip）。

---

## 1. 為什麼需要這份文件

本專案的診斷核心是**感測器指紋餘弦相似度**（[`agents/diagnosis.py`](../../factory_guardian/agents/diagnosis.py)，
權重 0.75，另加歷史先驗 0.15、文件支持 0.10）。問題在於：指紋的來源、被比對的訊號、
以及評估用的標籤，**全部來自本專案自己的 Digital Twin**。

提案書 §14 自己就把「資料是假的嗎」列為評審第一個預期追問。
對這個問題，唯一有效的回答不是解釋，是把同一套方法搬到一份**不是我們產生的**資料上再跑一次，
而且要和 baseline 比 —— 研究文件 §3.3 明列失分警訊：
「只報模型準確率，沒有和現行流程、人工判斷或簡單 baseline 比較。」

聲學偵測器那一側由 [`docs/factory_guardian/acoustic_validation.md`](acoustic_validation.md)（DCASE2020 pump，
真實工業錄音）負責；這份文件負責**感測器指紋診斷法**。

---

## 2. 誠實邊界（請先讀這一節）

這一節放在結果之前，不放附錄。以下每一條在程式碼裡都有對應的常數或測試。

### 2.1 AI4I 2020 是合成資料集，不是真實工廠量測

作者 Matzka (2020) 在論文與 UCI 頁面明講：
> "we present and provide a **synthetic** dataset that reflects real predictive maintenance encountered in industry."

五種故障模式全部由明文規則生成（見 §4 的表）。所以：

* ✅ 可以說：「這個方法在**外部、第三方、公開可查證**的資料上成立。」
* ❌ **不可以**說：「Factory Guardian 已在真實工廠資料上驗證。」

它的價值在於**資料的來源不是我們**：UCI 收錄、有 DOI、有論文，任何人都能下載同一份 CSV
重跑本模組並得到相同數字。我們無法為了讓方法好看而調整資料。這足以回答「數字是不是編的」，
但**不足以**回答「在真實產線的雜訊、感測器漂移與人為操作下是否同樣有效」。

程式上的標示：`validation/ai4i.py` 的 `DATASET_IS_SYNTHETIC = True` 與 `SYNTHETIC_NOTICE`，
由 `test_dataset_is_labelled_synthetic` 與 `test_markdown_carries_the_synthetic_notice` 守住 ——
報表本身會帶著這句話，有人只複製表格時它也跟著走。

### 2.2 這份資料沒有振動訊號

本專案診斷用四訊號（vibration / temperature / current / rpm），AI4I 只覆蓋得到其中三個。
影響有兩層，都必須講清楚：

1. **`bearing_degradation` 在 AI4I 中沒有對應模式**。而振動正是它指紋裡權重最大的分量
   （`FAULTS["bearing_degradation"].deltas` 中 vibration = 6.8，為最大項）。
   Demo 主線情境 `bearing-degradation` 的診斷能力，**這份驗證完全沒有覆蓋到**。
2. 因此本驗證支持的命題是「指紋餘弦法這個**機制**在外部資料上有鑑別力」，
   而不是「本專案的三個故障指紋都已驗證」。旋轉件類故障的外部驗證由聲學那條線承擔。

程式上的標示：`ai4i.MISSING_SIGNALS`，由 `test_project_signals_covered_and_vibration_declared_missing`
與 `test_mapping_table_reports_the_absent_signal` 守住。

### 2.3 指紋的來源在這裡和專案裡不一樣

專案的指紋來自**手冊**（`FAULTS[*].deltas`，工程師寫的領域知識，不需要任何標註故障歷史）。
AI4I 沒有手冊，所以本驗證的指紋是**從訓練切分的故障案例算出來的類別質心**。

這不是偷看答案（質心只用 train fold 算，test fold 完全沒參與，且交叉驗證下每筆樣本都是 out-of-fold），
但它確實讓方法多了一點「學習」成分。**後果是：專案主打的「零標註冷啟動」這條路徑，
在 AI4I 上根本量不到** —— 因為 AI4I 沒有手冊可以當作零標註的起點。
我們用學習曲線（§7.5）逼近了這個問題，結果並不支持我們原本的預期，也照實寫在下面。

### 2.4 我們核對過資料集文件與 CSV 的出入

UCI 頁面寫 TWF「120 instances」、RNF「5 instances」，但實際 CSV 的 `TWF` 欄是 46 筆、
`RNF` 欄是 19 筆（其中只有 1 筆同時被計入 `Machine failure`）。
本文件與程式一律以**實際 CSV 內容**為準，並把這些計數寫進測試
（`test_counts_match_the_published_dataset`、`test_label_derivation`），
以免日後有人「修正」成文件上的數字而不知道改壞了什麼。

---

## 3. 資料集

| 項目 | 內容 |
|---|---|
| 名稱 | AI4I 2020 Predictive Maintenance Dataset |
| 來源 | UCI Machine Learning Repository, dataset id 601 |
| 網址 | https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset |
| DOI | `10.24432/C5HS5C` |
| **授權** | **CC BY 4.0**（姓名標示 4.0 國際）— 已於 UCI 頁面確認。可商用、可改作，須標示出處 |
| 規模 | 10,000 筆 × 14 欄 |
| 標籤 | `Machine failure` 339 筆（3.39%）；TWF 46 / HDF 115 / PWF 95 / OSF 98 / RNF 19 |
| 特性 | 多模式紀錄 24 筆；RNF-only 18 筆；正常 9,652 筆 |

引用：

> S. Matzka, "Explainable Artificial Intelligence for Predictive Maintenance Applications,"
> *2020 Third International Conference on Artificial Intelligence for Industries (AI4I)*,
> 2020, pp. 69-74, doi:10.1109/AI4I49448.2020.00023.

授權比較：本專案另一份外部資料 DCASE2020/MIMII 是 **CC BY-NC-SA 4.0（非商業）**，
AI4I 則是 **CC BY 4.0（可商用）**。也就是說 §3 這條驗證線在正式商用部署時
**不需要更換資料集**，只需保留出處標示。

取得方式（`data/external/` 已在 `.gitignore`，不進版控）：

```bash
mkdir -p data/external/ai4i2020
# 自 UCI 下載 ai4i2020.csv 放入該目錄
```

---

## 4. 對應關係

### 4.1 訊號對應

對應規則寫在程式碼裡（`ai4i.CHANNEL_MAPPINGS`），本表由該結構產生，文件與程式不可能對不上。

| 本專案訊號 | AI4I 欄位 | 類型 | 理由 |
|---|---|---|---|
| `temperature` | `Process temperature [K]` | direct | 同一個物理量：製程溫度就是機台本體的工作溫度。 |
| `current` | `Torque [Nm]` | **proxy** | 扭矩是電流的力學代理。定速激磁馬達的軸端扭矩 `T = k_t · I`，扭矩與電樞電流成正比，因此「扭矩上升」在指紋空間中與「電流上升」同方向。本專案 `motor_overload` 的指紋是「current 上升 + rpm 下降」，在 AI4I 就是「torque 上升 + rotational speed 下降」。**這是代理不是同一個量**：它不含電氣面故障（缺相、絕緣劣化）。 |
| `rpm` | `Rotational speed [rpm]` | direct | 同一個物理量：主軸轉速。 |
| （專案無此訊號） | `Tool wear [min]` | analog | 刀具磨耗是**累積量**，概念上對應 Digital Twin 的 health / fault_progress 遞減（Monitoring Agent 看得到健康度估計，因此納入觀測向量並未偷看 ground truth）。沒有它，TWF 與 OSF 在物理上無法辨識。 |
| **`vibration`** | **（無）** | **absent** | **AI4I 不含振動量測。見 §2.2。** |

**推導通道（只做 ablation，不進主實驗）**：`thermal_margin` = 製程溫度 − 環境溫度、
`power` = 扭矩 × 角速度。這兩個量是標準工程指標，但它們**正好就是 AI4I 生成 HDF 與 PWF 的判準本身**。
用了它們數字會好看（Top-1 0.724 → 0.797），但那更接近「知道出題規則」而不是「方法有效」，
所以主實驗一律用嚴格對應的四個通道。這條分界由 `test_derived_channels_are_separated_from_strict` 守住。

### 4.2 故障模式對應

| AI4I | 本專案情境 | 可診斷 | AI4I 生成規則（論文載明） |
|---|---|---|---|
| **HDF** 散熱失效 | `cooling_failure` | ✅ | 製程溫度 − 環境溫度 < 8.6 K **且** 轉速 < 1380 rpm |
| **PWF** 功率失效 | `motor_overload` | ✅ | 機械功率 `T·ω` < 3500 W **或** > 9000 W |
| **OSF** 過應變 | 軸承／機構過載 | ✅ | 刀具磨耗 × 扭矩 > 11000 (L) / 12000 (M) / 13000 (H) min·Nm |
| **TWF** 刀具磨耗 | （專案無直接對應） | ✅ | 刀具磨耗達 200–240 min 間的隨機值 |
| **RNF** 隨機故障 | 不可診斷案例測試素材 | ❌ | 每筆製程 0.1% 機率，**與任何製程參數無關** |
| — | `bearing_degradation` | — | **AI4I 無對應模式**（缺振動訊號） |

RNF 依定義不可能有指紋，因此**不進訓練**，也不算進任何一個任務，單獨診斷（§7.4）。
`test_rnf_is_declared_undiagnosable` 守住這件事 —— 把 RNF 標成可診斷等於承諾一件做不到的事。

注意 PWF 是**雙側**條件（功率過高或過低都算）。這對「每個故障一個原型」的指紋法是結構性難題，
結果見 §8.2。

---

## 5. 方法

`validation/fingerprint.py` 是 `agents/diagnosis.py` 的**獨立等價實作**，不 import 對方。
理由：診斷 Agent 綁在 `FactorySnapshot` / `AnomalyEvent` / `KnowledgeBase` 上，外部資料集沒有這些東西；
而且它正在持續演進，外部驗證若跟著它漂，「這個數字在驗證什麼版本的方法」就說不清楚。

| 本模組 | `agents/diagnosis.py` |
|---|---|
| `Normalizer.deviation()` | `DiagnosisAgent._deviation_vector()` + `SignalSpec.nominal/scale` |
| `FingerprintModel.fit()` 的類別質心 | `twin/faults.py::fault_signatures()`（profile = deltas / scale） |
| `cosine()` | `DiagnosisAgent._cosine()`（逐字等價） |
| `combined` 三項加權 | `W_SIGNATURE 0.75` / `W_PRIOR 0.15` / `W_DOCS 0.10` |
| `_confidences()` | `DiagnosisAgent._confidences()`（softmax T=0.16 + 訊號強度衰減） |
| `_no_fault_candidate()` | `DiagnosisAgent._no_fault_candidate()` |

三項如何在 AI4I 上落地：

* **指紋餘弦（0.75）** —— 觀測偏離向量 `(value − nominal) / scale`，其中 nominal/scale
  取自**訓練切分的正常樣本**（nominal 的定義就是「這台機器沒事時長什麼樣」；
  把故障樣本混進去會把基準線往故障方向拉，偏離量就被稀釋了）。
  指紋 = 該模式訓練樣本偏離向量的質心，只用**單一模式**樣本歸納
  （多模式樣本會同時污染兩個質心，讓兩個指紋互相靠攏）。
* **歷史先驗（0.15）** —— 專案是「這台機器過去得過什麼病」；AI4I 沒有機台 id，
  最接近的分群是產品等級 L/M/H（OSF 的門檻本來就依等級不同），
  故用 `P(mode | product_type)`（Laplace 平滑）。
* **文件支持（0.10）** —— AI4I **沒有**手冊 / SOP / 維修紀錄語料，此項對所有候選一律為 0。
  保留這一項而不是把權重併掉，是為了讓算式與 `diagnosis.py` 逐項對得上：
  常數項對所有候選相同，**不影響排名**，只讓 `combined` 的絕對值低 0.10。

**門檻校準**：專案用固定常數 `NO_FAULT_STRENGTH = 0.45`（因為它的 scale 是手冊訂的物理量）；
AI4I 的通道是 z 分數，固定常數沒有意義，改成「訓練正常樣本強度的 0.99 分位」
（＝容許 1% 誤報）。`STRENGTH_FULL` 則取訓練故障樣本強度的中位數。兩者都只用 train fold 估。

---

## 6. 評估設計

**切分**：**5-fold 分層交叉驗證**。每筆樣本恰好被預測一次，且預測它的模型從未看過它。
指紋質心、先驗、正規化基準、門檻、分類器**全部**只在 train fold 上估計。
不用單次 hold-out 的理由是樣本數：TWF 只有 46 筆，30% 測試集只剩 14 筆，
per-class recall 的不確定性會寬到不能引用。

**類別不平衡**（故障 3.39%，TWF 僅 0.46%）的處理：

* 分層切分，確保每個 fold 都有各模式樣本。
* 主指標用 **macro-F1**（每個模式等權），accuracy 只列出來提醒它會騙人 —— 全猜正常就有 96.6%。
* LR / RF 一律 `class_weight="balanced"`；不設的話兩者都會學成「全部猜正常」，比較就變成表演。
* **不做過採樣／SMOTE** —— 那會讓 test fold 混進合成樣本的近鄰，數字會虛高。
* per-class 數字一律連同 support 報出，support < 30 者標 ⚠︎。

**兩個任務**：

1. **根因歸因（主）** —— 只看故障樣本，候選集 = 4 個可診斷模式，**不含** `no_equipment_fault`。
   這對應本專案的實際流程：Monitoring Agent 先偵測、Diagnosis Agent 才啟動，
   被呼叫時「有異常」已是前提。為求公平，**所有方法**都同樣移除 no-fault 候選。
2. **端到端（次）** —— 正常樣本一起放進去，候選集多一個 `no_equipment_fault`，量的是偵測＋歸因。

多模式紀錄（24 筆）的處理：Top-k 採「集合命中」（答對其中任一模式即算命中，
現場維修上這確實是有效診斷）；混淆矩陣則只用單一模式樣本，避免真實標籤無定義。

**對照組**：門檻規則的門檻是**在訓練切分上以 F1 最佳化選出來的**，不是拍腦袋的數字 ——
故意讓現行流程這一組盡量強。LR / RF 吃到的特徵與指紋法**完全相同**
（同一組正規化偏離向量 + 產品等級 one-hot），比的是方法不是特徵工程。

---

## 7. 實測結果

以下數字由 `python -m factory_guardian.validation` 產生（5 fold、seed 20260809、scikit-learn 1.7.2）。

### 7.1 任務一：根因歸因（僅故障樣本，330 筆 = 307 單模式 + 23 多模式）

Top-k 以全部 330 筆計；每模式 precision / recall 與混淆矩陣以 307 筆單模式樣本計
（多模式樣本的「真實類別」無定義，硬指定一個會讓矩陣說謊）。

| 方法 | Top-1 | Top-3 | macro-P | macro-R | macro-F1 | fold macro-F1 (mean±sd) |
|---|---:|---:|---:|---:|---:|---:|
| **指紋餘弦（本專案方法）** | **0.724** | **0.985** | 0.809 | 0.702 | **0.706** | 0.705±0.046 |
| 單一訊號門檻規則（現行流程） | 0.458 | 0.852 | 0.545 | 0.512 | 0.463 | 0.406±0.043 |
| LogisticRegression | 0.964 | 1.000 | 0.956 | 0.963 | 0.959 | 0.959±0.017 |
| RandomForest | 0.939 | 0.997 | 0.930 | 0.929 | 0.929 | 0.930±0.017 |
| 多數決（下限） | 0.139 | 0.764 | 0.035 | 0.250 | 0.061 | 0.061±0.004 |
| 分層隨機（下限） | 0.245 | 0.764 | 0.225 | 0.225 | 0.216 | 0.214±0.042 |
| *隨機期望值（4 類）* | *0.250* | *0.750* | — | — | — | — |

### 7.2 指紋餘弦法：每模式 precision / recall

| 模式 | support | precision | recall | F1 |
|---|---:|---:|---:|---:|
| TWF 刀具磨耗 | 43 | 1.000 | 0.698 | 0.822 |
| HDF 散熱失效 | 106 | 0.645 | 0.736 | 0.687 |
| PWF 功率失效 | 80 | 0.969 | **0.388** | 0.554 |
| OSF 過應變 | 78 | 0.621 | 0.987 | 0.762 |

### 7.3 指紋餘弦法：混淆矩陣（out-of-fold pooled）

| 真實＼預測 | TWF | HDF | PWF | OSF | 合計 |
|---|---:|---:|---:|---:|---:|
| TWF | **30** | 0 | 1 | 12 | 43 |
| HDF | 0 | **78** | 0 | 28 | 106 |
| PWF | 0 | **42** | 31 | 7 | 80 |
| OSF | 0 | 1 | 0 | **77** | 78 |

最大的單一錯誤來源一目了然：**80 筆 PWF 中有 42 筆被判成 HDF**。原因見 §8.2。

### 7.4 任務二：端到端（含 9,652 筆正常樣本）

| 方法 | macro-P | macro-R | macro-F1 | accuracy |
|---|---:|---:|---:|---:|
| 指紋餘弦 | 0.276 | 0.587 | **0.308** | 0.798 |
| 單一訊號門檻規則 | 0.404 | 0.347 | 0.354 | 0.947 |
| LogisticRegression | 0.393 | 0.914 | 0.453 | 0.777 |
| RandomForest | 0.640 | 0.447 | **0.497** | 0.978 |
| 多數決 | 0.194 | 0.200 | 0.197 | 0.969 |

**指紋餘弦法在這個任務上是所有方法裡最差的（macro-F1 0.308）。** 照實寫出來。
原因是 no-fault gate 的誤報：

| no-fault gate | 數值 |
|---|---:|
| 正常樣本中訊號強度超過校準門檻者 | 1.0%（＝名目設計值） |
| 正常樣本中**最終 Top-1 落在故障上**者 | **19.3%** |
| 故障樣本中訊號強度超過門檻者 | 10.0% |

名目 1% 與實際 19.3% 的落差來自方法本身的規則（與 `diagnosis.py` 一致）：
no-fault 候選要**信心贏過最佳故障候選**才會被插到最前面，而故障候選的 softmax 信心至少有 1/K。
這是方法的真實性質，不是 bug，但不寫出來就沒有人看得出誤報從哪裡來。

**這說明的是架構問題，不是方法失敗**：在本專案中偵測是 Monitoring Agent 的工作
（threshold + trend + health 三重觸發），Diagnosis Agent 只做歸因。
這張表量的是把診斷器單獨當偵測器用會發生什麼事 —— 答案是不該這樣用。
我們仍然把它列出來，因為只報任務一而藏起任務二，正是這份文件想避免的選擇性報告。

### 7.5 RNF：不可診斷案例

| 指標 | RNF-only（18 筆） | 正常樣本（9,652 筆） |
|---|---:|---:|
| Top-1 給出 `no_equipment_fault` 的比例 | 66.7% | 80.7% |
| 給故障候選的最高信心平均 | 0.451 | 0.415 |

**誠實解讀：指紋法對 RNF 並沒有特別的「拒答能力」。** 它只是把 RNF 當成一般正常樣本處理
（這在物理上是對的 —— RNF 依定義沒有徵兆），而且拒答率甚至比正常樣本略低。
所以**不能宣稱**本系統能辨識「不可診斷案例」。
Orchestrator 的 `confirm_diagnosis` 是靠信心門檻延後決策，不是靠識別隨機故障 ——
這兩件事不一樣，提案中不應混為一談。（18 筆的樣本量也太小，這兩個數字只能當方向參考。）

### 7.6 Ablation

| 變體 | Top-1 | Top-3 | macro-F1 | 對照主實驗 |
|---|---:|---:|---:|---|
| 主實驗（嚴格四通道、單一原型、含先驗） | 0.724 | 0.985 | 0.706 | — |
| 移除歷史先驗（權重 0.15 → 0） | 0.715 | 0.888 | 0.697 | Top-1 −0.9pt |
| **每模式 2 個原型** | **0.821** | 1.000 | **0.830** | **Top-1 +9.7pt** |
| 加入推導通道（ΔT、功率） | 0.797 | 1.000 | 0.744 | Top-1 +7.3pt（但接近出題規則，不採計） |

先驗只貢獻 0.9 個百分點的 Top-1，符合「指紋主導排名」的設計意圖（關鍵設計決定 5）。
它對 Top-3 影響較大（0.985 → 0.888），也就是說先驗主要在整理**尾部**排序。

### 7.7 學習曲線：每模式標註筆數 vs 歸因 Top-1

| 每模式可用標註筆數 | 指紋餘弦 | LogisticRegression | RandomForest |
|---|---:|---:|---:|
| 1 | 0.497 | **0.612** | 0.552 |
| 2 | 0.621 | **0.842** | 0.667 |
| 3 | 0.636 | **0.873** | 0.697 |
| 5 | 0.661 | **0.903** | 0.755 |
| 10 | 0.718 | **0.945** | 0.815 |
| 20 | 0.703 | **0.948** | 0.870 |
| 40 | 0.721 | **0.958** | 0.915 |
| 全部 | 0.724 | **0.964** | 0.939 |

**這是一個否定我們原本假設的結果。** 我們預期指紋法的優勢在冷啟動（標註極少時），
但在 AI4I 上 LogisticRegression **在每模式只有 1 筆標註時就已經領先**，且領先幅度隨標註增加而擴大。
指紋法的「少量標註就夠用」在這份資料上**沒有得到支持**。

唯一沒被這條曲線涵蓋的是**真正的零標註**（指紋直接來自手冊，一筆故障歷史都沒有）——
那是專案在真實場域的實際起點，但 AI4I 沒有手冊，所以量不到。這一點只能誠實留白。

---

## 8. 從結果學到的三件事

### 8.1 「多訊號組合」本身確實有價值，而且量得出來

指紋餘弦法 Top-1 0.724 vs 單一訊號門檻規則 0.458，macro-F1 0.706 vs 0.463。
兩者的**唯一**差別是：門檻規則一次只看一個維度，指紋法看的是整個向量的方向。
而且門檻是在訓練集上以 F1 最佳化選出來的，不是拍腦袋的數字。
所以 +26.6 個百分點可以直接歸因於「把多個訊號當成一個向量來比對」這個設計 ——
這正是提案中主張「不要只做門檻告警」的實證支撐，而且是在外部資料上量到的。

### 8.2 單一原型無法表示雙側故障 —— 這是可以直接修的結構性弱點

PWF recall 只有 0.388，80 筆中 42 筆被誤判為 HDF。原因是 PWF 的生成規則是**雙側**的
（功率 < 3500 W **或** > 9000 W），在偏離向量空間裡是**兩個相反方向的簇**。
單一質心會落在兩簇中間，方向失去意義，餘弦自然指不到正確答案。

把每個模式的原型數改成 2（k-means 分群後取最大餘弦）之後：

| | 單一原型 | 2 個原型 |
|---|---:|---:|
| PWF recall | 0.388 | **0.788** |
| PWF F1 | 0.554 | **0.813** |
| 整體 Top-1 | 0.724 | **0.821** |
| 整體 macro-F1 | 0.706 | **0.830** |

**這是給 `agents/diagnosis.py` 的具體改進建議**：`FaultSignature.profile` 目前是單一向量，
若某個故障在不同工況下有相反的訊號方向（例如馬達過載 vs 失載、冷卻過度 vs 不足），
就需要一個故障對多個指紋、取最大餘弦。

> **✅ 已採納（見 §11.1）。** `FaultSignature` 現在支援 `alt_prototypes`，
> `twin/faults.py` 為 `motor_overload`（失載）與 `cooling_failure`（冷卻過度）
> 各補了一個手冊來源的第二原型。本模組也可以用 `--prototypes 2` 重跑主實驗。

### 8.3 有標註歷史時，判別式分類器該被加進來 —— 但那不會取代指紋法

LR 0.964 / RF 0.939 明顯優於指紋法 0.724，且學習曲線顯示這個差距在極少標註時就存在。
誠實的結論是：**在已經累積標註故障歷史的產線上，應該加一層判別式模型**。

但這不會讓指紋法失去角色，理由是**兩者需要的輸入不同**：

* 判別式模型需要**每個故障模式的標註樣本**。新產線、新設備、罕見故障，樣本就是不存在。
* 指紋法只需要**手冊上的徵兆描述**（`FAULTS[*].deltas`），這在設備進廠第一天就有。

也就是說，正確的定位不是「指紋法 vs 分類器」，而是**指紋法負責冷啟動與可解釋性、
分類器在資料累積後接手排序**，並保留指紋餘弦作為可稽核的解釋依據
（Dashboard 上把 88% 拆成算式的那一格，判別式模型給不出等價的東西）。
這一點應該寫進提案的技術路線圖，而不是宣稱指紋法是最強的方法 —— 它不是。

> **✅ 程式路徑已建好，但預設關閉（見 §11.2）。** `agents/diagnosis.py` 新增
> `DiscriminativeReranker`，在「同機台帶讀值的標註案例 ≥ 8 筆」時以固定權重參與合分。
> **預設權重 0.0、且 `DiagnosisAgent` 預設不掛載這一層**，理由見 §11.2 ——
> Demo 的維修歷史是合成的，用它去推動排名等於用自己編的資料證明自己。

---

## 8.4 `bearing_degradation` 的缺口已由另一份驗證補上

§2.2 寫過：AI4I 沒有振動訊號，Demo 主線情境 `bearing-degradation` 的診斷能力
「這份驗證完全沒有覆蓋到」。這個缺口現在由 **CWRU 軸承振動資料集**（真實加速規量測）
補上，見 [`docs/factory_guardian/cwru_validation.md`](cwru_validation.md)。

一句話摘要（細節與誠實邊界請讀那份文件，不要只引用這三行）：

* 內圈／滾珠／外圈三類歸因，leave-one-load-out Top-1 **0.994**；
* 但**同尺寸內的數字幾乎飽和，證明不了什麼** —— 單一特徵規則就有 0.925，
  RMS 單一門檻的偵測 AUC 是 **1.000**（贏過指紋法的 0.982）；
* 真正有資訊量的是**跨嚴重度轉移**：拿 0.021″（壞得明顯）訓練、測 0.007″（剛開始壞），
  指紋 Top-1 掉到 **0.335 ＝ 隨機**。這條結果不支持「早期偵測」的宣稱。

---

## 9. 明確不做的宣稱

* ❌ 不宣稱本專案已在**真實工廠資料**上驗證診斷能力（AI4I 是合成資料集）。
* ❌ 不宣稱指紋法優於機器學習分類器（實測輸給 LR 與 RF，見 §7.1）。
* ❌ 不宣稱已驗證 `bearing_degradation` 的辨識能力（缺振動訊號，AI4I 無對應模式）。
  **這份文件**仍然不覆蓋它；覆蓋它的是 [`docs/factory_guardian/cwru_validation.md`](cwru_validation.md)，
  而那份文件同樣明確不宣稱早期偵測能力（跨嚴重度轉移 Top-1 = 隨機）。
* ❌ 不宣稱系統能辨識「不可診斷案例」（RNF 拒答率並未優於一般正常樣本，見 §7.5）。
* ❌ 不宣稱指紋法可單獨作為異常偵測器（端到端 macro-F1 0.308，見 §7.4）。
* ✅ **宣稱**：指紋餘弦這個機制在一份外部、第三方、公開可查證、非本專案產生的資料上，
  多類別根因歸因顯著優於隨機（Top-1 0.724 vs 0.250）與代表現行流程的單一訊號門檻規則（0.458）。

---

## 10. 重現與版本

```bash
# 完整驗證（約 1 分鐘，含 ablation 與學習曲線）
python -m factory_guardian.validation --json runs/ai4i_validation.json --markdown runs/ai4i_validation.md

# 只跑主實驗（較快）
python -m factory_guardian.validation --no-ablations --no-learning-curve

# 測試（資料集不存在時自動 skip，不會弄壞 CI）
python -m pytest tests/test_validation.py -q
```

* 隨機種子 `20260809`（與專案 `FG_SEED` 一致），所有結果確定性可重現。
* 核心路徑（載入、指紋、指標、門檻規則）**只用 Python 標準庫**，不依賴 numpy / pandas。
  scikit-learn 為**選配**：缺席時 LR / RF 對照組不執行，並在報告的「執行備註」中明講「未執行」，
  不會靜默跳過（由 `test_sklearn_absence_is_reported_not_hidden` 守住）。
* 本模組**不被** `twin/`、`agents/`、`api/`、`cli.py` 任何一處匯入 ——
  沒有任何 Demo 數字來自這裡。

**被驗證的方法版本**：`W_SIGNATURE = 0.75`、`W_PRIOR = 0.15`、`W_DOCS = 0.10`、
softmax 溫度 `0.16`、四訊號偏離向量餘弦。
原型數由 `--prototypes` 決定：**預設 1**（§7 的所有數字），`2` 對應 §11.1 採納後的設計。
`agents/diagnosis.py` 日後若調整融合權重或新增模態（例如聲學），
本模組的常數**不會自動跟上**（刻意不 import，見 `validation/fingerprint.py` 模組說明）。
要驗證新版本時，更新 `validation/fingerprint.py` 的常數並重跑上面的指令，同時更新本節。

判別式接手層（§11.2）的權重 `W_RERANK` **不在**本模組的驗證範圍內：
它預設為 0、不參與任何排名，因此上面所有數字都是「純指紋法」的數字。

---

## 11. §8 的建議被採納之後（重跑數字）

§8 列的是「從結果學到的三件事」。這一節記錄其中兩件**已經改進到程式裡**之後的實際數字，
以及一件刻意**沒有**打開的改進與理由。改動落在
[`agents/diagnosis.py`](../../factory_guardian/agents/diagnosis.py)、
[`twin/faults.py`](../../factory_guardian/twin/faults.py)、
[`domain.py`](../../factory_guardian/domain.py) 的 `FaultSignature`。

### 11.1 多原型指紋：診斷 Agent 已採納

`FaultSignature` 現在有 `alt_prototypes`，比對時對所有原型**取最大餘弦**
（平手取索引小的，也就是優先採信手冊主徵兆）。
`twin/faults.py` 為兩個故障各補了一個第二原型，來源同樣是**手冊語意**、不是從標籤學來的：

| 故障 | 第二原型 | 方向 | 手冊理由（摘要，完整版在程式碼裡） |
|---|---|---|---|
| `motor_overload` | `under_load` | 電流 −4.0 A、轉速 +6.0%、溫度 −4.0°C、振動 +1.2 mm/s | MAN-A-5.3 把主軸動力異常拆成過載與**失載**兩側：皮帶斷裂、聯軸器鬆脫、刀具脫落時馬達失去負載，電流大幅下降、轉速衝過 100%、振動因失去阻尼而略升。 |
| `cooling_failure` | `overcooling` | 溫度 −12.0°C、電流 +0.8 A、振動 +0.4 mm/s、轉速 −0.8% | MAN-A-4.1 定義的是「冷卻迴路**失去調節能力**」，不只有冷卻不足：調節閥卡在全開時機台被過度冷卻，熱變形量偏離設計點、切削阻力上升。 |

`bearing_degradation` 沒有第二原型（手冊上它只有一個方向），因此它的行為**逐位元不變** ——
由 `tests/test_agents.py::TestMultiPrototypeFingerprint::test_single_prototype_faults_are_unchanged` 守住。

#### AI4I 上的重跑數字：單原型 vs 多原型

```bash
python3 -m factory_guardian.validation --json runs/ai4i_validation.json      # 單原型（§7 的數字）
python3 -m factory_guardian.validation --prototypes 2 --json runs/ai4i_validation_p2.json
```

**任務一：根因歸因**

| 方法 | Top-1 | Top-3 | macro-P | macro-R | macro-F1 | fold macro-F1 (mean±sd) |
|---|---:|---:|---:|---:|---:|---:|
| 指紋餘弦（**單原型**，§7 主實驗） | 0.724 | 0.985 | 0.809 | 0.702 | 0.706 | 0.705±0.046 |
| 指紋餘弦（**2 原型**） | **0.821** | **1.000** | 0.848 | 0.820 | **0.830** | 0.829±0.051 |
| 差值 | **+9.7pt** | +1.5pt | +3.9pt | **+11.8pt** | **+12.4pt** | — |
| *（不變）單一訊號門檻規則* | *0.458* | *0.852* | *0.545* | *0.512* | *0.463* | *0.406±0.043* |
| *（不變）LogisticRegression* | *0.964* | *1.000* | *0.956* | *0.963* | *0.959* | *0.959±0.017* |

**每模式 precision / recall**（多原型救回來的正是 §8.2 指出的 PWF）：

| 模式 | support | P（1 原型） | R（1 原型） | F1（1 原型） | P（2 原型） | R（2 原型） | F1（2 原型） |
|---|---:|---:|---:|---:|---:|---:|---:|
| TWF 刀具磨耗 | 43 | 1.000 | 0.698 | 0.822 | 1.000 | **0.814** | **0.897** |
| HDF 散熱失效 | 106 | 0.645 | 0.736 | 0.687 | **0.800** | 0.792 | **0.796** |
| PWF 功率失效（雙側） | 80 | 0.969 | **0.388** | 0.554 | 0.840 | **0.787** | **0.813** |
| OSF 過應變 | 78 | 0.621 | 0.987 | 0.762 | **0.750** | 0.885 | **0.812** |

**混淆矩陣（2 原型，out-of-fold pooled）**：§7.3 那個「80 筆 PWF 有 42 筆被判成 HDF」的
單一最大錯誤來源消失了（42 → 12）。

| 真實＼預測 | TWF | HDF | PWF | OSF | 合計 |
|---|---:|---:|---:|---:|---:|
| TWF | **35** | 0 | 1 | 7 | 43 |
| HDF | 0 | **84** | 11 | 11 | 106 |
| PWF | 0 | 12 | **63** | 5 | 80 |
| OSF | 0 | 9 | 0 | **69** | 78 |

**學習曲線也整條抬升**（每模式標註筆數 vs 歸因 Top-1）：

| 每模式標註筆數 | 指紋（1 原型） | 指紋（2 原型） | LogisticRegression |
|---|---:|---:|---:|
| 1 | 0.497 | 0.497 | **0.612** |
| 2 | 0.621 | 0.621 | **0.842** |
| 3 | 0.636 | 0.636 | **0.873** |
| 5 | 0.661 | 0.706 | **0.903** |
| 10 | 0.718 | 0.748 | **0.945** |
| 20 | 0.703 | 0.797 | **0.948** |
| 40 | 0.721 | 0.809 | **0.958** |
| 全部 | 0.724 | 0.821 | **0.964** |

**兩個必須一起講的觀察，否則這張表會被過度解讀：**

1. 標註 ≤ 3 筆時多原型**一點忙都沒幫上**（0.497 / 0.621 / 0.636，與單原型完全相同）。
   原因寫在 `fingerprint.py` 裡：`len(vectors) < 2 × n_prototypes` 時自動退回單一質心 ——
   4 筆樣本分兩群，每群 2 筆，那不是分群是過擬合。
   **也就是說「多原型」與「冷啟動」是兩件互斥的事**：它要有足夠樣本才分得出第二個方向。
   在真實部署上，Agent 的第二原型來自**手冊**（不需要樣本），這個限制不存在 ——
   但那件事 AI4I 量不到，只能在此留白（與 §2.3 同一個結構性限制）。
2. **多原型沒有翻轉 §8.3 的結論。** LogisticRegression 全程仍然領先（0.964 vs 0.821）。
   指紋法變強了，但它仍然不是最強的分類器。

**端到端任務（次要）也一併重跑**，結論不變 —— 指紋法單獨當偵測器仍然是最差的：

| 方法 | macro-P | macro-R | macro-F1 | accuracy |
|---|---:|---:|---:|---:|
| 指紋餘弦（1 原型） | 0.276 | 0.587 | 0.308 | 0.798 |
| 指紋餘弦（2 原型） | 0.281 | 0.502 | **0.305** | 0.836 |
| RandomForest | 0.640 | 0.447 | **0.497** | 0.978 |

多原型讓 no-fault gate 的實際誤報從 **19.3% 降到 15.0%**（名目設計值都是 1.0%），
但 macro-F1 幾乎沒動（0.308 → 0.305）。**這是誠實的壞消息**：
多原型解決的是「歸因時方向指錯」，不是「不該啟動時被啟動」——
後者是 §7.4 講的架構問題，不會因為指紋變準而消失。

### 11.2 判別式接手層：路徑建好了，但預設關閉

`agents/diagnosis.py` 新增 `DiscriminativeReranker`：

```
combined = 0.75 × cos_fused + 0.15 × prior + 0.10 × docs + w_rerank × P(fault | 讀值)
```

* 訓練資料：`knowledge/corpus.py::MAINTENANCE_HISTORY` 中帶 `readings` 的案例
  （新增欄位，**合成**，理由與限制寫在 `MaintenanceCase.readings` 的註解裡）。
* 特徵：與指紋法**完全相同**的正規化偏離向量 —— 比的是方法，不是特徵工程。
* 啟用條件：**同機台**帶讀值的標註案例 ≥ `MIN_LABELLED_CASES = 8`
  （M-A 19 筆、M-B 8 筆會啟用；M-C 只有 3 筆，維持停用 ＝ 冷啟動狀態）。
* 只用同一台機台的案例訓練：不同機台的 nominal/scale 與工況不同，
  拿 M-B 的歷史去推翻 M-A 的觀測是錯的。

**為什麼預設權重是 0、且 `DiagnosisAgent` 預設不掛載這一層：**

§8.3 的結論是「**有標註歷史時**應該加一層判別式模型」。Demo 的維修歷史是我們自己寫的合成語料，
拿它去推動排名，就是用自己編的資料證明自己 —— 那正是這整份文件想避免的事。
所以程式路徑建好、可稽核、可一行打開，權重留給有真實標註歷史的場域再調。
`tests/test_agents.py::TestDiscriminativeReranker::test_default_is_bit_identical`
守住「預設狀態下逐位元不變」。

**降級不可靜默。** 缺 scikit-learn、同機台案例不足、標註只涵蓋單一故障 ——
三種情況都會在 `Diagnosis.reranker` 與稽核 log 留下 `enabled=False` 與 `reason`，
而不是安靜地不作用。

**對 Dashboard 的影響**：`Diagnosis.weights` 多了一個 `rerank` 鍵（停用時 0.0），
`RootCauseCandidate.scores` 多了 `rerank`（機率）與 `prototype`（命中的原型索引）。
「合計 = 各項相加」在四項下仍然成立，由
`tests/test_agents.py::TestDiscriminativeReranker::test_weights_still_add_up` 守住。
⚠️ **未完成**：`api/static/index.html` 的推理面板目前只渲染三項；
啟用這一層時需要多渲染一列（見本文件末的整合備註）。

### 11.3 稽核輸出的新欄位

| 位置 | 欄位 | 型別 | 說明 |
|---|---|---|---|
| `RootCauseCandidate.scores` | `prototype` | float | 命中的原型**索引**（0 = 手冊主徵兆）。 |
| `RootCauseCandidate.scores` | `prototype_count` | float | 這個故障總共有幾個原型。 |
| `RootCauseCandidate.scores` | `rerank` | float | 判別式模型給這個候選的機率（停用時 0.0）。 |
| `Diagnosis.weights` | `rerank` | float | 判別式模型的權重（停用時 0.0）。 |
| `Diagnosis.reranker` | dict | — | `enabled` / `reason` / `cases` / `sklearn` / `probabilities`。 |
| 稽核 log `scores.<fault>` | `prototype` | str | 原型**名稱**（`primary` / `under_load` / `overcooling`）。 |

`scores` 的值一律是數字，`prototype` 因此存索引而不是名稱 ——
`stage/director.py` 會用 `f"{v:.3f}"` 逐項格式化整個 `scores`，塞字串進去會讓 Demo 導播稿炸掉。
名稱走稽核 log 與候選的 Evidence 文字（命中替代原型時，Evidence 會直接說明是哪一個變異型、
以及手冊上的理由）。這條由
`tests/test_agents.py::TestMultiPrototypeFingerprint::test_scores_stay_numeric_and_serialisable` 守住。

---

## 12. 整合備註（需要動到本文件範圍外的檔案）

以下兩件事**尚未做**，因為它們落在別的模組，改動需要與 Dashboard／導播稿一起驗證：

1. **`api/static/index.html` 的推理面板**：`reasonDiagnose()` 目前把「合計」拆成
   指紋 / 先驗 / 文件三列。判別式接手層啟用時（`weights.rerank > 0`）需要多一列
   `判別式模型 P(fault) × w.rerank`，否則畫面上的「合計」會對不起來。
   停用時（預設）`weights.rerank = 0`，畫面完全不受影響。
2. **原型名稱的呈現**：`scores.prototype` 是索引（0 = 主原型）。
   要在畫面上顯示「命中的是失載變異型」時，名稱在稽核 log 與該候選的 Evidence 文字裡，
   前端可以直接用 Evidence，不需要新的 API 欄位。
