"""CWRU 軸承振動資料集外部驗證 —— 補上 `bearing_degradation` 的驗證缺口。

## 這個模組為什麼存在

`docs/external_validation.md` §2.2 自己寫了一條缺口：AI4I 2020 **沒有振動訊號**，
所以 Demo 主線情境 `bearing-degradation` 的診斷能力「這份驗證完全沒有覆蓋到」。
而振動正是它指紋裡權重最大的分量（`FAULTS["bearing_degradation"].deltas` 的
vibration = 6.8，為最大項）。整個提案最常被展示的那條情境，外部驗證是空的。

Case Western Reserve University Bearing Data Center 的軸承振動資料集正好補這一塊：
**真實加速規量測**（不是模擬）、公開、有數十篇論文引用、故障類型正好是內圈／外圈／滾珠 ——
也就是 `bearing_degradation` 這個標籤底下真實存在的三種失效模式。

## 誠實邊界（請先讀，程式裡每一條都有對應常數）

1. **CWRU 是實驗台（test rig），不是產線。** 2 hp 馬達 + 測功機 + 扭矩感測器，
   單一轉子、固定轉速、無切削負載、無夾治具、無環境干擾。它證明的是
   「振動時域特徵能分辨軸承故障類型」，**不是**「在 CNC 加工現場也能」。
2. **故障是人工加工出來的**（電火花加工的單點凹坑，0.007″/0.021″），
   不是自然劣化。自然劣化是漸進、多點、伴隨潤滑劣化與溫升的過程；
   人工凹坑一開始就是成熟缺陷，訊號比真實早期劣化**乾淨得多**。
   所以這裡的數字是**上界**，不能拿來承諾早期偵測提前量。
3. **與本專案四訊號的對應只有一條是直接的**（見 `FEATURE_MAPPINGS`）：
   CWRU 給的是原始加速規波形，本專案的 Digital Twin 只輸出一個 vibration RMS 純量。
   其餘特徵（峰值因數、峭度、頻帶能量比）在專案裡**沒有感測器對應**，
   它們對應到的是聲學那一側的指標語彙（`acoustics/signatures.py`）。
4. **正常樣本與故障樣本的取樣率不同**（48 kHz vs 12 kHz），這是 CWRU 一個惡名昭彰的陷阱。
   直接混用會讓「正常 vs 故障」在頻譜特徵上被取樣率本身分開 —— 那是資料集的假象，
   不是方法的能力。本模組把正常樣本**降採樣 4 倍**對齊到 12 kHz，見 `NORMAL_DECIMATION`。

## 兩個評估

* **偵測（無監督）**：只用正常樣本擬合基準線，故障樣本評分，報 AUC / pAUC。
  評分函數就是本專案的 `signal_strength`（正規化偏離向量的 L2 長度，
  也就是 `diagnosis.py` 的 no-fault gate 用的那個量），對照組是單一訊號（RMS）門檻。
* **歸因（指紋餘弦）**：內圈／外圈／滾珠三類，報 Top-1 與 macro-F1，
  對照組是單一特徵最近質心規則、多數決與 LogisticRegression。

**切分：leave-one-load-out**（0/1/2/3 hp 各當一次測試集）。
不用隨機切窗的理由很實際：同一段錄音切出來的窗彼此高度相關，
隨機切分會讓訓練與測試共享同一段錄音，數字會虛高到沒有意義。

重現指令見 `docs/cwru_validation.md`。
"""

from __future__ import annotations

import json
import math
import statistics
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .fingerprint import cosine
from .metrics import (
    ClassificationReport,
    classification_report,
    partial_auc,
    random_top_k,
    roc_auc,
    top_k_hit_rate,
)

# --------------------------------------------------------------------------- 資料集
DATASET_DIR = Path(__file__).resolve().parents[2] / "data" / "external" / "cwru"

DATASET_NAME = "Case Western Reserve University Bearing Data Center"
DATASET_SOURCE = "Case Western Reserve University, Bearing Data Center"
DATASET_URL = "https://engineering.case.edu/bearingdatacenter"
FILE_URL_TEMPLATE = "https://engineering.case.edu/sites/default/files/{file_id}.mat"
DATASET_LICENSE = (
    "CWRU Bearing Data Center 公開提供，慣例上引用時須標示出處；"
    "**未附 CC 類授權條款**。商業部署前應直接向 CWRU 確認授權範圍。"
    "這一點與 AI4I 的 CC BY 4.0 不同，兩條驗證線的授權狀態不可混為一談。"
)
DATASET_CITATION = (
    "Case Western Reserve University Bearing Data Center Website, "
    "https://engineering.case.edu/bearingdatacenter （accessed 2026）。"
)

#: **這不是合成資料**：CWRU 是真實加速規量測。這句話與 `ai4i.SYNTHETIC_NOTICE` 正好相反，
#: 因此更要把「實驗台 ≠ 產線」寫清楚，否則很容易被誤讀成「已在真實工廠驗證」。
DATASET_IS_SYNTHETIC = False
REALITY_NOTICE = (
    "CWRU 為真實加速規量測（非合成），但取自**實驗台**：2 hp 馬達 + 測功機，"
    "單一轉子、固定轉速、無切削負載與現場干擾；故障為電火花加工的人工單點凹坑，"
    "非自然劣化。本驗證證明的是「振動特徵能分辨軸承故障類型」，"
    "而非「Factory Guardian 已在真實產線的軸承劣化上驗證」。"
)

#: 正常基準檔的取樣率是 48 kHz，故障檔是 12 kHz。**這是本資料集最容易踩的坑。**
#: 不對齊的話，「正常 vs 故障」會在頻帶能量比上被取樣率本身分開，
#: AUC 會漂亮得不像話，但量到的是資料集的假象而不是方法。
#: 本模組把正常檔降採樣 4 倍（48 → 12 kHz）之後才抽特徵。
#: 這個事實是實測驗證過的，不是照抄網路傳言：把正常檔當 48 kHz 解讀時，
#: 頻譜的主要譜線落在轉軸頻率的整數倍（12×、15×、18×、20×、48×）；
#: 當成 12 kHz 解讀則變成 3×、3.75×、4.5×、5.75× 這種四分之一倍頻，物理上說不通。
#: 由 `tests/test_cwru.py::test_normal_recordings_are_declared_48k` 守住。
SAMPLE_RATE_HZ = 12_000
NORMAL_SAMPLE_RATE_HZ = 48_000
NORMAL_DECIMATION = NORMAL_SAMPLE_RATE_HZ // SAMPLE_RATE_HZ

#: 每個分析窗的樣本數。4096 @ 12 kHz = 0.341 秒 ≈ 轉軸 10 圈（1797 rpm），
#: 足以讓內圈故障的調變邊帶成形；再長就會讓每段錄音只切出十幾個窗，
#: per-class 的數字不確定性變得不能引用。
WINDOW = 4096

NO_FAULT_LABEL = "normal"
#: 可歸因的三個故障類別。它們全部落在本專案的 `bearing_degradation` 這一個標籤底下 ——
#: 這正是本驗證補上的東西：專案只說「軸承劣化」，CWRU 讓我們量到「是哪一種軸承劣化」。
FAULT_CLASSES: tuple[str, ...] = ("inner_race", "ball", "outer_race")


@dataclass(frozen=True)
class Recording:
    """一段錄音。`file_id` 就是 CWRU 網站上的檔名（例：`105.mat`）。"""

    file_id: int
    fault_class: str
    """`normal` / `inner_race` / `ball` / `outer_race`。"""

    fault_size_in: float
    """人工凹坑直徑（英吋）。0.0 = 正常。"""

    load_hp: int
    """馬達負載（0–3 hp）。leave-one-load-out 就是以它切 fold。"""

    rpm: int
    sample_rate_hz: int

    @property
    def mat_key(self) -> str:
        """.mat 內的變數名。CWRU 的命名規則是 ``X<三位檔號>_DE_time``（drive end 加速規）。

        注意 `99.mat` 內同時含有 `X098_*` 與 `X099_*`（上游檔案本身的瑕疵），
        照這條規則取名就會拿到正確的那一組 —— 這也是為什麼要用規則而不是「取第一個變數」。
        """
        return f"X{self.file_id:03d}_DE_time"

    @property
    def label(self) -> str:
        return self.fault_class

    @property
    def is_normal(self) -> bool:
        return self.fault_class == NO_FAULT_LABEL


#: 12k Drive End 資料，凹坑 0.007″ 與 0.021″，負載 0–3 hp；外圈取 @6:00（負載區正下方）。
#: 為什麼只取兩種凹坑尺寸：0.007″ 是「剛開始壞」、0.021″ 是「壞得很明顯」，
#: 兩端各取一組就足以看出方法對嚴重度的敏感性，全取只是讓正常/故障更不平衡。
RECORDINGS: tuple[Recording, ...] = (
    # 正常基準（48 kHz，載入時降採樣至 12 kHz）
    Recording(97, "normal", 0.0, 0, 1797, NORMAL_SAMPLE_RATE_HZ),
    Recording(98, "normal", 0.0, 1, 1772, NORMAL_SAMPLE_RATE_HZ),
    Recording(99, "normal", 0.0, 2, 1750, NORMAL_SAMPLE_RATE_HZ),
    Recording(100, "normal", 0.0, 3, 1730, NORMAL_SAMPLE_RATE_HZ),
    # 內圈 0.007″
    Recording(105, "inner_race", 0.007, 0, 1797, SAMPLE_RATE_HZ),
    Recording(106, "inner_race", 0.007, 1, 1772, SAMPLE_RATE_HZ),
    Recording(107, "inner_race", 0.007, 2, 1750, SAMPLE_RATE_HZ),
    Recording(108, "inner_race", 0.007, 3, 1730, SAMPLE_RATE_HZ),
    # 滾珠 0.007″
    Recording(118, "ball", 0.007, 0, 1797, SAMPLE_RATE_HZ),
    Recording(119, "ball", 0.007, 1, 1772, SAMPLE_RATE_HZ),
    Recording(120, "ball", 0.007, 2, 1750, SAMPLE_RATE_HZ),
    Recording(121, "ball", 0.007, 3, 1730, SAMPLE_RATE_HZ),
    # 外圈 0.007″ @6:00
    Recording(130, "outer_race", 0.007, 0, 1797, SAMPLE_RATE_HZ),
    Recording(131, "outer_race", 0.007, 1, 1772, SAMPLE_RATE_HZ),
    Recording(132, "outer_race", 0.007, 2, 1750, SAMPLE_RATE_HZ),
    Recording(133, "outer_race", 0.007, 3, 1730, SAMPLE_RATE_HZ),
    # 內圈 0.021″
    Recording(209, "inner_race", 0.021, 0, 1797, SAMPLE_RATE_HZ),
    Recording(210, "inner_race", 0.021, 1, 1772, SAMPLE_RATE_HZ),
    Recording(211, "inner_race", 0.021, 2, 1750, SAMPLE_RATE_HZ),
    Recording(212, "inner_race", 0.021, 3, 1730, SAMPLE_RATE_HZ),
    # 滾珠 0.021″
    Recording(222, "ball", 0.021, 0, 1797, SAMPLE_RATE_HZ),
    Recording(223, "ball", 0.021, 1, 1772, SAMPLE_RATE_HZ),
    Recording(224, "ball", 0.021, 2, 1750, SAMPLE_RATE_HZ),
    Recording(225, "ball", 0.021, 3, 1730, SAMPLE_RATE_HZ),
    # 外圈 0.021″ @6:00
    Recording(234, "outer_race", 0.021, 0, 1797, SAMPLE_RATE_HZ),
    Recording(235, "outer_race", 0.021, 1, 1772, SAMPLE_RATE_HZ),
    Recording(236, "outer_race", 0.021, 2, 1750, SAMPLE_RATE_HZ),
    Recording(237, "outer_race", 0.021, 3, 1730, SAMPLE_RATE_HZ),
)

LOADS: tuple[int, ...] = (0, 1, 2, 3)


# --------------------------------------------------------------------------- 特徵對應
@dataclass(frozen=True)
class FeatureMapping:
    """一條「CWRU 振動特徵 → 本專案訊號語彙」的對應規則。

    寫成資料而不是散在程式裡，理由與 `ai4i.CHANNEL_MAPPINGS` 相同：
    `docs/cwru_validation.md` 的對應表由這裡產生，文件與程式不可能對不上。
    """

    feature: str
    project_signal: str | None
    """對應到 `twin/topology.py` 的哪一個感測器訊號；`None` = 專案沒有這個量。"""

    acoustic_indicator: str | None
    """對應到 `acoustics/signatures.py` 的哪一個聲學指標；`None` = 沒有對應。"""

    kind: str
    """``direct`` / ``analog``（概念對應）/ ``absent``（專案沒有這個量）。"""

    unit: str
    rationale: str


FEATURE_MAPPINGS: tuple[FeatureMapping, ...] = (
    FeatureMapping(
        feature="rms",
        project_signal="vibration",
        acoustic_indicator=None,
        kind="direct",
        unit="g",
        rationale=(
            "同一個量：本專案 vibration 感測器輸出的就是振動 RMS（mm/s），"
            "CWRU 給的是加速度 RMS（g）。單位不同但都是「振動能量的有效值」，"
            "而且兩邊都經過各自 scale 正規化才進向量空間，量綱在餘弦比對中被消掉。"
            "**這是四個訊號裡唯一一條直接對應。**"
        ),
    ),
    FeatureMapping(
        feature="crest_factor",
        project_signal=None,
        acoustic_indicator="crest_factor_db",
        kind="analog",
        unit="",
        rationale=(
            "峰值 / RMS。軸承出現局部凹坑時每滾過一次就產生一個衝擊脈衝，"
            "能量沒有明顯增加、波形卻變尖 —— RMS 看不到、峰值因數看得到。"
            "本專案的感測器側沒有這個量（Twin 只輸出 RMS 純量），"
            "但聲學側的 `crest_factor_db` 就是同一個概念（取 dB）。"
        ),
    ),
    FeatureMapping(
        feature="kurtosis",
        project_signal=None,
        acoustic_indicator=None,
        kind="absent",
        unit="",
        rationale=(
            "四階動差，衝擊性的標準指標（高斯訊號 = 0）。本專案感測器側與聲學側**都沒有**這個量。"
            "列出來是因為它是軸承診斷的教科書特徵，"
            "不列會讓「我們選的特徵是不是刻意挑好看的」變成一個沒有答案的問題。"
        ),
    ),
    FeatureMapping(
        feature="band_0_500",
        project_signal=None,
        acoustic_indicator="tonal_ratio",
        kind="analog",
        unit="",
        rationale=(
            "0–500 Hz 佔總能量的比例：轉軸頻率及其低階諧波所在的頻帶，"
            "對應聲學指標裡的「純音佔比」（旋轉件的規律成分）。"
        ),
    ),
    FeatureMapping(
        feature="band_500_1500",
        project_signal=None,
        acoustic_indicator=None,
        kind="absent",
        unit="",
        rationale="500–1500 Hz：齒輪/葉片類的中頻帶，本專案兩側都沒有對應量。",
    ),
    FeatureMapping(
        feature="band_1500_3000",
        project_signal=None,
        acoustic_indicator=None,
        kind="absent",
        unit="",
        rationale="1500–3000 Hz：軸承座結構共振的下半段，本專案兩側都沒有對應量。",
    ),
    FeatureMapping(
        feature="band_3000_6000",
        project_signal=None,
        acoustic_indicator="high_band_ratio",
        kind="analog",
        unit="",
        rationale=(
            "3–6 kHz 佔總能量的比例：軸承衝擊激發的高頻共振帶，"
            "正是包絡分析要解調的那一段。對應聲學指標的 `high_band_ratio`"
            "（本專案聲學側判斷「多出高頻嘶聲」用的就是它）。"
        ),
    ),
)

FEATURES: tuple[str, ...] = tuple(m.feature for m in FEATURE_MAPPINGS)

#: 頻帶定義（Hz），與 `FEATURE_MAPPINGS` 的 band_* 一一對應。
BANDS: tuple[tuple[str, float, float], ...] = (
    ("band_0_500", 0.0, 500.0),
    ("band_500_1500", 500.0, 1500.0),
    ("band_1500_3000", 1500.0, 3000.0),
    ("band_3000_6000", 3000.0, 6000.0),
)


def mapping_table() -> str:
    """由 `FEATURE_MAPPINGS` 產生 Markdown 對應表（文件直接引用這個輸出）。"""
    lines = [
        "| CWRU 特徵 | 專案感測器訊號 | 專案聲學指標 | 類型 | 理由 |",
        "|---|---|---|---|---|",
    ]
    for m in FEATURE_MAPPINGS:
        lines.append(
            f"| `{m.feature}` | {m.project_signal or '（無）'} | "
            f"{m.acoustic_indicator or '（無）'} | {m.kind} | {m.rationale} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- 取得資料
def dataset_available(directory: Path | None = None) -> bool:
    """所有宣告的錄音檔都在才算可用。少一個就 skip —— 半套資料算出來的數字不能引用。"""
    root = directory or DATASET_DIR
    return all((root / f"{r.file_id}.mat").exists() for r in RECORDINGS)


def missing_files(directory: Path | None = None) -> list[int]:
    root = directory or DATASET_DIR
    return [r.file_id for r in RECORDINGS if not (root / f"{r.file_id}.mat").exists()]


def download(directory: Path | None = None, timeout: float = 120.0) -> list[int]:
    """下載缺少的 .mat 檔，回傳實際下載的檔號。

    刻意**不**在 `load_windows()` 裡自動呼叫：測試與 CI 不該在背後打外部網路。
    下載是一個明確的動作（`python -m factory_guardian.validation.cwru --download`），
    失敗時的補救指令寫在 `docs/cwru_validation.md`。
    """
    root = directory or DATASET_DIR
    root.mkdir(parents=True, exist_ok=True)
    fetched: list[int] = []
    for file_id in missing_files(root):
        url = FILE_URL_TEMPLATE.format(file_id=file_id)
        target = root / f"{file_id}.mat"
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            target.write_bytes(response.read())
        fetched.append(file_id)
    return fetched


# --------------------------------------------------------------------------- 特徵抽取
@dataclass(frozen=True)
class Window:
    """一個分析窗抽出來的特徵，加上它的來源標籤。"""

    uid: int
    recording: int
    fault_class: str
    fault_size_in: float
    load_hp: int
    features: dict[str, float]

    @property
    def is_normal(self) -> bool:
        return self.fault_class == NO_FAULT_LABEL


def _signal(recording: Recording, directory: Path) -> "list[float]":
    """讀出單段 drive-end 波形，必要時降採樣到 12 kHz。"""
    import numpy as np
    from scipy.io import loadmat
    from scipy.signal import decimate

    mat = loadmat(str(directory / f"{recording.file_id}.mat"))
    if recording.mat_key not in mat:
        raise KeyError(
            f"{recording.file_id}.mat 內找不到變數 {recording.mat_key}；"
            "檔案可能下載不完整，請重新下載（見 docs/cwru_validation.md）。"
        )
    x = np.asarray(mat[recording.mat_key], dtype=float).ravel()
    if recording.sample_rate_hz != SAMPLE_RATE_HZ:
        factor = recording.sample_rate_hz // SAMPLE_RATE_HZ
        # 抗混疊低通後抽取。少了這一步，48 kHz 訊號 6 kHz 以上的能量會折疊回來，
        # 正常樣本的高頻帶能量比就會被人為抬高 —— 那正是要避免的那個假象。
        x = decimate(x, factor, ftype="fir", zero_phase=True)
    return x


def extract_features(segment) -> dict[str, float]:
    """單一分析窗的七個特徵。

    刻意全部是**時域/頻帶的低階統計量**，不做包絡解調也不算故障特徵頻率（BPFI/BPFO/BSF）。
    理由：包絡解調需要知道軸承的幾何參數與轉速，那是「為這個資料集量身訂做的診斷器」，
    量出來的是我們的訊號處理功力，不是本專案那套「多訊號組合成向量再比方向」的方法。
    本專案的 Diagnosis Agent 拿得到的就是幾個低階指標，這裡也只給它同樣的東西。
    """
    import numpy as np

    x = np.asarray(segment, dtype=float)
    x = x - x.mean()
    rms = float(np.sqrt(np.mean(x**2)))
    peak = float(np.max(np.abs(x)))
    safe_rms = max(rms, 1e-12)
    m2 = float(np.mean(x**2))
    m4 = float(np.mean(x**4))
    kurtosis = m4 / max(m2**2, 1e-24) - 3.0

    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE_HZ)
    total = float(spectrum.sum()) or 1.0

    out: dict[str, float] = {
        "rms": rms,
        "crest_factor": peak / safe_rms,
        "kurtosis": kurtosis,
    }
    nyquist = SAMPLE_RATE_HZ / 2.0
    for name, lo, hi in BANDS:
        # 上界一律開區間，唯獨最後一個頻帶要含 Nyquist bin ——
        # 少了這一格，四個頻帶佔比就不會加總成 1，之後任何「其餘能量跑去哪了」
        # 的追問都會得到一個看起來很小、其實是 bug 的答案。
        mask = (freqs >= lo) & (freqs <= hi if hi >= nyquist else freqs < hi)
        out[name] = float(spectrum[mask].sum()) / total
    return out


def load_windows(directory: Path | None = None) -> list[Window]:
    """把所有錄音切成不重疊的分析窗並抽特徵。

    **不重疊**是刻意的：重疊窗會讓相鄰樣本共享大部分原始資料，
    等於偷偷把樣本數灌水，per-class 的信賴區間會假性收窄。
    """
    root = directory or DATASET_DIR
    windows: list[Window] = []
    uid = 0
    for recording in RECORDINGS:
        signal = _signal(recording, root)
        n = len(signal) // WINDOW
        for i in range(n):
            segment = signal[i * WINDOW : (i + 1) * WINDOW]
            windows.append(
                Window(
                    uid=uid,
                    recording=recording.file_id,
                    fault_class=recording.fault_class,
                    fault_size_in=recording.fault_size_in,
                    load_hp=recording.load_hp,
                    features=extract_features(segment),
                )
            )
            uid += 1
    return windows


# --------------------------------------------------------------------------- 正規化
@dataclass
class Normalizer:
    """`(值 − nominal) / scale`，nominal/scale **只**由訓練切分的正常窗估計。

    與 `fingerprint.Normalizer` 同一個定義（那邊綁在 `ai4i.Sample` 上，這裡吃 `Window`）。
    只用正常樣本的理由不變：nominal 的定義就是「這台機器沒事時長什麼樣」。
    """

    nominal: dict[str, float] = field(default_factory=dict)
    scale: dict[str, float] = field(default_factory=dict)

    @classmethod
    def fit(cls, windows: list[Window]) -> "Normalizer":
        normals = [w for w in windows if w.is_normal] or windows
        nominal, scale = {}, {}
        for name in FEATURES:
            values = [w.features[name] for w in normals]
            mean = statistics.fmean(values)
            sd = statistics.pstdev(values) if len(values) > 1 else 0.0
            nominal[name] = mean
            scale[name] = max(sd, 1e-9)
        return cls(nominal=nominal, scale=scale)

    def deviation(self, window: Window) -> dict[str, float]:
        return {n: (window.features[n] - self.nominal[n]) / self.scale[n] for n in FEATURES}

    @staticmethod
    def strength(vector: dict[str, float]) -> float:
        """偏離向量長度。這就是 `diagnosis.py` 的 `signal_strength`／no-fault gate 用的量。"""
        return math.sqrt(sum(v * v for v in vector.values()))


# --------------------------------------------------------------------------- 模型
def _centroid(vectors: list[dict[str, float]]) -> dict[str, float]:
    n = len(vectors)
    return {name: sum(v[name] for v in vectors) / n for name in FEATURES}


def _kmeans(vectors: list[dict[str, float]], k: int, iters: int = 40) -> list[list[dict[str, float]]]:
    """最小可用 k-means（確定性初始化）。與 `fingerprint._kmeans` 同一套規則。

    初始化沿變異最大的特徵等分位取點，不用亂數 —— 結果可重現且不依賴 seed 品質。
    """
    if k <= 1 or len(vectors) <= k:
        return [vectors]

    def variance(values: list[float]) -> float:
        return statistics.pvariance(values) if len(values) > 1 else 0.0

    spread = max(FEATURES, key=lambda n: variance([v[n] for v in vectors]))
    ordered = sorted(vectors, key=lambda v: v[spread])
    centers = [
        _centroid(chunk)
        for chunk in (ordered[i * len(ordered) // k : (i + 1) * len(ordered) // k] for i in range(k))
        if chunk
    ]

    def sqdist(a: dict[str, float], b: dict[str, float]) -> float:
        return sum((a[n] - b[n]) ** 2 for n in FEATURES)

    for _ in range(iters):
        groups: list[list[dict[str, float]]] = [[] for _ in centers]
        for v in vectors:
            groups[min(range(len(centers)), key=lambda i: sqdist(v, centers[i]))].append(v)
        new_centers = [_centroid(g) if g else centers[i] for i, g in enumerate(groups)]
        if all(sqdist(a, b) < 1e-12 for a, b in zip(centers, new_centers)):
            centers = new_centers
            break
        centers = new_centers
    groups = [[] for _ in centers]
    for v in vectors:
        groups[min(range(len(centers)), key=lambda i: sqdist(v, centers[i]))].append(v)
    return [g for g in groups if g]


@dataclass
class FingerprintModel:
    """指紋餘弦歸因器（CWRU 版）。與 `fingerprint.FingerprintModel` 同一個方法，
    差別只在樣本型別與「這裡沒有歷史先驗也沒有文件語料」。

    先驗與文件在 CWRU 上都不存在（沒有機台 id、沒有手冊），所以 `combined` 退化成
    `W_SIGNATURE × max(0, cos)` —— 常數項對所有候選相同，不影響排名。
    這一點必須寫出來：本驗證量的是**指紋餘弦這一項**，不是完整的三項融合。
    """

    n_prototypes: int = 1
    normalizer: Normalizer | None = field(default=None, init=False)
    prototypes: dict[str, list[dict[str, float]]] = field(default_factory=dict, init=False)

    def fit(self, train: list[Window]) -> "FingerprintModel":
        self.normalizer = Normalizer.fit(train)
        self.prototypes = {}
        for cls in FAULT_CLASSES:
            vectors = [self.normalizer.deviation(w) for w in train if w.fault_class == cls]
            if not vectors:
                continue
            if self.n_prototypes <= 1 or len(vectors) < 2 * self.n_prototypes:
                self.prototypes[cls] = [_centroid(vectors)]
            else:
                self.prototypes[cls] = [_centroid(g) for g in _kmeans(vectors, self.n_prototypes)]
        return self

    def rank(self, window: Window) -> list[str]:
        assert self.normalizer is not None, "必須先 fit()"
        observed = self.normalizer.deviation(window)
        scored = [
            (cls, max(cosine(observed, p) for p in protos))
            for cls, protos in self.prototypes.items()
        ]
        scored.sort(key=lambda kv: -kv[1])
        return [cls for cls, _ in scored]

    def predict(self, window: Window) -> str:
        return self.rank(window)[0]


@dataclass
class SingleFeatureRule:
    """對照組：只看**一個**特徵的最近質心規則 —— 「現行流程」的振動版本。

    選哪一個特徵是在**訓練切分**上以 macro-F1 最佳化決定的，不是拍腦袋挑的，
    這樣才不會贏得太廉價。與 `baselines.ThresholdRuleBaseline` 的精神一致：
    它與指紋法的唯一差別是「一次只看一個維度」。
    """

    feature: str = "rms"
    centroids: dict[str, float] = field(default_factory=dict)
    normalizer: Normalizer | None = field(default=None, init=False)

    def fit(self, train: list[Window]) -> "SingleFeatureRule":
        normalizer = Normalizer.fit(train)
        best_feature, best_score, best_centroids = FEATURES[0], -1.0, {}
        for name in FEATURES:
            centroids = {}
            for cls in FAULT_CLASSES:
                values = [normalizer.deviation(w)[name] for w in train if w.fault_class == cls]
                if values:
                    centroids[cls] = statistics.fmean(values)
            if len(centroids) < 2:
                continue
            faults = [w for w in train if not w.is_normal]
            y_true = [w.fault_class for w in faults]
            y_pred = [
                min(centroids, key=lambda c: abs(normalizer.deviation(w)[name] - centroids[c]))
                for w in faults
            ]
            score = classification_report(y_true, y_pred, FAULT_CLASSES).macro_f1
            if score > best_score:
                best_feature, best_score, best_centroids = name, score, centroids
        self.feature = best_feature
        self.centroids = best_centroids
        self.normalizer = normalizer
        return self

    def rank(self, window: Window) -> list[str]:
        assert self.normalizer is not None, "必須先 fit()"
        value = self.normalizer.deviation(window)[self.feature]
        return sorted(self.centroids, key=lambda c: abs(value - self.centroids[c]))

    def predict(self, window: Window) -> str:
        return self.rank(window)[0]


# --------------------------------------------------------------------------- 結果容器
@dataclass
class DetectionResult:
    """偵測（正常 vs 故障）的成績。"""

    method: str
    auc: float
    pauc: float
    fold_auc: list[float] = field(default_factory=list)

    @property
    def auc_mean(self) -> float:
        return statistics.fmean(self.fold_auc) if self.fold_auc else self.auc

    @property
    def auc_std(self) -> float:
        return statistics.pstdev(self.fold_auc) if len(self.fold_auc) > 1 else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "auc": round(self.auc, 4),
            "pauc": round(self.pauc, 4),
            "fold_auc_mean": round(self.auc_mean, 4),
            "fold_auc_std": round(self.auc_std, 4),
        }


@dataclass
class AttributionResult:
    """歸因（三種軸承故障）的成績。"""

    method: str
    top1: float
    top2: float
    report: ClassificationReport
    fold_macro_f1: list[float] = field(default_factory=list)

    @property
    def macro_f1_mean(self) -> float:
        return statistics.fmean(self.fold_macro_f1) if self.fold_macro_f1 else self.report.macro_f1

    @property
    def macro_f1_std(self) -> float:
        return statistics.pstdev(self.fold_macro_f1) if len(self.fold_macro_f1) > 1 else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "top1": round(self.top1, 4),
            "top2": round(self.top2, 4),
            "macro_f1_mean_over_folds": round(self.macro_f1_mean, 4),
            "macro_f1_std_over_folds": round(self.macro_f1_std, 4),
            "pooled": self.report.to_dict(),
        }


@dataclass
class CwruReport:
    dataset: dict[str, object]
    config: dict[str, object]
    detection: list[DetectionResult]
    attribution: list[AttributionResult]
    severity: list[dict[str, object]]
    severity_transfer: list[dict[str, object]]
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "config": self.config,
            "detection": [r.to_dict() for r in self.detection],
            "attribution": [r.to_dict() for r in self.attribution],
            "severity": self.severity,
            "severity_transfer": self.severity_transfer,
            "notes": self.notes,
        }

    def to_markdown(self) -> str:
        ds = self.dataset
        lines: list[str] = []
        lines.append("## 資料集")
        lines.append("")
        lines.append(f"- 名稱：{DATASET_NAME}")
        lines.append(f"- 授權：{DATASET_LICENSE}")
        lines.append(
            f"- 錄音 {ds['recordings']} 段 → 分析窗 {ds['windows']} 個"
            f"（正常 {ds['normal_windows']}、故障 {ds['fault_windows']}）"
        )
        lines.append(f"- 每類窗數：{ds['by_class']}")
        lines.append("")
        lines.append(f"> **{REALITY_NOTICE}**")
        lines.append("")

        lines.append("## 任務一：偵測（正常 vs 故障，正常樣本擬合）")
        lines.append("")
        lines.append("| 方法 | AUC | pAUC (p=0.1) | fold AUC (mean±sd) |")
        lines.append("|---|---:|---:|---:|")
        for r in self.detection:
            lines.append(
                f"| {r.method} | {r.auc:.3f} | {r.pauc:.3f} | {r.auc_mean:.3f}±{r.auc_std:.3f} |"
            )
        lines.append("")

        lines.append("## 任務二：歸因（內圈／滾珠／外圈）")
        lines.append("")
        lines.append("| 方法 | Top-1 | Top-2 | macro-P | macro-R | macro-F1 | fold macro-F1 (mean±sd) |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for r in self.attribution:
            lines.append(
                f"| {r.method} | {r.top1:.3f} | {r.top2:.3f} | "
                f"{r.report.macro_precision:.3f} | {r.report.macro_recall:.3f} | "
                f"{r.report.macro_f1:.3f} | {r.macro_f1_mean:.3f}±{r.macro_f1_std:.3f} |"
            )
        lines.append("")

        primary = next((r for r in self.attribution if r.method == "fingerprint_cosine"), None)
        if primary:
            lines.append("### 指紋餘弦法：每類 precision / recall")
            lines.append("")
            lines.append("| 類別 | support | precision | recall | F1 |")
            lines.append("|---|---:|---:|---:|---:|")
            for label in primary.report.labels:
                m = primary.report.per_class[label]
                lines.append(
                    f"| {label} | {m.support} | {m.precision:.3f} | {m.recall:.3f} | {m.f1:.3f} |"
                )
            lines.append("")
            lines.append("### 指紋餘弦法：混淆矩陣（leave-one-load-out pooled）")
            lines.append("")
            lines.append(primary.report.matrix_markdown())
            lines.append("")

        if self.severity:
            lines.append("## 依凹坑尺寸拆解（指紋餘弦法 Top-1）")
            lines.append("")
            lines.append("| 凹坑直徑 | 窗數 | Top-1 | 偵測 AUC |")
            lines.append("|---|---:|---:|---:|")
            for row in self.severity:
                lines.append(
                    f"| {row['fault_size_in']}″ | {row['windows']} | "
                    f"{row['top1']:.3f} | {row['detection_auc']:.3f} |"
                )
            lines.append("")

        if self.severity_transfer:
            lines.append("## 跨嚴重度轉移（訓練與測試的凹坑尺寸不同）")
            lines.append("")
            lines.append("| 訓練凹坑 | 測試凹坑 | 測試窗數 | 指紋 Top-1 | 單一特徵規則 Top-1 | LR Top-1 |")
            lines.append("|---|---|---:|---:|---:|---:|")
            for row in self.severity_transfer:
                lr = row.get("logistic_regression_top1")
                lines.append(
                    f"| {row['train_size_in']}″ | {row['test_size_in']}″ | {row['windows']} | "
                    f"{row['fingerprint_top1']:.3f} | {row['single_feature_top1']:.3f} | "
                    + (f"{lr:.3f} |" if lr is not None else "（未執行） |")
                )
            lines.append("")

        if self.notes:
            lines.append("## 執行備註")
            lines.append("")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------- 執行
def _sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401
    except Exception:
        return False
    return True


def run_validation(
    directory: Path | None = None, n_prototypes: int = 1, seed: int = 20260809
) -> CwruReport:
    """leave-one-load-out 跑完偵測與歸因兩個任務。

    `seed` 只用在 LogisticRegression 的 `random_state`；指紋法與門檻 baseline
    的初始化都是確定性的（不吃亂數），所以本模組的主結果與 seed 無關。
    """
    windows = load_windows(directory)
    notes: list[str] = []

    by_class: dict[str, int] = {}
    for w in windows:
        by_class[w.fault_class] = by_class.get(w.fault_class, 0) + 1

    # ---------------------------------------------------------------- 各 fold 預測
    strength_score: dict[int, float] = {}
    rms_score: dict[int, float] = {}
    lof_score: dict[int, float] = {}
    rankings: dict[str, dict[int, list[str]]] = {}
    fold_auc: dict[str, list[float]] = {}

    use_sklearn = _sklearn_available()
    for load in LOADS:
        train = [w for w in windows if w.load_hp != load]
        test = [w for w in windows if w.load_hp == load]
        if not test:
            continue
        train_normal = [w for w in train if w.is_normal]
        train_fault = [w for w in train if not w.is_normal]

        # --- 偵測：只用**正常**樣本擬合基準線 ---------------------------------
        normalizer = Normalizer.fit(train_normal)
        for w in test:
            deviation = normalizer.deviation(w)
            strength_score[w.uid] = Normalizer.strength(deviation)
            rms_score[w.uid] = abs(deviation["rms"])

        if use_sklearn:
            from sklearn.neighbors import LocalOutlierFactor

            lof = LocalOutlierFactor(n_neighbors=20, novelty=True)
            lof.fit([[normalizer.deviation(w)[n] for n in FEATURES] for w in train_normal])
            raw = lof.score_samples([[normalizer.deviation(w)[n] for n in FEATURES] for w in test])
            for w, s in zip(test, raw):
                lof_score[w.uid] = -float(s)

        labels = [0 if w.is_normal else 1 for w in test]
        for name, table in (
            ("fingerprint_strength", strength_score),
            ("rms_threshold", rms_score),
            *(( ("local_outlier_factor", lof_score),) if use_sklearn else ()),
        ):
            fold_auc.setdefault(name, []).append(roc_auc(labels, [table[w.uid] for w in test]))

        # --- 歸因：三個故障類別 -------------------------------------------------
        models: dict[str, object] = {
            "fingerprint_cosine": FingerprintModel(n_prototypes=n_prototypes).fit(train_fault),
            "fingerprint_cosine_2_prototypes": FingerprintModel(n_prototypes=2).fit(train_fault),
            "single_feature_rule": SingleFeatureRule().fit(train),
        }
        test_fault = [w for w in test if not w.is_normal]
        majority = max(FAULT_CLASSES, key=lambda c: sum(1 for w in train_fault if w.fault_class == c))
        for w in test_fault:
            rankings.setdefault("majority", {})[w.uid] = [
                majority,
                *[c for c in FAULT_CLASSES if c != majority],
            ]
        for name, model in models.items():
            bucket = rankings.setdefault(name, {})
            for w in test_fault:
                bucket[w.uid] = model.rank(w)  # type: ignore[union-attr]

        if use_sklearn:
            from sklearn.linear_model import LogisticRegression

            lr_norm = Normalizer.fit(train)
            clf = LogisticRegression(
                class_weight="balanced", max_iter=2000, random_state=seed
            ).fit(
                [[lr_norm.deviation(w)[n] for n in FEATURES] for w in train_fault],
                [w.fault_class for w in train_fault],
            )
            proba = clf.predict_proba([[lr_norm.deviation(w)[n] for n in FEATURES] for w in test_fault])
            classes = list(clf.classes_)
            bucket = rankings.setdefault("logistic_regression", {})
            for w, row in zip(test_fault, proba):
                bucket[w.uid] = [c for _, c in sorted(zip(row, classes), key=lambda kv: -kv[0])]

    # ---------------------------------------------------------------- 彙總
    labels_all = [0 if w.is_normal else 1 for w in windows]
    detection = []
    for name, table in (
        ("fingerprint_strength", strength_score),
        ("rms_threshold", rms_score),
        *((("local_outlier_factor", lof_score),) if use_sklearn else ()),
    ):
        scores = [table[w.uid] for w in windows]
        detection.append(
            DetectionResult(
                method=name,
                auc=roc_auc(labels_all, scores),
                pauc=partial_auc(labels_all, scores, 0.1),
                fold_auc=fold_auc.get(name, []),
            )
        )

    faults = [w for w in windows if not w.is_normal]
    truths = [(w.fault_class,) for w in faults]
    method_order = [
        "fingerprint_cosine",
        "fingerprint_cosine_2_prototypes",
        "single_feature_rule",
        "logistic_regression",
        "majority",
    ]
    attribution = []
    for name in method_order:
        bucket = rankings.get(name)
        if not bucket:
            continue
        ranked = [bucket[w.uid] for w in faults]
        report = classification_report(
            [w.fault_class for w in faults], [bucket[w.uid][0] for w in faults], FAULT_CLASSES
        )
        fold_scores = []
        for load in LOADS:
            pool = [w for w in faults if w.load_hp == load]
            if pool:
                fold_scores.append(
                    classification_report(
                        [w.fault_class for w in pool],
                        [bucket[w.uid][0] for w in pool],
                        FAULT_CLASSES,
                    ).macro_f1
                )
        attribution.append(
            AttributionResult(
                method=name,
                top1=top_k_hit_rate(ranked, truths, 1),
                top2=top_k_hit_rate(ranked, truths, 2),
                report=report,
                fold_macro_f1=fold_scores,
            )
        )

    # 依凹坑尺寸拆解：人工凹坑越大訊號越乾淨，這張表就是「越嚴重越好認」的直接證據，
    # 也是「不能拿這些數字承諾早期偵測」的直接理由。
    severity: list[dict[str, object]] = []
    primary_bucket = rankings.get("fingerprint_cosine", {})
    for size in sorted({w.fault_size_in for w in faults}):
        pool = [w for w in faults if w.fault_size_in == size]
        detect_pool = [w for w in windows if w.is_normal or w.fault_size_in == size]
        severity.append(
            {
                "fault_size_in": size,
                "windows": len(pool),
                "top1": top_k_hit_rate(
                    [primary_bucket[w.uid] for w in pool], [(w.fault_class,) for w in pool], 1
                ),
                "detection_auc": roc_auc(
                    [0 if w.is_normal else 1 for w in detect_pool],
                    [strength_score[w.uid] for w in detect_pool],
                ),
            }
        )

    # 跨嚴重度轉移。這是本模組裡**唯一一個不容易的設定**，也是最貼近專案主張的那個：
    # 專案賣的是「早期發現」，也就是「拿成熟故障累積的知識去認剛開始壞的訊號」。
    # 同尺寸內的 leave-one-load-out 幾乎全對（見上表），那個數字不足以支持早期偵測的宣稱。
    severity_transfer = _severity_transfer(windows, n_prototypes, seed, use_sklearn)

    if use_sklearn:
        import sklearn

        notes.append(
            f"scikit-learn {sklearn.__version__} 已安裝，"
            "LocalOutlierFactor 與 LogisticRegression 對照組已執行。"
        )
    else:
        notes.append(
            "scikit-learn 未安裝，LocalOutlierFactor 與 LogisticRegression 對照組**未執行**。"
            "指紋法的相對優勢因此只對上單一特徵規則與多數決，證據強度較弱。"
        )
    notes.append(
        f"歸因候選集 {len(FAULT_CLASSES)} 類，隨機參考值 Top-1 "
        f"{random_top_k(len(FAULT_CLASSES), 1):.3f}、Top-2 {random_top_k(len(FAULT_CLASSES), 2):.3f}。"
    )
    notes.append(
        f"正常基準檔為 {NORMAL_SAMPLE_RATE_HZ} Hz，已降採樣 {NORMAL_DECIMATION} 倍對齊到 "
        f"{SAMPLE_RATE_HZ} Hz 再抽特徵；不做這件事的話「正常 vs 故障」會被取樣率本身分開。"
    )
    notes.append(REALITY_NOTICE)

    dataset = {
        "name": DATASET_NAME,
        "url": DATASET_URL,
        "license": DATASET_LICENSE,
        "citation": DATASET_CITATION,
        "is_synthetic": DATASET_IS_SYNTHETIC,
        "recordings": len(RECORDINGS),
        "windows": len(windows),
        "normal_windows": sum(1 for w in windows if w.is_normal),
        "fault_windows": len(faults),
        "by_class": by_class,
    }
    config = {
        "window": WINDOW,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "features": list(FEATURES),
        "split": "leave-one-load-out",
        "loads": list(LOADS),
        "n_prototypes": n_prototypes,
        "seed": seed,
        "sklearn": use_sklearn,
    }
    return CwruReport(
        dataset=dataset,
        config=config,
        detection=detection,
        attribution=attribution,
        severity=severity,
        severity_transfer=severity_transfer,
        notes=notes,
    )


def _severity_transfer(
    windows: list[Window], n_prototypes: int, seed: int, use_sklearn: bool
) -> list[dict[str, object]]:
    """訓練用一種凹坑尺寸，測試用另一種。

    為什麼這一節比上面那些表更重要：0.021″ 是「壞得很明顯」，0.007″ 是「剛開始壞」。
    專案主打的是**早期發現**，所以「拿嚴重故障的知識去認早期訊號」才是要量的東西。
    同尺寸內的 leave-one-load-out 幾乎全對，那個數字證明不了早期偵測。
    正規化基準線一律只用**正常樣本**估（與主實驗一致），確保基準線不含測試側的故障資訊。
    """
    sizes = sorted({w.fault_size_in for w in windows if not w.is_normal})
    normals = [w for w in windows if w.is_normal]
    rows: list[dict[str, object]] = []
    for train_size in sizes:
        for test_size in sizes:
            if train_size == test_size:
                continue
            train_fault = [w for w in windows if w.fault_size_in == train_size and not w.is_normal]
            test_fault = [w for w in windows if w.fault_size_in == test_size and not w.is_normal]
            if not train_fault or not test_fault:
                continue
            truths = [(w.fault_class,) for w in test_fault]

            fp = FingerprintModel(n_prototypes=n_prototypes).fit(normals + train_fault)
            rule = SingleFeatureRule().fit(normals + train_fault)
            row: dict[str, object] = {
                "train_size_in": train_size,
                "test_size_in": test_size,
                "windows": len(test_fault),
                "fingerprint_top1": top_k_hit_rate([fp.rank(w) for w in test_fault], truths, 1),
                "single_feature_top1": top_k_hit_rate([rule.rank(w) for w in test_fault], truths, 1),
                "single_feature_used": rule.feature,
            }
            if use_sklearn:
                from sklearn.linear_model import LogisticRegression

                norm = Normalizer.fit(normals)
                clf = LogisticRegression(
                    class_weight="balanced", max_iter=2000, random_state=seed
                ).fit(
                    [[norm.deviation(w)[n] for n in FEATURES] for w in train_fault],
                    [w.fault_class for w in train_fault],
                )
                proba = clf.predict_proba(
                    [[norm.deviation(w)[n] for n in FEATURES] for w in test_fault]
                )
                classes = list(clf.classes_)
                ranked = [
                    [c for _, c in sorted(zip(r, classes), key=lambda kv: -kv[0])] for r in proba
                ]
                row["logistic_regression_top1"] = top_k_hit_rate(ranked, truths, 1)
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m factory_guardian.validation.cwru",
        description="CWRU 軸承振動資料集外部驗證：指紋餘弦法 vs baseline",
    )
    parser.add_argument("--data", type=Path, default=None, help=f".mat 目錄（預設 {DATASET_DIR}）")
    parser.add_argument("--download", action="store_true", help="下載缺少的 .mat 檔")
    parser.add_argument("--prototypes", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260809)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)

    root = args.data or DATASET_DIR
    if args.download:
        fetched = download(root)
        print(f"[download] 取得 {len(fetched)} 個檔案 → {root}")

    if not dataset_available(root):
        print(f"[skip] CWRU 資料不完整，缺少：{missing_files(root)}")
        print(f"       下載位置：{DATASET_URL}")
        print("       取得指令：python -m factory_guardian.validation.cwru --download")
        return 2

    report = run_validation(root, n_prototypes=args.prototypes, seed=args.seed)
    text = report.to_markdown()
    print(text)
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    if args.markdown:
        args.markdown.write_text(text, "utf-8")
    return 0


__all__ = [
    "BANDS",
    "DATASET_CITATION",
    "DATASET_DIR",
    "DATASET_IS_SYNTHETIC",
    "DATASET_LICENSE",
    "DATASET_NAME",
    "DATASET_SOURCE",
    "DATASET_URL",
    "FAULT_CLASSES",
    "FEATURES",
    "FEATURE_MAPPINGS",
    "NORMAL_DECIMATION",
    "NORMAL_SAMPLE_RATE_HZ",
    "NO_FAULT_LABEL",
    "RECORDINGS",
    "REALITY_NOTICE",
    "SAMPLE_RATE_HZ",
    "WINDOW",
    "AttributionResult",
    "CwruReport",
    "DetectionResult",
    "FingerprintModel",
    "Normalizer",
    "Recording",
    "SingleFeatureRule",
    "Window",
    "dataset_available",
    "download",
    "extract_features",
    "load_windows",
    "main",
    "mapping_table",
    "missing_files",
    "run_validation",
]


if __name__ == "__main__":  # pragma: no cover - 手動執行入口
    import sys

    sys.exit(main(sys.argv[1:]))
