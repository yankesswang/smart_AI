"""DCASE2020 Challenge Task 2（MIMII pump）開發集轉接器與外部驗證。

## 這份資料集在專案裡的角色

Factory Guardian 的 Digital Twin 是**合成**的，這是提案最容易被質疑的一點
（提案書 §14 自己就把「數字是不是編的」列為第一個預期追問）。
最直接的回答不是解釋，是拿一份**外部的、公開的、有官方基準的真實工業資料**
把偵測器再跑一次，並且和官方 baseline 比。

這份資料的角色僅止於此 —— 它**驗證偵測器**，不參與 Demo 閉環，
也不會有任何一個 Demo 上的數字來自這裡。邊界寫在 `docs/factory_guardian/acoustic_validation.md`，
程式上的保證則是：本模組不被 `twin/`、`agents/`、`api/` 任何一處匯入。

## 資料集

* 名稱：DCASE2020 Challenge Task 2 development dataset（pump 部分），衍生自
  MIMII Dataset（Purohit et al., 2019），內容是**真實工業泵浦的運轉錄音**。
* 內容：4 個泵浦個體（id 00 / 02 / 04 / 06），10 秒、16 kHz、單聲道。
  train 3,349 段全部是正常；test 400 段正常 + 456 段異常。
* 授權：**CC BY-NC-SA 4.0（姓名標示—非商業性—相同方式分享）**。
  非商業授權，處理方式與專案既有的 TabFM 預訓練權重一致：
  可用於競賽與研究驗證，正式商用部署需改用自有或具商用授權的錄音重跑同一套流程。
  程式碼不需要改動 —— 換的只是 `fit()` 吃進去的那批音訊。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from .detector import DEFAULT_N_NEIGHBORS, AcousticAnomalyDetector, auc_scores
from .features import DEFAULT_CONFIG, MelConfig, log_mel_summary

# 專案根目錄下的既定位置（`data/external/` 已在 .gitignore，1 GB 不進版控）。
DATASET_DIR = Path(__file__).resolve().parents[2] / "data" / "external" / "dcase2020_pump"
DATASET_ZIP = DATASET_DIR / "dev_data_pump.zip"
FEATURE_CACHE = DATASET_DIR / "pump_logmel_summary_v2.npz"

MACHINE_IDS: tuple[str, ...] = ("00", "02", "04", "06")
PAUC_P: float = 0.1

DATASET_NAME = "DCASE2020 Task2 development dataset — pump (derived from MIMII Dataset)"
DATASET_LICENSE = "CC BY-NC-SA 4.0（非商業性使用）"
DATASET_URL = "https://zenodo.org/record/3678171"
DATASET_CITATION = (
    "Y. Koizumi et al., “Description and Discussion on DCASE2020 Challenge Task2: "
    "Unsupervised Anomalous Sound Detection for Machine Condition Monitoring,” DCASE2020 Workshop; "
    "H. Purohit et al., “MIMII Dataset: Sound Dataset for Malfunctioning Industrial Machine "
    "Investigation and Inspection,” DCASE2019 Workshop."
)

# 官方 baseline（autoencoder）在同一份 development set 上的公開數字。
# 來源：DCASE2020 Task2 官方 baseline system（y-kawagu/dcase2020_task2_baseline）附的結果表。
# 挑戰網站公布的 pump 平均為 AUC 72.89% / pAUC 59.99%，與下表的 72.59% / 60.00%
# 差在 baseline 訓練的隨機性，量級一致。
OFFICIAL_BASELINE: dict[str, dict[str, float]] = {
    "00": {"auc": 0.670769, "pauc": 0.572690},
    "02": {"auc": 0.609369, "pauc": 0.580370},
    "04": {"auc": 0.888600, "pauc": 0.676842},
    "06": {"auc": 0.734902, "pauc": 0.570175},
    "average": {"auc": 0.725910, "pauc": 0.600019},
}
OFFICIAL_BASELINE_SOURCE = (
    "DCASE2020 Task2 official baseline system (autoencoder), development set；"
    "https://github.com/y-kawagu/dcase2020_task2_baseline"
)

_NAME_RE = re.compile(r"pump/(train|test)/(normal|anomaly)_id_(\d+)_(\d+)\.wav$")


# --------------------------------------------------------------------------------------
# 可用性
# --------------------------------------------------------------------------------------
def dataset_available() -> bool:
    """資料集在不在。**測試一律先問這個再決定要不要 skip** —— 1 GB 的檔案不進版控，
    CI 上不會有，所以它的存在與否絕不能決定測試套件會不會壞。"""
    return DATASET_ZIP.is_file()


def cache_available() -> bool:
    return FEATURE_CACHE.is_file()


def dataset_info() -> dict[str, Any]:
    return {
        "name": DATASET_NAME,
        "license": DATASET_LICENSE,
        "url": DATASET_URL,
        "citation": DATASET_CITATION,
        "machine_ids": list(MACHINE_IDS),
        "zip": str(DATASET_ZIP),
        "zip_available": dataset_available(),
        "feature_cache": str(FEATURE_CACHE),
        "cache_available": cache_available(),
        "role": "external validation of the detector only — never used in the demo loop",
    }


# --------------------------------------------------------------------------------------
# 讀檔
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Clip:
    name: str
    split: str          # train / test
    label: int          # 0 = normal, 1 = anomaly
    machine_id: str


def iter_clips(zip_path: Path | None = None) -> Iterator[Clip]:
    path = Path(zip_path or DATASET_ZIP)
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            match = _NAME_RE.match(name)
            if match is None:
                continue
            split, label, machine_id, _ = match.groups()
            yield Clip(name=name, split=split, label=1 if label == "anomaly" else 0,
                       machine_id=machine_id)


def read_waveform(archive: zipfile.ZipFile, name: str) -> tuple[np.ndarray, int]:
    import soundfile as sf

    data, sample_rate = sf.read(io.BytesIO(archive.read(name)), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, int(sample_rate)


# --------------------------------------------------------------------------------------
# 特徵快取
# --------------------------------------------------------------------------------------
def build_feature_cache(
    zip_path: Path | None = None,
    cache_path: Path | None = None,
    config: MelConfig = DEFAULT_CONFIG,
    progress: bool = False,
) -> Path:
    """解碼整份資料集一次，把 448 維特徵向量存成 npz。

    這是整條流程唯一昂貴的一步（約 1–2 分鐘）。存下來的是特徵而不是模型物件：
    npz 沒有版本相依，任何人都可以拿它自己重算一次 AUC 來稽核我們報的數字。
    """
    zip_file = Path(zip_path or DATASET_ZIP)
    out = Path(cache_path or FEATURE_CACHE)
    if not zip_file.is_file():
        raise FileNotFoundError(f"找不到資料集 {zip_file}（見 docs/factory_guardian/acoustic_validation.md 的下載說明）")

    clips = list(iter_clips(zip_file))
    features = np.empty((len(clips), config.feature_dim), dtype=np.float32)
    with zipfile.ZipFile(zip_file) as archive:
        for idx, clip in enumerate(clips):
            waveform, sample_rate = read_waveform(archive, clip.name)
            if sample_rate != config.sample_rate:
                raise ValueError(f"{clip.name} 取樣率 {sample_rate} != {config.sample_rate}")
            features[idx] = log_mel_summary(waveform, config)
            if progress and (idx + 1) % 500 == 0:
                print(f"  {idx + 1}/{len(clips)}", flush=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        out,
        features=features,
        split=np.array([c.split for c in clips]),
        label=np.array([c.label for c in clips], dtype=np.int8),
        machine_id=np.array([c.machine_id for c in clips]),
        clip=np.array([c.name for c in clips]),
        config=np.array([repr(config.to_dict())]),
    )
    return out


def load_feature_cache(cache_path: Path | None = None) -> dict[str, np.ndarray]:
    path = Path(cache_path or FEATURE_CACHE)
    if not path.is_file():
        raise FileNotFoundError(f"特徵快取不存在：{path}；先執行 build_feature_cache()")
    with np.load(path, allow_pickle=False) as data:
        return {k: data[k] for k in ("features", "split", "label", "machine_id")}


def ensure_feature_cache(**kwargs: Any) -> dict[str, np.ndarray]:
    if not cache_available():
        build_feature_cache(**kwargs)
    return load_feature_cache()


# --------------------------------------------------------------------------------------
# 評估
# --------------------------------------------------------------------------------------
def evaluate(
    cache: dict[str, np.ndarray] | None = None,
    n_neighbors: int = DEFAULT_N_NEIGHBORS,
    p: float = PAUC_P,
) -> dict[str, Any]:
    """在 DCASE test split 上分 machine id 評估，回傳 AUC / pAUC。

    **每個 machine id 各自擬合一個模型**，只用該 id 的正常音訊。這是 DCASE 的規定，
    也是真實工廠的做法：不同個體的基準聲音本來就不同，拿 A 泵浦的「正常」
    去判 B 泵浦，量到的是個體差異而不是異常。
    """
    data = cache or load_feature_cache()
    features, split, label, machine_id = (
        data["features"], data["split"], data["label"], data["machine_id"]
    )

    per_machine: dict[str, dict[str, float]] = {}
    for mid in MACHINE_IDS:
        train_mask = (machine_id == mid) & (split == "train")
        test_mask = (machine_id == mid) & (split == "test")
        if not train_mask.any() or not test_mask.any():
            continue
        # 訓練集必須全是正常音訊 —— 這是「無監督」這三個字的具體意思。
        assert int(label[train_mask].sum()) == 0, "訓練集出現了異常標籤"
        detector = AcousticAnomalyDetector(n_neighbors=n_neighbors).fit(features[train_mask])
        scores = detector.score(features[test_mask])
        auc, pauc = auc_scores(label[test_mask], scores, p=p)
        per_machine[mid] = {
            "auc": auc,
            "pauc": pauc,
            "train_clips": int(train_mask.sum()),
            "test_normal": int((label[test_mask] == 0).sum()),
            "test_anomaly": int((label[test_mask] == 1).sum()),
        }

    aucs = [v["auc"] for v in per_machine.values()]
    paucs = [v["pauc"] for v in per_machine.values()]
    return {
        "per_machine": per_machine,
        "average": {"auc": float(np.mean(aucs)), "pauc": float(np.mean(paucs))},
        "official_baseline": OFFICIAL_BASELINE,
        "official_baseline_source": OFFICIAL_BASELINE_SOURCE,
        "n_neighbors": n_neighbors,
        "pauc_p": p,
        "dataset": dataset_info(),
    }


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"{'machine id':<12}{'AUC':>9}{'pAUC':>9}   {'baseline AUC':>13}{'baseline pAUC':>15}",
        "-" * 60,
    ]
    for mid, row in result["per_machine"].items():
        base = result["official_baseline"].get(mid, {})
        lines.append(
            f"id {mid:<9}{row['auc']:>9.4f}{row['pauc']:>9.4f}   "
            f"{base.get('auc', float('nan')):>13.4f}{base.get('pauc', float('nan')):>15.4f}"
        )
    avg, base_avg = result["average"], result["official_baseline"]["average"]
    lines += [
        "-" * 60,
        f"{'AVERAGE':<12}{avg['auc']:>9.4f}{avg['pauc']:>9.4f}   "
        f"{base_avg['auc']:>13.4f}{base_avg['pauc']:>15.4f}",
    ]
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - 手動執行的驗證入口
    # BLAS 多執行緒在小矩陣上是淨損失（本機實測慢 100 倍以上），這裡只跑一次所以直接壓成單執行緒。
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    if not dataset_available():
        print(f"找不到資料集：{DATASET_ZIP}")
        return 1
    if not cache_available():
        print("建立特徵快取（第一次會跑 1–2 分鐘）…")
        build_feature_cache(progress=True)
    print(format_report(evaluate()))
    return 0


__all__ = [
    "DATASET_CITATION",
    "DATASET_DIR",
    "DATASET_LICENSE",
    "DATASET_NAME",
    "DATASET_URL",
    "DATASET_ZIP",
    "FEATURE_CACHE",
    "MACHINE_IDS",
    "OFFICIAL_BASELINE",
    "OFFICIAL_BASELINE_SOURCE",
    "PAUC_P",
    "Clip",
    "build_feature_cache",
    "cache_available",
    "dataset_available",
    "dataset_info",
    "ensure_feature_cache",
    "evaluate",
    "format_report",
    "iter_clips",
    "load_feature_cache",
    "read_waveform",
]
