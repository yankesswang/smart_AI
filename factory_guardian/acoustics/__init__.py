"""機器聲音模態（Acoustic Modality）。

競賽研究文件 §4.4 把「同步使用感測訊號、**機器聲音**、現場影像、SOP 與工單歷史」
列為第一條差異化。這個套件就是那句話裡的「機器聲音」。

四個檔案，兩條互不相通的路徑：

| 檔案 | 角色 | 資料 |
|---|---|---|
| `features.py` | log-mel + 時間統計摘要（**唯一**的特徵定義） | 兩條路徑共用 |
| `detector.py` | 只用正常音訊擬合的無監督異常偵測器 | 兩條路徑共用 |
| `dcase.py` | DCASE2020 / MIMII **真實泵浦錄音**的外部驗證 | 真實，只驗證偵測器 |
| `signatures.py` + `synthetic.py` | Digital Twin 的**合成**聲學觀測 | 合成，只跑 Demo |

**誠實邊界**：Demo 閉環裡的每一個聲學數字都是合成的（由既有振動物理推導），
真實錄音只用來證明偵測器本身有效，兩者沒有任何一條資料流互通 ——
`dcase.py` 不被 `twin/`、`agents/`、`api/` 的任何一處匯入。詳見 `synthetic.py`
的模組說明與 `docs/acoustic_validation.md`。

`features` / `detector` / `dcase` 需要 numpy / librosa / scikit-learn，
所以採**延遲匯入**：Agent 閉環、CLI 與 Dashboard 不載入它們，
`pytest` 也不會為了三個聲學測試付出 import 成本。
"""

from __future__ import annotations

from typing import Any

from .signatures import (
    ACOUSTIC_RESPONSE,
    INDICATOR_NAMES,
    INDICATOR_SCALES,
    INDICATOR_UNITS,
    NOMINAL_INDICATORS,
    acoustic_signatures,
)
from .synthetic import synthesize_waveform, target_indicators

_LAZY: dict[str, str] = {
    "AcousticAnomalyDetector": "detector",
    "auc_scores": "detector",
    "MelConfig": "features",
    "DEFAULT_CONFIG": "features",
    "log_mel_spectrogram": "features",
    "log_mel_summary": "features",
    "indicators_from_waveform": "features",
}


def __getattr__(name: str) -> Any:
    """延遲載入 numpy / librosa / scikit-learn 相依的部分。"""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module_name}", __name__), name)


__all__ = [
    "ACOUSTIC_RESPONSE",
    "DEFAULT_CONFIG",
    "INDICATOR_NAMES",
    "INDICATOR_SCALES",
    "INDICATOR_UNITS",
    "NOMINAL_INDICATORS",
    "AcousticAnomalyDetector",
    "MelConfig",
    "acoustic_signatures",
    "auc_scores",
    "indicators_from_waveform",
    "log_mel_spectrogram",
    "log_mel_summary",
    "synthesize_waveform",
    "target_indicators",
]
