"""外部驗證執行器：把指紋餘弦法與各對照組跑在 AI4I 2020 上。

## 評估設計

**切分**：5-fold **分層**交叉驗證（依單標籤分層），每一筆樣本恰好被預測一次，
且預測它的模型從未看過它。指紋質心、先驗、正規化基準、門檻、分類器全部只在 train fold 上估。
不用單次 hold-out 的理由是樣本數：TWF 只有 46 筆，30% 測試集只剩 14 筆，
per-class recall 的信賴區間會寬到不能引用。交叉驗證讓每個模式都有全部樣本的 out-of-fold 預測。

**類別不平衡**：故障佔 3.39%，TWF 佔 0.46%。處理方式：

* 分層切分，確保每個 fold 都有各模式樣本。
* 主指標用 **macro-F1**（每個模式等權），accuracy 只列出來當提醒。
* LR / RF 一律 `class_weight="balanced"`；指紋質心本身對先驗不敏感（它比的是方向）。
* 不做過採樣／SMOTE —— 那會讓 test fold 混進合成樣本的近鄰，數字會虛高。

## 兩個任務

1. **根因歸因（主）**：只看故障樣本，候選集＝4 個可診斷模式，**不含** `no_equipment_fault`。
   這對應本專案的實際流程 —— Monitoring Agent 先偵測、Diagnosis Agent 才啟動，
   診斷器被呼叫時「有異常」已經是前提，它要回答的是「是哪一種」。
   把 no-fault 留在候選裡會讓這個任務同時考偵測與歸因，兩件事混在一個數字裡就沒法解讀。
   為了公平，**所有方法**在這個任務都同樣把 no-fault 從排序中移除（不是只優待指紋法）。
   指標：Top-1 / Top-3 命中率、每模式 precision / recall、混淆矩陣。
2. **端到端（次）**：正常樣本一起放進去，候選集多一個 `no_equipment_fault`，量的是偵測＋歸因。
   這裡才看得到誤報代價。

## 學習曲線

指紋法每個故障只有一個原型（自由度極低），本來就不該期望它贏過有監督分類器。
它的設計理由是**冷啟動**：新產線沒有標註故障歷史，手冊卻已經寫好徵兆。
所以額外跑一條學習曲線 —— 限制每個模式可用的標註故障筆數（3/5/10/20/40/全部），
看指紋法與 LogisticRegression 各自的表現。這條曲線才是「為什麼不直接用分類器」的實證答案。

## RNF：不可診斷案例

RNF 依定義與感測器無關。正確行為不是「猜對」，而是**不給高信心**。
所以 RNF 不進訓練，也不算進上面兩個任務，單獨報「拒答率」與「平均最高信心」。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import ai4i
from .ai4i import (
    DIAGNOSABLE_MODES,
    NO_FAULT_LABEL,
    STRICT_CHANNELS,
    Sample,
)
from .baselines import (
    MajorityBaseline,
    StratifiedRandomBaseline,
    ThresholdRuleBaseline,
    sklearn_available,
    sklearn_baselines,
)
from .fingerprint import FingerprintModel
from .metrics import ClassificationReport, classification_report, random_top_k, top_k_hit_rate

DEFAULT_FOLDS = 5
DEFAULT_SEED = 20260809
#: 每個故障模式的指紋原型數。預設 1 —— 文件裡既有的數字全部綁在單一原型上。
#: `agents/diagnosis.py` 已經採納多原型（來源是手冊語意），本模組要量它時用 `--prototypes 2`。
DEFAULT_PROTOTYPES = 1

#: 歸因任務的候選集合：只有 4 個可診斷模式（no-fault 在這個任務被移除，見模組 docstring）。
ATTRIBUTION_LABELS: tuple[str, ...] = DIAGNOSABLE_MODES
#: 端到端任務的候選集合：多一個「無故障徵兆」。
END_TO_END_LABELS: tuple[str, ...] = (*DIAGNOSABLE_MODES, NO_FAULT_LABEL)
#: 學習曲線的取樣點：每個故障模式可用的標註筆數。`None` = 全部。
#: 從 1 開始是刻意的 —— 「手冊上只寫了一個案例」是新產線的真實起點。
LEARNING_CURVE_BUDGETS: tuple[int | None, ...] = (1, 2, 3, 5, 10, 20, 40, None)


# --------------------------------------------------------------------------- 切分
def stratified_folds(samples: list[Sample], n_folds: int, seed: int) -> list[int]:
    """分層 k-fold 的 fold 編號。純標準庫實作，確定性。

    分層鍵用單標籤（多模式樣本用 ``+`` 串接的組合標籤），確保 24 筆多模式樣本
    也被平均分散，不會整批落在同一個 fold 裡讓那個 fold 的數字爆掉。
    """
    import random

    rng = random.Random(seed)
    buckets: dict[str, list[int]] = {}
    for idx, s in enumerate(samples):
        buckets.setdefault(s.label, []).append(idx)

    assignment = [0] * len(samples)
    for label in sorted(buckets):
        idxs = buckets[label][:]
        rng.shuffle(idxs)
        for position, idx in enumerate(idxs):
            assignment[idx] = position % n_folds
    return assignment


# --------------------------------------------------------------------------- 結果
@dataclass
class TaskResult:
    """單一方法在單一任務上的成績。"""

    method: str
    task: str
    top1: float
    top3: float
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
            "task": self.task,
            "top1": round(self.top1, 4),
            "top3": round(self.top3, 4),
            "macro_f1_mean_over_folds": round(self.macro_f1_mean, 4),
            "macro_f1_std_over_folds": round(self.macro_f1_std, 4),
            "pooled": self.report.to_dict(),
        }


@dataclass
class RnfDiagnostics:
    """RNF（不可診斷）樣本上的行為。"""

    n: int
    refusal_rate: float
    """Top-1 給出 `no_equipment_fault` 的比例 —— 這是**正確**行為。"""

    mean_top_confidence: float
    mean_fault_confidence: float
    """給故障候選的最高信心平均值。與正常樣本比較才有意義。"""

    normal_mean_fault_confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "refusal_rate": round(self.refusal_rate, 4),
            "mean_top_confidence": round(self.mean_top_confidence, 4),
            "mean_fault_confidence": round(self.mean_fault_confidence, 4),
            "normal_mean_fault_confidence": round(self.normal_mean_fault_confidence, 4),
        }


@dataclass
class GateDiagnostics:
    """no-fault gate 的行為。拆成「名目」與「實際」兩個數字，因為兩者差很多。

    `raw_flag_rate` 只看訊號強度是否超過校準門檻（設計上應該 ≈ 1−`NO_FAULT_QUANTILE`）。
    `effective_fault_rate` 是最終 Top-1 真的落在某個故障上的比例 —— 這個高得多，
    因為 diagnosis.py 的規則是「no-fault 信心要**贏過**最佳故障候選」才會插到最前面，
    而故障候選的 softmax 信心至少有 1/K。這個落差是方法的真實性質，不是 bug，
    但不寫出來的話沒有人看得出誤報是從哪裡來的。
    """

    normal_rows: int
    raw_flag_rate: float
    effective_fault_rate: float
    fault_rows: int
    fault_raw_pass_rate: float

    def to_dict(self) -> dict[str, object]:
        return {
            "normal_rows": self.normal_rows,
            "raw_flag_rate": round(self.raw_flag_rate, 4),
            "effective_fault_rate": round(self.effective_fault_rate, 4),
            "fault_rows": self.fault_rows,
            "fault_raw_pass_rate": round(self.fault_raw_pass_rate, 4),
        }


@dataclass
class LearningCurvePoint:
    """每個故障模式只給 `budget` 筆標註時，各方法的歸因 Top-1。"""

    budget: int | None
    scores: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "labelled_per_mode": self.budget,
            "top1": {k: round(v, 4) for k, v in self.scores.items()},
        }


@dataclass
class ValidationReport:
    dataset: dict[str, object]
    config: dict[str, object]
    attribution: list[TaskResult]
    end_to_end: list[TaskResult]
    ablations: list[TaskResult]
    rnf: RnfDiagnostics | None
    gate: GateDiagnostics | None
    learning_curve: list[LearningCurvePoint]
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "config": self.config,
            "attribution": [r.to_dict() for r in self.attribution],
            "end_to_end": [r.to_dict() for r in self.end_to_end],
            "ablations": [r.to_dict() for r in self.ablations],
            "rnf": self.rnf.to_dict() if self.rnf else None,
            "gate": self.gate.to_dict() if self.gate else None,
            "learning_curve": [p.to_dict() for p in self.learning_curve],
            "notes": self.notes,
        }

    # ------------------------------------------------------------------ 報表
    def to_markdown(self) -> str:
        lines: list[str] = []
        ds = self.dataset
        lines.append("## 資料集")
        lines.append("")
        lines.append(f"- 名稱：{ai4i.DATASET_NAME}（{ai4i.DATASET_SOURCE}）")
        lines.append(f"- 授權：{ai4i.DATASET_LICENSE}")
        lines.append(f"- 筆數：{ds['rows']}；`Machine failure` {ds['machine_failure']} 筆")
        lines.append(f"- 各模式：{ds['by_mode']}")
        lines.append(f"- 多模式紀錄 {ds['multi_mode_rows']} 筆；RNF-only {ds['rnf_only_rows']} 筆")
        lines.append("")
        lines.append(f"> **{ai4i.SYNTHETIC_NOTICE}**")
        lines.append("")

        lines.append("## 任務一：根因歸因（僅故障樣本）")
        lines.append("")
        lines.append(_task_table(self.attribution))
        lines.append("")
        primary = next((r for r in self.attribution if r.method == "fingerprint_cosine"), None)
        if primary:
            lines.append("### 指紋餘弦法：每模式 precision / recall")
            lines.append("")
            lines.append(_per_class_table(primary.report))
            lines.append("")
            lines.append("### 指紋餘弦法：混淆矩陣（out-of-fold pooled）")
            lines.append("")
            lines.append(primary.report.matrix_markdown())
            lines.append("")

        lines.append("## 任務二：端到端（含正常樣本）")
        lines.append("")
        lines.append(_task_table(self.end_to_end))
        lines.append("")
        e2e = next((r for r in self.end_to_end if r.method == "fingerprint_cosine"), None)
        if e2e:
            lines.append("### 指紋餘弦法：每類 precision / recall")
            lines.append("")
            lines.append(_per_class_table(e2e.report))
            lines.append("")

        if self.ablations:
            lines.append("## Ablation")
            lines.append("")
            lines.append(_task_table(self.ablations))
            lines.append("")

        if self.gate:
            g = self.gate
            lines.append("## no-fault gate 行為")
            lines.append("")
            lines.append(
                f"- 正常樣本 {g.normal_rows} 筆：訊號強度超過校準門檻者 {g.raw_flag_rate:.1%}"
                f"（名目設計值 {1 - 0.99:.1%}），但最終 Top-1 落在故障上的有 "
                f"**{g.effective_fault_rate:.1%}**。"
            )
            lines.append(
                f"- 故障樣本 {g.fault_rows} 筆：訊號強度超過門檻者 {g.fault_raw_pass_rate:.1%}。"
            )
            lines.append("")

        if self.learning_curve:
            lines.append("## 學習曲線：每模式標註筆數 vs 歸因 Top-1")
            lines.append("")
            methods = sorted({m for p in self.learning_curve for m in p.scores})
            lines.append("| 每模式標註筆數 | " + " | ".join(methods) + " |")
            lines.append("|---|" + "---:|" * len(methods))
            for p in self.learning_curve:
                budget = "全部" if p.budget is None else str(p.budget)
                cells = [f"{p.scores.get(m, float('nan')):.3f}" for m in methods]
                lines.append(f"| {budget} | " + " | ".join(cells) + " |")
            lines.append("")

        if self.rnf:
            lines.append("## RNF：不可診斷案例")
            lines.append("")
            r = self.rnf
            lines.append(f"- 樣本數 {r.n}")
            lines.append(f"- 拒答率（Top-1 = `no_equipment_fault`）**{r.refusal_rate:.1%}**")
            lines.append(f"- 給故障候選的最高信心平均 {r.mean_fault_confidence:.3f}")
            lines.append(f"- 對照：正常樣本同一數值為 {r.normal_mean_fault_confidence:.3f}")
            lines.append("")

        if self.notes:
            lines.append("## 執行備註")
            lines.append("")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")
        return "\n".join(lines)


def _task_table(results: list[TaskResult]) -> str:
    head = "| 方法 | Top-1 | Top-3 | macro-P | macro-R | macro-F1 | fold macro-F1 (mean±sd) | accuracy |"
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|"
    lines = [head, sep]
    for r in results:
        lines.append(
            f"| {r.method} | {r.top1:.3f} | {r.top3:.3f} | "
            f"{r.report.macro_precision:.3f} | {r.report.macro_recall:.3f} | "
            f"{r.report.macro_f1:.3f} | {r.macro_f1_mean:.3f}±{r.macro_f1_std:.3f} | "
            f"{r.report.accuracy:.3f} |"
        )
    return "\n".join(lines)


def _per_class_table(report: ClassificationReport) -> str:
    lines = ["| 類別 | support | precision | recall | F1 |", "|---|---:|---:|---:|---:|"]
    for label in report.labels:
        m = report.per_class.get(label)
        if m is None:
            continue
        flag = " ⚠︎" if 0 < m.support < 30 else ""
        lines.append(
            f"| {label}{flag} | {m.support} | {m.precision:.3f} | {m.recall:.3f} | {m.f1:.3f} |"
        )
    lines.append("")
    lines.append("⚠︎ = support < 30，該列數字的不確定性大，不應單獨引用。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 執行
def _truth_set(sample: Sample) -> tuple[str, ...]:
    return sample.diagnosable_modes or (NO_FAULT_LABEL,)


def _single_label(sample: Sample) -> str | None:
    """混淆矩陣用的單一真實標籤；多模式樣本回 `None`（排除）。"""
    if sample.is_normal:
        return NO_FAULT_LABEL
    modes = sample.diagnosable_modes
    return modes[0] if len(modes) == 1 else None


def _strip_no_fault(ranked: list[str]) -> list[str]:
    """歸因任務把 no-fault 從候選中移除。對所有方法一視同仁。"""
    return [r for r in ranked if r != NO_FAULT_LABEL]


def _evaluate(
    method: str,
    task: str,
    rankings: dict[int, list[str]],
    samples: list[Sample],
    fold_of: dict[int, int],
    keep,
    labels: tuple[str, ...],
    strip_no_fault: bool,
) -> TaskResult:
    subset = [s for s in samples if keep(s)]
    ranked_lists = {
        s.uid: (_strip_no_fault(rankings[s.uid]) if strip_no_fault else rankings[s.uid])
        for s in subset
    }
    ranked = [ranked_lists[s.uid] for s in subset]
    truths = [_truth_set(s) for s in subset]

    def rows(pool: list[Sample]) -> tuple[list[str], list[str]]:
        y_true: list[str] = []
        y_pred: list[str] = []
        for s in pool:
            label = _single_label(s)
            if label is None or not ranked_lists[s.uid]:
                continue
            y_true.append(label)
            y_pred.append(ranked_lists[s.uid][0])
        return y_true, y_pred

    y_true, y_pred = rows(subset)
    report = classification_report(y_true, y_pred, labels)

    fold_scores: list[float] = []
    for fold in sorted(set(fold_of.values())):
        ft, fp = rows([s for s in subset if fold_of[s.uid] == fold])
        if ft:
            fold_report = classification_report(ft, fp, labels)
            fold_scores.append(fold_report.subset(_present(fold_report, labels)).macro_f1)

    return TaskResult(
        method=method,
        task=task,
        top1=top_k_hit_rate(ranked, truths, 1),
        top3=top_k_hit_rate(ranked, truths, 3),
        report=report.subset(_present(report, labels)),
        fold_macro_f1=fold_scores,
    )


def _present(report: ClassificationReport, labels: tuple[str, ...]) -> tuple[str, ...]:
    """macro 平均只算「真的出現過」的類別。

    出現過＝有真實樣本 **或** 曾被預測出來。少了後者，一個把所有東西都誤判成某類的方法
    會因為那一類不進 macro 而白白躲掉它的 precision 懲罰 —— 混淆矩陣也會少一整欄，
    讓每列加總對不上樣本數。
    """
    return tuple(
        lab
        for lab in labels
        if report.per_class[lab].support > 0 or report.per_class[lab].tp + report.per_class[lab].fp > 0
    )


def _budget_train(train: list[Sample], budget: int | None, seed: int) -> list[Sample]:
    """限制每個故障模式可用的標註筆數，正常樣本全留。

    模擬「新產線剛上線，只累積了幾筆故障紀錄」。抽樣用固定 seed，結果可重現。
    """
    if budget is None:
        return train
    import random

    rng = random.Random(seed)
    kept = [s for s in train if s.is_normal]
    for code in DIAGNOSABLE_MODES:
        pool = [s for s in train if s.is_single_mode and s.modes[0] == code]
        pool.sort(key=lambda s: s.uid)
        rng.shuffle(pool)
        kept.extend(pool[:budget])
    return kept


def _learning_curve(
    samples: list[Sample],
    folds: list[int],
    fold_of: dict[int, int],
    n_folds: int,
    channels: tuple[str, ...],
    seed: int,
    n_prototypes: int = DEFAULT_PROTOTYPES,
) -> list[LearningCurvePoint]:
    points: list[LearningCurvePoint] = []
    failures = [s for s in samples if s.diagnosable_modes]
    truths = [_truth_set(s) for s in failures]

    for budget in LEARNING_CURVE_BUDGETS:
        ranked: dict[str, dict[int, list[str]]] = {}
        for fold in range(n_folds):
            full_train = [s for i, s in enumerate(samples) if folds[i] != fold and not s.is_rnf_only]
            train = _budget_train(full_train, budget, seed + fold)
            test = [s for i, s in enumerate(samples) if folds[i] == fold and s.diagnosable_modes]
            if not test:
                continue
            fp = FingerprintModel(channels=channels, n_prototypes=n_prototypes, seed=seed).fit(train)
            bucket = ranked.setdefault("fingerprint_cosine", {})
            for s in test:
                bucket[s.uid] = _strip_no_fault([c.fault_id for c in fp.rank(s)])
            sk = sklearn_baselines(train, channels, seed=seed)
            if sk:
                for name, model in sk.items():
                    b = ranked.setdefault(name, {})
                    for s, r in zip(test, model.rank_batch(test)):
                        b[s.uid] = _strip_no_fault(r)

        scores = {
            name: top_k_hit_rate([bucket[s.uid] for s in failures], truths, 1)
            for name, bucket in ranked.items()
            if len(bucket) == len(failures)
        }
        points.append(LearningCurvePoint(budget=budget, scores=scores))
    return points


def run_validation(
    path: Path | None = None,
    n_folds: int = DEFAULT_FOLDS,
    seed: int = DEFAULT_SEED,
    channels: tuple[str, ...] = STRICT_CHANNELS,
    with_ablations: bool = True,
    with_learning_curve: bool = True,
    n_prototypes: int = DEFAULT_PROTOTYPES,
) -> ValidationReport:
    """跑完整外部驗證。

    `n_prototypes` 預設 1 —— 文件裡既有的 0.724 / 0.706 全部綁在單一原型上，
    改預設值會讓那些數字在別人重跑時對不起來。要跑多原型主實驗請顯式給
    `--prototypes 2`，報告的 `config.n_prototypes` 會標明這一次跑的是哪一種。
    """
    samples = ai4i.load_samples(path)
    summary = ai4i.dataset_summary(samples)
    folds = stratified_folds(samples, n_folds, seed)
    fold_of = {s.uid: folds[i] for i, s in enumerate(samples)}
    notes: list[str] = []

    # 方法工廠：每個 fold 重新建立、只吃 train fold。
    def make_methods(train: list[Sample]) -> dict[str, object]:
        methods: dict[str, object] = {
            "fingerprint_cosine": FingerprintModel(
                channels=channels, n_prototypes=n_prototypes, seed=seed
            ).fit(train),
            "threshold_rule": ThresholdRuleBaseline(channels=channels).fit(train),
            "majority": MajorityBaseline().fit(train),
            "stratified_random": StratifiedRandomBaseline(seed=seed).fit(train),
        }
        sk = sklearn_baselines(train, channels, seed=seed)
        if sk:
            methods.update(sk)
        return methods

    ablation_specs: dict[str, object] = {}
    if with_ablations:
        ablation_specs = {
            "fingerprint_no_prior": lambda tr: FingerprintModel(
                channels=channels, n_prototypes=n_prototypes, use_prior=False, seed=seed
            ).fit(tr),
            # 這一列刻意固定成「1 個原型」與「2 個原型」的對照，不跟著 --prototypes 走：
            # 它要回答的問題是「多原型值不值得」，而不是「這次跑了幾個原型」。
            "fingerprint_1_prototype": lambda tr: FingerprintModel(
                channels=channels, n_prototypes=1, seed=seed
            ).fit(tr),
            "fingerprint_2_prototypes": lambda tr: FingerprintModel(
                channels=channels, n_prototypes=2, seed=seed
            ).fit(tr),
            "fingerprint_derived_channels": lambda tr: FingerprintModel(
                channels=ai4i.EXTENDED_CHANNELS, n_prototypes=n_prototypes, seed=seed
            ).fit(tr),
        }

    rankings: dict[str, dict[int, list[str]]] = {}
    confidences: dict[int, tuple[float, float, str]] = {}
    # uid → (訊號強度, 該 fold 的 no-fault 門檻)，用來拆解 gate 的名目 vs 實際行為。
    gate_probe: dict[int, tuple[float, float]] = {}

    for fold in range(n_folds):
        train = [
            s
            for i, s in enumerate(samples)
            if folds[i] != fold and not s.is_rnf_only  # RNF 依定義無指紋，不進訓練
        ]
        test = [s for i, s in enumerate(samples) if folds[i] == fold]

        methods = make_methods(train)
        for name, fn in ablation_specs.items():
            methods[name] = fn(train)  # type: ignore[operator]

        for name, model in methods.items():
            bucket = rankings.setdefault(name, {})
            rank_batch = getattr(model, "rank_batch", None)
            if rank_batch is not None:
                # sklearn 模型：逐筆呼叫 predict_proba 在 10,000 × 5 fold 下慢得沒必要。
                for s, ranked in zip(test, rank_batch(test)):
                    bucket[s.uid] = ranked
            elif isinstance(model, FingerprintModel):
                # 指紋模型回傳的是 Candidate（帶分數），其他 baseline 直接回標籤字串。
                for s in test:
                    bucket[s.uid] = [c.fault_id for c in model.rank(s)]
            else:
                for s in test:
                    bucket[s.uid] = model.rank(s)  # type: ignore[union-attr]

        primary: FingerprintModel = methods["fingerprint_cosine"]  # type: ignore[assignment]
        for s in test:
            ranked = primary.rank(s)
            top = ranked[0]
            fault_conf = max((c.confidence for c in ranked if c.fault_id != NO_FAULT_LABEL), default=0.0)
            confidences[s.uid] = (top.confidence, fault_conf, top.fault_id)
            gate_probe[s.uid] = (primary.strength_of(s), primary.no_fault_threshold)

    if not sklearn_available():
        notes.append(
            "scikit-learn 未安裝，LogisticRegression / RandomForest 對照組**未執行**。"
            "指紋法的相對優勢因此只對上門檻規則與下限 baseline，證據強度較弱。"
        )
    else:
        import sklearn

        notes.append(f"scikit-learn {sklearn.__version__} 已安裝，標準分類器對照組已執行。")

    method_order = [
        "fingerprint_cosine",
        "threshold_rule",
        "logistic_regression",
        "random_forest",
        "majority",
        "stratified_random",
    ]
    ordered = [m for m in method_order if m in rankings]

    def attribution_of(name: str) -> TaskResult:
        return _evaluate(
            name,
            "attribution",
            rankings[name],
            samples,
            fold_of,
            lambda s: bool(s.diagnosable_modes),
            labels=ATTRIBUTION_LABELS,
            strip_no_fault=True,
        )

    attribution = [attribution_of(m) for m in ordered]
    end_to_end = [
        _evaluate(
            m,
            "end_to_end",
            rankings[m],
            samples,
            fold_of,
            lambda s: s.is_normal or bool(s.diagnosable_modes),
            labels=END_TO_END_LABELS,
            strip_no_fault=False,
        )
        for m in ordered
    ]
    ablations = [attribution_of(m) for m in ablation_specs if m in rankings]

    normals = [s for s in samples if s.is_normal]
    faults = [s for s in samples if s.diagnosable_modes]

    rnf_samples = [s for s in samples if s.is_rnf_only]
    rnf = None
    if rnf_samples and normals:
        rnf = RnfDiagnostics(
            n=len(rnf_samples),
            refusal_rate=sum(1 for s in rnf_samples if confidences[s.uid][2] == NO_FAULT_LABEL)
            / len(rnf_samples),
            mean_top_confidence=statistics.fmean(confidences[s.uid][0] for s in rnf_samples),
            mean_fault_confidence=statistics.fmean(confidences[s.uid][1] for s in rnf_samples),
            normal_mean_fault_confidence=statistics.fmean(confidences[s.uid][1] for s in normals),
        )

    gate = None
    if normals and faults:
        gate = GateDiagnostics(
            normal_rows=len(normals),
            raw_flag_rate=sum(1 for s in normals if gate_probe[s.uid][0] >= gate_probe[s.uid][1])
            / len(normals),
            effective_fault_rate=sum(1 for s in normals if confidences[s.uid][2] != NO_FAULT_LABEL)
            / len(normals),
            fault_rows=len(faults),
            fault_raw_pass_rate=sum(1 for s in faults if gate_probe[s.uid][0] >= gate_probe[s.uid][1])
            / len(faults),
        )

    curve = (
        _learning_curve(samples, folds, fold_of, n_folds, channels, seed, n_prototypes)
        if with_learning_curve
        else []
    )

    notes.append(
        f"歸因任務候選集 {len(ATTRIBUTION_LABELS)} 類，隨機參考值 Top-1 "
        f"{random_top_k(len(ATTRIBUTION_LABELS), 1):.3f}、Top-3 "
        f"{random_top_k(len(ATTRIBUTION_LABELS), 3):.3f}；"
        f"端到端候選集 {len(END_TO_END_LABELS)} 類，隨機參考值 Top-1 "
        f"{random_top_k(len(END_TO_END_LABELS), 1):.3f}、Top-3 "
        f"{random_top_k(len(END_TO_END_LABELS), 3):.3f}。"
    )
    notes.append(
        "歸因任務中所有方法的排序都已移除 `no_equipment_fault`；"
        "端到端任務則保留，兩張表因此不可直接互比。"
    )
    notes.append(
        f"本次主實驗每個故障模式使用 {n_prototypes} 個指紋原型"
        + (
            "（＝ docs/external_validation.md §7 既有數字的設定）。"
            if n_prototypes == 1
            else "（多原型；對應 agents/diagnosis.py 的 FaultSignature.alt_prototypes 設計）。"
        )
    )

    config = {
        "n_folds": n_folds,
        "seed": seed,
        "n_prototypes": n_prototypes,
        "channels": list(channels),
        "weights": {"signature": 0.75, "prior": 0.15, "docs": 0.10},
        "docs_note": "AI4I 無文件語料，docs 項對所有候選皆為 0，不影響排名。",
        "attribution_labels": list(ATTRIBUTION_LABELS),
        "end_to_end_labels": list(END_TO_END_LABELS),
        "sklearn": sklearn_available(),
    }
    return ValidationReport(
        dataset=summary,
        config=config,
        attribution=attribution,
        end_to_end=end_to_end,
        ablations=ablations,
        rnf=rnf,
        gate=gate,
        learning_curve=curve,
        notes=notes,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m factory_guardian.validation",
        description="AI4I 2020 外部驗證：感測器指紋餘弦法 vs baseline",
    )
    parser.add_argument("--csv", type=Path, default=None, help="ai4i2020.csv 路徑")
    parser.add_argument("--folds", type=int, default=DEFAULT_FOLDS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--json", type=Path, default=None, help="輸出 JSON 結果")
    parser.add_argument("--markdown", type=Path, default=None, help="輸出 Markdown 結果段落")
    parser.add_argument(
        "--prototypes",
        type=int,
        default=DEFAULT_PROTOTYPES,
        help="每個故障模式的指紋原型數（預設 1；2 = 對應 agents/diagnosis.py 的多原型設計）",
    )
    parser.add_argument("--no-ablations", action="store_true")
    parser.add_argument("--no-learning-curve", action="store_true")
    args = parser.parse_args(argv)

    if not ai4i.dataset_available(args.csv or ai4i.DATASET_CSV):
        print(f"[skip] 找不到資料集：{args.csv or ai4i.DATASET_CSV}")
        print(f"       下載位置：{ai4i.DATASET_URL}")
        return 2

    report = run_validation(
        path=args.csv,
        n_folds=args.folds,
        seed=args.seed,
        with_ablations=not args.no_ablations,
        with_learning_curve=not args.no_learning_curve,
        n_prototypes=args.prototypes,
    )
    text = report.to_markdown()
    print(text)
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    if args.markdown:
        args.markdown.write_text(text, "utf-8")
    return 0


__all__ = [
    "ATTRIBUTION_LABELS",
    "DEFAULT_FOLDS",
    "DEFAULT_PROTOTYPES",
    "DEFAULT_SEED",
    "END_TO_END_LABELS",
    "LEARNING_CURVE_BUDGETS",
    "GateDiagnostics",
    "LearningCurvePoint",
    "RnfDiagnostics",
    "TaskResult",
    "ValidationReport",
    "main",
    "run_validation",
    "stratified_folds",
]
