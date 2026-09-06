"""外部資料驗證層 —— 用第三方公開資料集驗證本專案的診斷方法本身有效。

`agents/diagnosis.py` 的感測器指紋餘弦法（權重 0.75）是整個系統的診斷核心，
但它的指紋、訊號與評估標籤全部來自本專案自己的 Digital Twin。
本套件把**同一套方法**搬到 UCI AI4I 2020 Predictive Maintenance Dataset 上重跑，
並與門檻規則、LogisticRegression、RandomForest 及下限 baseline 比較。

```bash
python3 -m factory_guardian.validation --json runs/ai4i_validation.json                    # 單原型
python3 -m factory_guardian.validation --prototypes 2 --json runs/ai4i_validation_p2.json  # 多原型
```

**誠實邊界**：AI4I 2020 是**合成**資料集（作者以物理規則模擬產生的公開 benchmark），
不是真實工廠量測，而且**不含振動訊號**。詳見 `ai4i.py` 模組 docstring 與
`docs/factory_guardian/external_validation.md`。本套件不被 `twin/`、`agents/`、`api/`、`cli.py` 匯入。

## 第二條驗證線：CWRU 軸承振動

AI4I 沒有振動訊號，`bearing_degradation`（Demo 主線情境）因此完全沒被覆蓋。
那個缺口由 `cwru.py` 補上（真實加速規量測，內圈／滾珠／外圈三類）：

```bash
python3 -m factory_guardian.validation.cwru --download
python3 -m factory_guardian.validation.cwru --json runs/cwru_validation.json
```

`cwru` **刻意不在這個 `__init__` 裡 re-export**：它與 `ai4i` 有一堆同名的東西
（`FingerprintModel` / `Normalizer` / `dataset_available` / `run_validation`），
攤平到同一個命名空間只會讓「這個數字是哪一份資料集算的」變得可以搞錯。
用 `from factory_guardian.validation import cwru` 明確指名。
它另外需要 `scipy`（讀 `.mat`、抗混疊降採樣），也不該讓 AI4I 那條線被這個相依綁住。
誠實邊界見 `cwru.py` 模組 docstring 與 `docs/factory_guardian/cwru_validation.md`。
"""

from .ai4i import (
    CHANNEL_MAPPINGS,
    DATASET_CITATION,
    DATASET_CSV,
    DATASET_IS_SYNTHETIC,
    DATASET_LICENSE,
    DATASET_NAME,
    DATASET_URL,
    DERIVED_MAPPINGS,
    DIAGNOSABLE_MODES,
    FAULT_MODES,
    MISSING_SIGNALS,
    NO_FAULT_LABEL,
    STRICT_CHANNELS,
    SYNTHETIC_NOTICE,
    ChannelMapping,
    FaultModeMapping,
    Sample,
    dataset_available,
    dataset_summary,
    load_samples,
    mapping_table,
)
from .baselines import (
    MajorityBaseline,
    StratifiedRandomBaseline,
    ThresholdRuleBaseline,
    sklearn_available,
    sklearn_baselines,
)
from .fingerprint import W_DOCS, W_PRIOR, W_SIGNATURE, FingerprintModel, Normalizer, cosine
from .metrics import ClassificationReport, classification_report, random_top_k, top_k_hit_rate
from .runner import ValidationReport, run_validation, stratified_folds

__all__ = [
    "CHANNEL_MAPPINGS",
    "DATASET_CITATION",
    "DATASET_CSV",
    "DATASET_IS_SYNTHETIC",
    "DATASET_LICENSE",
    "DATASET_NAME",
    "DATASET_URL",
    "DERIVED_MAPPINGS",
    "DIAGNOSABLE_MODES",
    "FAULT_MODES",
    "MISSING_SIGNALS",
    "NO_FAULT_LABEL",
    "STRICT_CHANNELS",
    "SYNTHETIC_NOTICE",
    "W_DOCS",
    "W_PRIOR",
    "W_SIGNATURE",
    "ChannelMapping",
    "ClassificationReport",
    "FaultModeMapping",
    "FingerprintModel",
    "MajorityBaseline",
    "Normalizer",
    "Sample",
    "StratifiedRandomBaseline",
    "ThresholdRuleBaseline",
    "ValidationReport",
    "classification_report",
    "cosine",
    "dataset_available",
    "dataset_summary",
    "load_samples",
    "mapping_table",
    "random_top_k",
    "run_validation",
    "sklearn_available",
    "sklearn_baselines",
    "stratified_folds",
    "top_k_hit_rate",
]
