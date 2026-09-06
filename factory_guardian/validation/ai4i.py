"""UCI **AI4I 2020 Predictive Maintenance Dataset** 轉接器與訊號對應層。

## 這份資料集在專案裡的角色

Factory Guardian 的診斷核心是「感測器指紋餘弦相似度」（`agents/diagnosis.py`，權重 0.75）。
但指紋的來源、被比對的訊號、以及評估用的標籤，全部都來自本專案自己的 Digital Twin。
提案書 §14 自己就把「資料是假的嗎」列為評審第一個預期追問。

這個模組的目的只有一件事：**把同一套指紋餘弦方法，原封不動搬到一份不是我們產生的、
公開可查證的第三方資料集上，看它還成不成立**，並且和 baseline 比較。
它不參與 Demo 閉環，也沒有任何一個 Demo 上的數字來自這裡；程式上的保證是
本套件不被 `twin/`、`agents/`、`api/`、`cli.py` 任何一處匯入。

## 誠實邊界（最重要的一節，請不要在轉述時省略）

**AI4I 2020 是「合成」資料集，不是真實工廠量測。** 作者 Matzka (2020) 在論文與 UCI 頁面
明講它是依既有銑床（milling machine）的物理關係模擬產生的公開 benchmark，
五種故障模式由明文規則生成（見 `FAULT_MODES` 各項的 ``generating_rule``）。
所以本模組**不得**被引用為「Factory Guardian 已在真實工廠資料上驗證」。

它的價值在另一個維度，而那個維度正好就是評審在問的：

* **外部**：不是本專案作者產生的。
* **第三方**：UCI ML Repository 收錄，有論文、有 DOI。
* **公開可查證**：任何人都能下載同一份 CSV 重跑本模組並得到相同數字。
* **標籤與特徵都不是我們定的**：我們無法為了讓方法好看而調整資料。

也就是說，它能回答「數字是不是編的」，但**不能**回答「在真實產線的雜訊下是否同樣有效」。
後者要靠 `acoustics/`（DCASE2020 pump，真實工業錄音）與未來的 PoC 場域資料。

還有一個必須講清楚的限制：**本資料集沒有振動訊號**。本專案診斷用四訊號
（vibration / temperature / current / rpm），這裡只覆蓋得到其中三個，
而且 `bearing_degradation` 這個以振動主導的故障在 AI4I 中**沒有對應模式**。
詳見 `MISSING_SIGNALS` 與 `docs/factory_guardian/external_validation.md`。

## 資料集

* 名稱：AI4I 2020 Predictive Maintenance Dataset
* 來源：UCI Machine Learning Repository, dataset id 601
* 授權：**CC BY 4.0**（姓名標示 4.0 國際）——UCI 全站資料集預設授權，可商用，須標示出處。
* 規模：10,000 筆 × 14 欄；`Machine failure` 339 筆（3.39%）。
* 引用：S. Matzka, "Explainable Artificial Intelligence for Predictive Maintenance
  Applications," 2020 Third International Conference on Artificial Intelligence for
  Industries (AI4I), 2020, pp. 69-74, doi:10.1109/AI4I49448.2020.00023.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

# 專案根目錄下的既定位置（`data/external/` 已在 .gitignore，不進版控）。
DATASET_DIR = Path(__file__).resolve().parents[2] / "data" / "external" / "ai4i2020"
DATASET_CSV = DATASET_DIR / "ai4i2020.csv"

DATASET_NAME = "AI4I 2020 Predictive Maintenance Dataset"
DATASET_SOURCE = "UCI Machine Learning Repository (id=601)"
DATASET_URL = "https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset"
DATASET_LICENSE = "CC BY 4.0（姓名標示 4.0 國際；可商用，須標示出處）"
DATASET_DOI = "10.24432/C5HS5C"
DATASET_CITATION = (
    'S. Matzka, "Explainable Artificial Intelligence for Predictive Maintenance Applications," '
    "2020 Third International Conference on Artificial Intelligence for Industries (AI4I), "
    "2020, pp. 69-74, doi:10.1109/AI4I49448.2020.00023."
)

#: 這是**合成**資料集。任何引用本模組數字的地方都必須同時出現這句話。
DATASET_IS_SYNTHETIC = True
SYNTHETIC_NOTICE = (
    "AI4I 2020 為作者依銑床物理關係模擬產生的公開 benchmark，非真實工廠量測；"
    "本驗證證明的是「指紋餘弦法在外部、第三方、公開可查證的資料上成立」，"
    "而非「已在真實產線驗證」。"
)

EXPECTED_ROWS = 10_000


# --------------------------------------------------------------------------- 訊號對應
@dataclass(frozen=True)
class ChannelMapping:
    """一條「UCI 欄位 → 本專案訊號」的對應規則。

    對應規則寫成資料而不是散在程式裡，是為了讓 `docs/factory_guardian/external_validation.md` 的對應表
    可以直接由這裡產生 —— 文件與程式不可能對不上。
    """

    channel: str
    """本模組內部使用的通道名稱。"""

    project_signal: str | None
    """對應到本專案 `twin/topology.py` 四訊號中的哪一個；`None` 表示專案沒有這個訊號。"""

    column: str | None
    """來源 CSV 欄位；`None` 表示由其他欄位推導。"""

    kind: str
    """``direct``（同一個物理量）/ ``proxy``（力學代理）/ ``derived``（推導）/ ``analog``（概念對應）。"""

    unit: str
    rationale: str
    """為什麼可以這樣對應。這段話會直接進文件，所以請寫成經得起追問的理由。"""

    group: str = "strict"
    """``strict``＝直接感測器對應；``derived``＝工程推導量（見 ablation 說明）。"""


#: 嚴格對應：只用「直接讀得到的感測器量」，一對一對到本專案的訊號語彙。
#: 這一組是主實驗，因為它才是 `agents/diagnosis.py` 真正吃到的東西（四個原始訊號）。
CHANNEL_MAPPINGS: tuple[ChannelMapping, ...] = (
    ChannelMapping(
        channel="temperature",
        project_signal="temperature",
        column="Process temperature [K]",
        kind="direct",
        unit="K",
        rationale=(
            "同一個物理量：製程溫度就是機台本體的工作溫度，"
            "對應本專案 M-A/M-B/M-C 的 temperature 感測器。"
        ),
    ),
    ChannelMapping(
        channel="current",
        project_signal="current",
        column="Torque [Nm]",
        kind="proxy",
        unit="Nm",
        rationale=(
            "扭矩是電流的力學代理。定速激磁下的馬達，軸端扭矩 T = k_t · I，"
            "扭矩與電樞電流成正比，因此扭矩上升在指紋空間中的方向與電流上升一致。"
            "本專案 motor_overload 的指紋是「current 上升 + rpm 下降」，"
            "在 AI4I 中就是「torque 上升 + rotational speed 下降」。"
            "注意這是代理不是同一個量：它不含電氣面的故障（如缺相、絕緣劣化）。"
        ),
    ),
    ChannelMapping(
        channel="rpm",
        project_signal="rpm",
        column="Rotational speed [rpm]",
        kind="direct",
        unit="rpm",
        rationale="同一個物理量：主軸轉速，對應本專案的 rpm 訊號。",
    ),
    ChannelMapping(
        channel="wear",
        project_signal=None,
        column="Tool wear [min]",
        kind="analog",
        unit="min",
        rationale=(
            "本專案四訊號沒有對應項。刀具磨耗是**累積量**，概念上對應 Digital Twin 的 "
            "health / fault_progress 遞減（Monitoring Agent 看得到健康度估計，"
            "所以把它放進觀測向量並沒有偷看 ground truth）。"
            "沒有這個通道，TWF 與 OSF 在物理上就無法辨識，因此列入嚴格對應組。"
        ),
    ),
)

#: 推導通道：工程師拿手冊會自己算的量，但不是直接讀到的感測器值。
#: 刻意獨立成一組並單獨做 ablation —— 因為 ΔT 與機械功率**正好是 AI4I 生成規則用的量**
#: （HDF 用 ΔT<8.6K 且 rpm<1380；PWF 用 P=T·ω 落在 [3500, 9000] W 之外）。
#: 用了它們，方法會更接近「知道出題規則」，數字會好看，但論述強度會下降。
#: 所以主實驗一律用 strict，derived 只當 ablation 報告。
DERIVED_MAPPINGS: tuple[ChannelMapping, ...] = (
    ChannelMapping(
        channel="thermal_margin",
        project_signal=None,
        column=None,
        kind="derived",
        unit="K",
        rationale=(
            "製程溫度 − 環境溫度，冷卻迴路的散熱裕度。這是熱管理的標準工程指標，"
            "但它也是 AI4I 生成 HDF 的判準之一，因此只列 ablation。"
        ),
        group="derived",
    ),
    ChannelMapping(
        channel="power",
        project_signal=None,
        column=None,
        kind="derived",
        unit="W",
        rationale=(
            "機械功率 P = 扭矩 × 角速度 = T · 2π·rpm/60。"
            "這是 AI4I 生成 PWF 的判準本身，因此只列 ablation。"
        ),
        group="derived",
    ),
)

#: 本專案有、AI4I 沒有的訊號。這一項必須主動揭露，不能只在附錄提一句。
MISSING_SIGNALS: tuple[tuple[str, str], ...] = (
    (
        "vibration",
        "AI4I 2020 不含振動量測。本專案四訊號中的 vibration 在此完全缺席，"
        "而 vibration 正是 bearing_degradation 指紋的主導分量（deltas 中權重最大者），"
        "所以 AI4I 上的結果**不能**外推到軸承類故障的辨識能力。"
        "軸承/旋轉件的外部驗證由 acoustics/（DCASE2020 pump，真實工業錄音）承擔。",
    ),
)

STRICT_CHANNELS: tuple[str, ...] = tuple(m.channel for m in CHANNEL_MAPPINGS)
DERIVED_CHANNELS: tuple[str, ...] = tuple(m.channel for m in DERIVED_MAPPINGS)
EXTENDED_CHANNELS: tuple[str, ...] = STRICT_CHANNELS + DERIVED_CHANNELS


# --------------------------------------------------------------------------- 故障模式對應
@dataclass(frozen=True)
class FaultModeMapping:
    """AI4I 故障模式 ↔ 本專案情境的對應。"""

    code: str
    label: str
    column: str
    project_fault_id: str | None
    """對應到 `twin/faults.py` 的哪一個故障；`None` 表示專案沒有直接對應。"""

    diagnosable: bool
    """是否可由感測器指紋判定。RNF 依定義**不可**判定，它是誤報測試素材。"""

    rationale: str
    generating_rule: str
    """AI4I 論文載明的生成規則。寫出來是誠實揭露：這份資料的標籤是規則產生的，不是量測的。"""


FAULT_MODES: tuple[FaultModeMapping, ...] = (
    FaultModeMapping(
        code="TWF",
        label="刀具磨耗失效 (Tool Wear Failure)",
        column="TWF",
        project_fault_id=None,
        diagnosable=True,
        rationale=(
            "本專案三個設備故障中沒有刀具類故障（最接近的是 Twin 的 health 遞減）。"
            "保留它是因為它提供一個「單一累積量主導、其餘訊號正常」的指紋型態，"
            "正好可以測試指紋法在「只有一個維度異常」時的鑑別力 ——"
            "這與 cooling_failure（溫度單獨異常）是同一類問題。"
        ),
        generating_rule="刀具磨耗達 200–240 min 之間的隨機值時觸發。",
    ),
    FaultModeMapping(
        code="HDF",
        label="散熱失效 (Heat Dissipation Failure)",
        column="HDF",
        project_fault_id="cooling_failure",
        diagnosable=True,
        rationale=(
            "直接對應本專案 cooling_failure 情境：散熱能力不足導致機台溫度失控。"
            "本專案的指紋是「temperature 大幅上升，vibration 與 current 幾乎正常」，"
            "AI4I 的 HDF 則是「散熱裕度不足且轉速偏低」——兩者在觀測面同樣以熱訊號主導。"
        ),
        generating_rule="製程溫度 − 環境溫度 < 8.6 K 且轉速 < 1380 rpm 時觸發。",
    ),
    FaultModeMapping(
        code="PWF",
        label="功率失效 (Power Failure)",
        column="PWF",
        project_fault_id="motor_overload",
        diagnosable=True,
        rationale=(
            "對應本專案 motor_overload：主軸驅動的功率超出正常工作區間。"
            "本專案的指紋是「current 上升 + rpm 下降」，AI4I 的 PWF 是「T·ω 離開 [3.5, 9] kW」。"
            "注意 PWF 是**雙側**條件（功率過低也算），這對單一原型的指紋法是硬考題，"
            "結果與檢討見 docs/factory_guardian/external_validation.md。"
        ),
        generating_rule="機械功率 P = 扭矩 × 角速度 低於 3500 W 或高於 9000 W 時觸發。",
    ),
    FaultModeMapping(
        code="OSF",
        label="過應變失效 (Overstrain Failure)",
        column="OSF",
        project_fault_id=None,
        diagnosable=True,
        rationale=(
            "對應本專案語彙中的「軸承／機構過載」。它是磨耗與扭矩的乘積超限，"
            "亦即「累積劣化 × 當下負載」——這正是本專案 Safety Agent 預測型規則"
            "（SR-02P/SR-03P）背後的直覺：現在不違規，但持續這樣跑會出事。"
        ),
        generating_rule="刀具磨耗 × 扭矩 超過 11000 (L) / 12000 (M) / 13000 (H) min·Nm 時觸發。",
    ),
    FaultModeMapping(
        code="RNF",
        label="隨機失效 (Random Failure)",
        column="RNF",
        project_fault_id=None,
        diagnosable=False,
        rationale=(
            "依定義**與感測器狀態無關**，因此不可能有指紋。它在本驗證中的角色是"
            "「不可診斷案例」的測試素材：正確行為是**拒絕給出高信心根因**，"
            "對應 diagnosis.py 的 no_fault gate 與 Orchestrator 的 confirm_diagnosis。"
        ),
        generating_rule="每筆製程有 0.1% 機率隨機觸發，與任何製程參數無關。",
    ),
)

MODE_BY_CODE: dict[str, FaultModeMapping] = {m.code: m for m in FAULT_MODES}
#: 可由指紋判定的模式（RNF 不在其中）。
DIAGNOSABLE_MODES: tuple[str, ...] = tuple(m.code for m in FAULT_MODES if m.diagnosable)
UNDIAGNOSABLE_MODES: tuple[str, ...] = tuple(m.code for m in FAULT_MODES if not m.diagnosable)

#: 「無設備故障徵兆」標籤。名稱刻意與 `agents/diagnosis.py` 的 ``NO_FAULT_ID`` 相同，
#: 方便交叉對照——但這裡是獨立定義的常數，不 import 對方，對方改動不會弄壞本模組。
NO_FAULT_LABEL = "no_equipment_fault"

PRODUCT_TYPES: tuple[str, ...] = ("L", "M", "H")


# --------------------------------------------------------------------------- 樣本
@dataclass(frozen=True)
class Sample:
    """一筆 AI4I 製程紀錄，已完成訊號對應。"""

    uid: int
    product_type: str
    channels: dict[str, float]
    """已對應到本專案語彙的通道值（原始單位，尚未正規化）。"""

    modes: tuple[str, ...]
    """這筆紀錄被標記的故障模式（可能多個，也可能空的）。"""

    machine_failure: int
    raw: dict[str, float] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """單標籤化：無模式→`no_equipment_fault`；單一模式→該模式；多模式→以 ``+`` 串接。"""
        if not self.modes:
            return NO_FAULT_LABEL
        if len(self.modes) == 1:
            return self.modes[0]
        return "+".join(self.modes)

    @property
    def is_normal(self) -> bool:
        return not self.modes

    @property
    def is_single_mode(self) -> bool:
        return len(self.modes) == 1

    @property
    def diagnosable_modes(self) -> tuple[str, ...]:
        return tuple(m for m in self.modes if MODE_BY_CODE[m].diagnosable)

    @property
    def is_rnf_only(self) -> bool:
        """只被標記 RNF 的紀錄：本驗證的「不可診斷」測試素材。"""
        return bool(self.modes) and all(not MODE_BY_CODE[m].diagnosable for m in self.modes)


def dataset_available(path: Path | None = None) -> bool:
    """資料集是否存在。CI 沒有 `data/external/` 時測試靠這個 skip。"""
    return (path or DATASET_CSV).exists()


def _derive(raw: dict[str, float]) -> dict[str, float]:
    """推導通道。公式與 `DERIVED_MAPPINGS` 的 rationale 一一對應。"""
    rpm = raw["Rotational speed [rpm]"]
    torque = raw["Torque [Nm]"]
    return {
        "thermal_margin": raw["Process temperature [K]"] - raw["Air temperature [K]"],
        "power": torque * (2.0 * math.pi * rpm / 60.0),
    }


def load_samples(path: Path | None = None) -> list[Sample]:
    """讀 CSV 並套用訊號對應層。

    只做「讀檔 + 欄位改名 + 推導」，不做任何過濾或平衡 ——
    切分與類別不平衡處理留在 `runner.py`，才不會有人在載入層偷偷動資料。
    """
    csv_path = path or DATASET_CSV
    if not csv_path.exists():
        raise FileNotFoundError(
            f"找不到 AI4I 2020 資料集：{csv_path}\n"
            f"請自 {DATASET_URL} 下載 ai4i2020.csv 後放到 {DATASET_DIR}/。"
        )

    samples: list[Sample] = []
    # utf-8-sig：原始檔第一欄帶 BOM，不處理的話欄名會變成 '﻿UDI'。
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw = {
                col: float(row[col])
                for col in (
                    "Air temperature [K]",
                    "Process temperature [K]",
                    "Rotational speed [rpm]",
                    "Torque [Nm]",
                    "Tool wear [min]",
                )
            }
            channels = {m.channel: raw[m.column] for m in CHANNEL_MAPPINGS if m.column}
            channels.update(_derive(raw))
            modes = tuple(m.code for m in FAULT_MODES if row[m.column].strip() == "1")
            samples.append(
                Sample(
                    uid=int(row["UDI"]),
                    product_type=row["Type"].strip(),
                    channels=channels,
                    modes=modes,
                    machine_failure=int(row["Machine failure"]),
                    raw=raw,
                )
            )
    return samples


def dataset_summary(samples: list[Sample]) -> dict[str, object]:
    """資料集摘要，用來在文件與測試中確認我們讀到的是同一份東西。"""
    by_mode = {m.code: sum(1 for s in samples if m.code in s.modes) for m in FAULT_MODES}
    return {
        "rows": len(samples),
        "machine_failure": sum(s.machine_failure for s in samples),
        "by_mode": by_mode,
        "multi_mode_rows": sum(1 for s in samples if len(s.modes) > 1),
        "rnf_only_rows": sum(1 for s in samples if s.is_rnf_only),
        "normal_rows": sum(1 for s in samples if s.is_normal),
        "by_product_type": {t: sum(1 for s in samples if s.product_type == t) for t in PRODUCT_TYPES},
    }


def mapping_table() -> list[dict[str, str]]:
    """給文件用的對應表（Markdown 由 runner 產生）。"""
    rows: list[dict[str, str]] = []
    for m in CHANNEL_MAPPINGS + DERIVED_MAPPINGS:
        rows.append(
            {
                "channel": m.channel,
                "project_signal": m.project_signal or "（專案無此訊號）",
                "column": m.column or "（推導）",
                "kind": m.kind,
                "group": m.group,
                "rationale": m.rationale,
            }
        )
    for name, note in MISSING_SIGNALS:
        rows.append(
            {
                "channel": "（缺）",
                "project_signal": name,
                "column": "—",
                "kind": "absent",
                "group": "missing",
                "rationale": note,
            }
        )
    return rows


__all__ = [
    "CHANNEL_MAPPINGS",
    "DATASET_CITATION",
    "DATASET_CSV",
    "DATASET_DIR",
    "DATASET_DOI",
    "DATASET_IS_SYNTHETIC",
    "DATASET_LICENSE",
    "DATASET_NAME",
    "DATASET_SOURCE",
    "DATASET_URL",
    "DERIVED_CHANNELS",
    "DERIVED_MAPPINGS",
    "DIAGNOSABLE_MODES",
    "EXPECTED_ROWS",
    "EXTENDED_CHANNELS",
    "FAULT_MODES",
    "MISSING_SIGNALS",
    "MODE_BY_CODE",
    "NO_FAULT_LABEL",
    "PRODUCT_TYPES",
    "STRICT_CHANNELS",
    "SYNTHETIC_NOTICE",
    "ChannelMapping",
    "FaultModeMapping",
    "Sample",
    "UNDIAGNOSABLE_MODES",
    "dataset_available",
    "dataset_summary",
    "load_samples",
    "mapping_table",
]
