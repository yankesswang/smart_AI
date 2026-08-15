"""評估指標：Top-k 命中率、每類 precision / recall / F1、混淆矩陣。

全部手寫（不依賴 sklearn.metrics），理由有二：

1. `sklearn` 不在 `pyproject.toml` 的相依裡，本模組的核心路徑不該因為它缺席就跑不動。
2. 指標定義必須在程式碼裡看得到。研究文件 §3.3 把「只報準確率」列為失分警訊，
   那麼至少要讓評審能一眼看出 macro / micro、以及不平衡資料下我們報的是哪一種。

**類別極度不平衡**（故障 3.39%），所以：

* accuracy 完全不看（全猜正常就有 96.6%）—— 只在報告裡當對照數字列出來提醒這件事。
* 主指標是 **macro-F1**：每個故障模式權重相同，樣本少的 TWF 不會被 HDF 蓋掉。
* 每一類的 precision / recall 一律連同 support 一起報，support < 30 的類別要標明不可靠。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClassMetrics:
    label: str
    support: int
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "support": self.support,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class ClassificationReport:
    labels: tuple[str, ...]
    per_class: dict[str, ClassMetrics]
    matrix: dict[str, dict[str, int]]
    """``matrix[true][pred]`` 的計數。"""

    total: int

    @property
    def accuracy(self) -> float:
        correct = sum(self.matrix[label].get(label, 0) for label in self.labels)
        return correct / self.total if self.total else 0.0

    @property
    def macro_f1(self) -> float:
        if not self.per_class:
            return 0.0
        return sum(m.f1 for m in self.per_class.values()) / len(self.per_class)

    @property
    def macro_precision(self) -> float:
        if not self.per_class:
            return 0.0
        return sum(m.precision for m in self.per_class.values()) / len(self.per_class)

    @property
    def macro_recall(self) -> float:
        if not self.per_class:
            return 0.0
        return sum(m.recall for m in self.per_class.values()) / len(self.per_class)

    def subset(self, labels: tuple[str, ...]) -> "ClassificationReport":
        """只保留指定類別的 macro 統計（例如排掉 `no_equipment_fault` 只看故障模式）。"""
        return ClassificationReport(
            labels=labels,
            per_class={k: v for k, v in self.per_class.items() if k in labels},
            matrix=self.matrix,
            total=self.total,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "accuracy": round(self.accuracy, 4),
            "macro_precision": round(self.macro_precision, 4),
            "macro_recall": round(self.macro_recall, 4),
            "macro_f1": round(self.macro_f1, 4),
            "per_class": [self.per_class[label].to_dict() for label in self.labels if label in self.per_class],
            "matrix": {t: dict(row) for t, row in self.matrix.items()},
        }

    def matrix_markdown(self) -> str:
        """混淆矩陣的 Markdown 表格：列＝真實，欄＝預測。"""
        head = "| 真實＼預測 | " + " | ".join(self.labels) + " | 合計 |"
        sep = "|---|" + "---:|" * (len(self.labels) + 1)
        lines = [head, sep]
        for t in self.labels:
            row = self.matrix.get(t, {})
            total = sum(row.values())
            cells = []
            for p in self.labels:
                value = row.get(p, 0)
                cells.append(f"**{value}**" if p == t and value else str(value))
            lines.append(f"| {t} | " + " | ".join(cells) + f" | {total} |")
        return "\n".join(lines)


def classification_report(
    y_true: list[str], y_pred: list[str], labels: tuple[str, ...]
) -> ClassificationReport:
    matrix: dict[str, dict[str, int]] = {t: {p: 0 for p in labels} for t in labels}
    for t, p in zip(y_true, y_pred):
        if t not in matrix:
            matrix[t] = {lab: 0 for lab in labels}
        matrix[t][p] = matrix[t].get(p, 0) + 1

    per_class: dict[str, ClassMetrics] = {}
    for label in labels:
        tp = matrix.get(label, {}).get(label, 0)
        fp = sum(matrix.get(t, {}).get(label, 0) for t in matrix if t != label)
        fn = sum(v for p, v in matrix.get(label, {}).items() if p != label)
        per_class[label] = ClassMetrics(label=label, support=tp + fn, tp=tp, fp=fp, fn=fn)

    return ClassificationReport(labels=labels, per_class=per_class, matrix=matrix, total=len(y_true))


def top_k_hit_rate(rankings: list[list[str]], truths: list[tuple[str, ...]], k: int) -> float:
    """Top-k 命中率：前 k 個候選中出現任何一個正確標籤就算命中。

    用「集合命中」而不是「等於第一個標籤」，是因為 AI4I 有 24 筆同時被標記多個故障模式的紀錄。
    對這些紀錄而言，答對其中任一個模式在維修現場都是有效的診斷，不該判為錯。
    """
    if not rankings:
        return 0.0
    hits = 0
    for ranked, truth in zip(rankings, truths):
        if set(ranked[:k]) & set(truth):
            hits += 1
    return hits / len(rankings)


def random_top_k(n_classes: int, k: int) -> float:
    """隨機排序下的 Top-k 期望命中率（單一正確答案）。

    報 Top-3 一定要同時報這個數字。候選只有 5 類時 Top-3 隨機就有 60%，
    不寫出來的話 Top-3 = 0.9 看起來很強，其實可能只贏隨機 30 個百分點。
    """
    if n_classes <= 0:
        return 0.0
    return min(1.0, k / n_classes)


__all__ = [
    "ClassMetrics",
    "ClassificationReport",
    "classification_report",
    "random_top_k",
    "top_k_hit_rate",
]
