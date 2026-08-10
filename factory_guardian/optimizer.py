"""方案排名引擎（規格 §9）。

> LLM 負責理解事件、整理 Evidence 與協調 Agent；
> 最終 Plan 排名應由規則或最佳化模型計算，避免 LLM 直接決定所有控制行動。

實作方式是加權多準則決策（Weighted Multi-Criteria Decision Analysis）：

1. **硬限制**先過濾：Safety BLOCK 的方案直接標記為不可行，不進入排名。
2. 每個準則做 min–max 正規化（同一批方案之間相對比較），方向統一為「越大越好」。
3. 加權求和得到分數，並輸出每個準則的貢獻，讓評審看得到「為什麼是這個方案」。

所有輸入（產能、交期、復原時間、成本、殘餘風險）都來自 Simulator 的乾跑，不是估的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .domain import RecoveryPlan, SafetyVerdictKind

# 準則說明與原始值方向（True = 原始值越大越好）。
# delivery / recovery / cost 的原始值是「越小越好」，由 _score_criterion 反轉成得分。
CRITERIA_DIRECTION: dict[str, bool] = {
    "safety": True,             # 安全裕度 0~1
    "production": True,         # 產線達成率 %
    "delivery": False,          # 最大交期延遲（分鐘）
    "recovery": False,          # 復原時間（分鐘）
    "cost": False,              # 總成本（NTD）
    "equipment": True,          # 設備保全 = 1 − 殘餘劣化風險
}

# 固定評分尺規（見 _score_criterion）。這些是可稽核的常數，不是每次執行才決定的。
PRODUCTION_FULL_PCT = 100.0      # 產線達成率到 100% 即滿分（超過視同滿分）
DELAY_WORST_MIN = 240.0          # 交期延遲 4 小時視為零分
RECOVERY_WORST_MIN = 120.0       # 復原時間 2 小時視為零分
COST_WORST_NTD = 300_000.0       # 總成本 30 萬 NTD 視為零分

DEFAULT_WEIGHTS: dict[str, float] = {
    "safety": 0.30,
    "production": 0.20,
    "delivery": 0.22,
    "recovery": 0.12,
    "cost": 0.08,
    "equipment": 0.08,
}


@dataclass
class RankingResult:
    ranked: list[RecoveryPlan]
    weights: dict[str, float]
    raw_criteria: dict[str, dict[str, float]] = field(default_factory=dict)
    normalized: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def recommended(self) -> RecoveryPlan | None:
        for plan in self.ranked:
            if plan.feasible:
                return plan
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "weights": self.weights,
            "recommended_plan_id": self.recommended.plan_id if self.recommended else None,
            "raw_criteria": {p: {k: round(v, 3) for k, v in c.items()} for p, c in self.raw_criteria.items()},
            "normalized": {p: {k: round(v, 3) for k, v in c.items()} for p, c in self.normalized.items()},
            "ranking": [
                {
                    "rank": p.rank,
                    "plan_id": p.plan_id,
                    "title": p.title,
                    "score": round(p.score, 4),
                    "feasible": p.feasible,
                    "infeasible_reason": p.infeasible_reason,
                }
                for p in self.ranked
            ],
        }


def _safety_margin(plan: RecoveryPlan) -> float:
    """把 Safety 裁決轉成 0~1 的安全裕度分數。

    APPROVAL_REQUIRED 只扣一點點（0.9）。它代表的是「需要人簽名」這個治理程序，
    不是「這個方案比較危險」。早期版本把它扣到 0.6，結果系統會為了避開核准流程
    而偏好「不用人簽名但其實比較糟」的方案 —— 那是把治理成本誤當成安全風險。
    真正的危險由 BLOCK 表達，而 BLOCK 是硬限制，方案會直接出局。
    """
    if plan.safety is None:
        return 0.5
    if plan.safety.verdict is SafetyVerdictKind.BLOCK:
        return 0.0
    if plan.safety.verdict is SafetyVerdictKind.APPROVAL_REQUIRED:
        return 0.9
    return max(0.8, 1.0 - 0.02 * len(plan.safety.findings))


NEVER_RECOVERED_PENALTY_MIN = 60.0


def extract_criteria(plan: RecoveryPlan) -> dict[str, float]:
    """從方案的模擬投影抽出六個準則的原始值（方向已統一為越大越好）。"""
    proj = plan.projection
    # 「整段都沒回到門檻」和「剛好在最後一刻回來」不能得到同樣的復原分數，
    # 否則放著不管會和真正的復原方案並列。
    recovery_penalty = 0.0 if proj.recovered else NEVER_RECOVERED_PENALTY_MIN
    return {
        "safety": _safety_margin(plan),
        "production": proj.production_pct,
        "delivery": proj.max_order_delay_min,
        "recovery": proj.recovery_min + recovery_penalty,
        "cost": proj.cost_ntd,
        "equipment": 1.0 - proj.residual_risk,
    }


def _score_criterion(criterion: str, raw: float) -> float:
    """用**固定尺規**把原始值換算成 0~1 的得分。

    這裡刻意不用 min–max 正規化。min–max 是相對比較：
    只要候選方案在某個準則上差距很小，正規化也會把最好的拉到 1、最差的壓到 0，
    無中生有地製造出鑑別力 —— 一個 0.15 的實質差距可以整碗端走 0.30 的權重。

    固定尺規讓分數有絕對意義：0.83 分的方案在不同情境、不同執行之間都代表同一件事，
    評審也能直接看懂「這個方案為什麼拿這個分數」。
    """
    if criterion == "safety":
        return max(0.0, min(1.0, raw))                      # 已是 0~1 絕對尺度
    if criterion == "equipment":
        return max(0.0, min(1.0, raw))                      # 1 − 殘餘風險
    if criterion == "production":
        return max(0.0, min(1.0, raw / PRODUCTION_FULL_PCT))
    if criterion == "delivery":
        return max(0.0, 1.0 - min(raw, DELAY_WORST_MIN) / DELAY_WORST_MIN)
    if criterion == "recovery":
        return max(0.0, 1.0 - min(raw, RECOVERY_WORST_MIN) / RECOVERY_WORST_MIN)
    if criterion == "cost":
        return max(0.0, 1.0 - min(raw, COST_WORST_NTD) / COST_WORST_NTD)
    return 0.0


def rank_plans(
    plans: Iterable[RecoveryPlan],
    weights: dict[str, float] | None = None,
) -> RankingResult:
    """對方案做加權多準則排名。這是純計算，沒有 LLM 參與。"""
    plan_list = list(plans)
    weights = dict(weights or DEFAULT_WEIGHTS)
    total_weight = sum(weights.values()) or 1.0
    weights = {k: v / total_weight for k, v in weights.items()}

    if not plan_list:
        return RankingResult(ranked=[], weights=weights)

    # 1) 硬限制：Safety BLOCK → 不可行
    for plan in plan_list:
        if plan.safety is not None and plan.safety.verdict is SafetyVerdictKind.BLOCK:
            plan.feasible = False
            reasons = "；".join(f.message for f in plan.safety.findings if f.verdict is SafetyVerdictKind.BLOCK)
            plan.infeasible_reason = f"Safety Agent BLOCK：{reasons}"

    raw = {plan.plan_id: extract_criteria(plan) for plan in plan_list}

    # 2) 依固定尺規換算成 0~1 得分
    normalized: dict[str, dict[str, float]] = {
        pid: {c: _score_criterion(c, values[c]) for c in weights} for pid, values in raw.items()
    }

    # 3) 加權求和
    for plan in plan_list:
        breakdown = {c: weights[c] * normalized[plan.plan_id][c] for c in weights}
        plan.score_breakdown = breakdown
        plan.score = sum(breakdown.values())
        if not plan.feasible:
            plan.score = -1.0  # 不可行方案永遠排在最後，但仍然列出來供審視

    ranked = sorted(plan_list, key=lambda p: (-p.score, p.plan_id))
    for idx, plan in enumerate(ranked, start=1):
        plan.rank = idx

    return RankingResult(ranked=ranked, weights=weights, raw_criteria=raw, normalized=normalized)


def explain_ranking(result: RankingResult) -> str:
    """把排名結果寫成一段可以直接放進報告的說明。"""
    if not result.ranked:
        return "沒有可比較的方案。"
    lines: list[str] = []
    recommended = result.recommended
    if recommended is None:
        return "所有方案都被 Safety Agent 阻擋，需要人工介入重新規劃。"
    lines.append(
        f"推薦方案：{recommended.plan_id}｜{recommended.title}（加權分數 {recommended.score:.3f}）。"
    )
    top_criteria = sorted(recommended.score_breakdown.items(), key=lambda kv: -kv[1])[:3]
    lines.append("主要得分來源：" + "、".join(f"{k}（{v:.3f}）" for k, v in top_criteria) + "。")
    for plan in result.ranked:
        if plan is recommended:
            continue
        if not plan.feasible:
            lines.append(f"{plan.plan_id}｜{plan.title}：不可行。{plan.infeasible_reason}")
        else:
            gap = recommended.score - plan.score
            worst = max(
                plan.score_breakdown.items(),
                key=lambda kv: recommended.score_breakdown.get(kv[0], 0.0) - kv[1],
            )
            lines.append(
                f"{plan.plan_id}｜{plan.title}：分數 {plan.score:.3f}（落後 {gap:.3f}），"
                f"主要劣勢在 {worst[0]}。"
            )
    return "\n".join(lines)


__all__ = [
    "CRITERIA_DIRECTION",
    "DEFAULT_WEIGHTS",
    "RankingResult",
    "extract_criteria",
    "rank_plans",
    "explain_ranking",
]
