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

import math
import random
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

# --------------------------------------------------------------------------------------
# 權重穩健性掃描（robustness_scan）
#
# 為什麼需要這一段：上面那六個權重是人訂的。只要它們是人訂的，評審就一定會問
# 「權重改一點，推薦是不是就換人了？」——「不會」不能用講的，要用算的。
# 所以排名之後再跑一次擾動掃描，回報「推薦方案在多少比例的權重擾動下不變」，
# 以及「哪個準則的權重最容易翻轉排名、翻轉的臨界值是多少」。
#
# 這是純計算：固定種子、固定網格，同一批方案跑幾次結果都一樣，沒有 LLM 參與。
# --------------------------------------------------------------------------------------
ROBUSTNESS_PERTURBATION = 0.20     # 每個權重的擾動幅度（±20%）
ROBUSTNESS_SAMPLES = 128           # 六個權重同時擾動的隨機抽樣數
ROBUSTNESS_SEED = 20260809         # 固定種子：抽樣也必須可重現
_FLIP_SCAN_MAX_MULTIPLIER = 200.0  # 臨界權重搜尋時單一權重可以被放大的上限


@dataclass
class RankingResult:
    ranked: list[RecoveryPlan]
    weights: dict[str, float]
    raw_criteria: dict[str, dict[str, float]] = field(default_factory=dict)
    normalized: dict[str, dict[str, float]] = field(default_factory=dict)
    # 權重擾動掃描結果（見 robustness_scan）。None 代表這次排名沒有跑掃描。
    robustness: dict[str, object] | None = None

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
            "robustness": self.robustness,
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


def normalize_weights(weights: dict[str, float] | None = None) -> dict[str, float]:
    """把權重正規化成總和 1。掃描與排名共用同一個入口，避免兩邊算的不是同一組權重。"""
    values = dict(weights or DEFAULT_WEIGHTS)
    total = sum(values.values()) or 1.0
    return {k: v / total for k, v in values.items()}


def is_feasible(plan: RecoveryPlan) -> bool:
    """硬限制：Safety BLOCK 的方案不可行。這條和權重無關，所以擾動掃描時不必重算。"""
    if plan.safety is not None and plan.safety.verdict is SafetyVerdictKind.BLOCK:
        return False
    return plan.feasible


def score_plan(
    plan: RecoveryPlan, weights: dict[str, float] | None = None
) -> tuple[float, dict[str, float]]:
    """單一方案的加權分數與各準則貢獻（不改動方案物件）。

    Production Agent 的參數搜尋需要在「還沒進排名」的時候比較變體，
    用的必須是**同一套**尺規與權重 —— 所以評分邏輯只有這一份實作。
    """
    normalized_weights = normalize_weights(weights)
    raw = extract_criteria(plan)
    breakdown = {c: normalized_weights[c] * _score_criterion(c, raw[c]) for c in normalized_weights}
    return sum(breakdown.values()), breakdown


def _weighted(row: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights[c] * row[c] for c in weights)


def _pick_winner(scores: dict[str, float]) -> str | None:
    """和 rank_plans 同一套排序規則：分數高者勝，同分時 plan_id 字典序小者勝。"""
    if not scores:
        return None
    return min(scores, key=lambda pid: (-scores[pid], pid))


def _flip_interval(
    rows: dict[str, dict[str, float]],
    live: list[str],
    weights: dict[str, float],
    criterion: str,
    baseline: str,
) -> tuple[float, float]:
    """求「推薦方案維持不變」的權重倍率區間 ``[lo, hi]``（乘在 criterion 的權重上）。

    這裡不用二分搜尋，因為答案是**解析解**：把 criterion 的權重乘以 m 之後，
    方案 i 的分數是 ``(A_i + m·B_i) / (S + m·w_c)``，分母對所有方案相同且恆正，
    所以「baseline 仍然贏過 j」等價於一條線性不等式 ``(A_b−A_j) + m·(B_b−B_j) ≥ 0``。
    每條不等式切出一條半線，交集必然是一個包含 m=1 的區間 —— 直接解出來就好，
    既精確又可重現，不會因為搜尋步長不同而給出不一樣的答案。
    """
    weight_c = weights[criterion]
    lo, hi = 0.0, math.inf
    if weight_c <= 0.0:
        return lo, hi
    others = [c for c in weights if c != criterion]
    base_a = sum(weights[c] * rows[baseline][c] for c in others)
    base_b = weight_c * rows[baseline][criterion]
    for pid in live:
        if pid == baseline:
            continue
        delta_a = base_a - sum(weights[c] * rows[pid][c] for c in others)
        delta_b = base_b - weight_c * rows[pid][criterion]
        if abs(delta_b) < 1e-12:
            if delta_a < 0 or (abs(delta_a) < 1e-12 and pid < baseline):
                return 1.0, 1.0     # 這個準則怎麼調都救不回來（理論上不會發生）
            continue
        crossing = -delta_a / delta_b
        if delta_b > 0:
            lo = max(lo, crossing)
        else:
            hi = min(hi, crossing)
    return max(0.0, lo), hi


def _weight_after(weight_c: float, multiplier: float) -> float:
    """權重乘上倍率並重新正規化之後，這個準則實際佔多少（其餘權重維持原比例）。"""
    scaled = weight_c * multiplier
    return scaled / (scaled + (1.0 - weight_c)) if scaled + (1.0 - weight_c) > 0 else 0.0


def robustness_scan(
    plans: Iterable[RecoveryPlan],
    weights: dict[str, float] | None = None,
    perturbation: float = ROBUSTNESS_PERTURBATION,
    samples: int = ROBUSTNESS_SAMPLES,
) -> dict[str, object]:
    """權重擾動掃描：推薦方案禁不禁得起「權重是你自己訂的」這個質疑。

    回報三件事：

    1. **穩定比例**：在 ±perturbation 的權重擾動下，推薦方案不變的情境佔多少。
       擾動情境分兩種：單一權重加減 perturbation 的確定性網格（每個準則兩個點），
       以及六個權重同時擾動的隨機抽樣（固定種子，可重現）。
    2. **第一名與第二名的分數差**：基準權重下的差距，以及所有擾動情境中的**最小**差距 ——
       後者才是真正的安全邊界。
    3. **最敏感準則與臨界權重**：哪個準則的權重只要動到某個值，推薦就換人。

    純計算，不可用 LLM：這是要拿去回答評審質疑的數字，必須每次跑都一樣。
    """
    plan_list = list(plans)
    base_weights = normalize_weights(weights)
    criteria = list(base_weights)
    rows = {
        plan.plan_id: {c: _score_criterion(c, values[c]) for c in criteria}
        for plan, values in ((p, extract_criteria(p)) for p in plan_list)
    }
    live = [plan.plan_id for plan in plan_list if is_feasible(plan)]

    result: dict[str, object] = {
        "perturbation": round(perturbation, 4),
        "samples": int(samples),
        "seed": ROBUSTNESS_SEED,
        "feasible_plans": len(live),
        "baseline_plan_id": None,
        "stable_fraction": None,
        "scenarios": 0,
        "stable_scenarios": 0,
        "flips": [],
        "baseline_top_gap": None,
        "min_top_gap": None,
        "runner_up_plan_id": None,
        "most_sensitive_criterion": None,
        "critical_weight": None,
        "criteria": {},
        "note": "確定性網格 + 固定種子抽樣，純計算；LLM 未參與。",
    }
    if not live:
        return result

    base_scores = {pid: _weighted(rows[pid], base_weights) for pid in live}
    baseline = _pick_winner(base_scores)
    result["baseline_plan_id"] = baseline
    ordered = sorted(live, key=lambda pid: (-base_scores[pid], pid))
    if len(ordered) >= 2:
        result["runner_up_plan_id"] = ordered[1]
        result["baseline_top_gap"] = round(base_scores[ordered[0]] - base_scores[ordered[1]], 4)

    if len(live) == 1:
        # 只有一個可行方案：權重再怎麼動都是它。這不是「很穩健」，是「沒得選」，
        # 所以照實回報，不要讓 100% 看起來像是比較出來的結果。
        result.update({
            "stable_fraction": 1.0, "scenarios": 0, "stable_scenarios": 0,
            "min_top_gap": None,
            "note": "只有一個可行方案，權重擾動不影響結果（其餘方案被 Safety 硬限制擋下）。",
        })
        return result

    # --- 情境 1：單一權重 ±perturbation 的確定性網格 ---------------------------------
    scenarios: list[tuple[str, str, float, dict[str, float]]] = []
    for criterion in criteria:
        for sign in (-1.0, 1.0):
            perturbed = dict(base_weights)
            perturbed[criterion] = max(1e-9, base_weights[criterion] * (1.0 + sign * perturbation))
            scenarios.append(("grid", criterion, sign, normalize_weights(perturbed)))

    # --- 情境 2：六個權重同時擾動的抽樣（固定種子）------------------------------------
    rng = random.Random(ROBUSTNESS_SEED)
    for _ in range(max(0, int(samples))):
        perturbed = {
            c: max(1e-9, base_weights[c] * (1.0 + rng.uniform(-perturbation, perturbation)))
            for c in criteria
        }
        scenarios.append(("sample", "", 0.0, normalize_weights(perturbed)))

    stable = 0
    min_gap = math.inf
    flips: list[dict[str, object]] = []
    for kind, criterion, sign, perturbed in scenarios:
        scores = {pid: _weighted(rows[pid], perturbed) for pid in live}
        order = sorted(live, key=lambda pid: (-scores[pid], pid))
        min_gap = min(min_gap, scores[order[0]] - scores[order[1]])
        if order[0] == baseline:
            stable += 1
        elif kind == "grid" and len(flips) < 12:
            flips.append({
                "criterion": criterion,
                "direction": "+" if sign > 0 else "-",
                "weight": round(perturbed[criterion], 4),
                "winner": order[0],
            })

    result["scenarios"] = len(scenarios)
    result["stable_scenarios"] = stable
    result["stable_fraction"] = round(stable / len(scenarios), 4) if scenarios else None
    result["min_top_gap"] = round(min_gap, 4) if min_gap < math.inf else None
    result["flips"] = flips

    # --- 臨界權重：哪個準則最容易把推薦翻掉 -------------------------------------------
    detail: dict[str, object] = {}
    most_sensitive: str | None = None
    smallest_shift = math.inf
    for criterion in criteria:
        lo, hi = _flip_interval(rows, live, base_weights, criterion, baseline)
        entry: dict[str, object] = {"weight": round(base_weights[criterion], 4)}
        options: list[tuple[float, float, str]] = []
        if lo > 0.0:
            options.append((abs(_weight_after(base_weights[criterion], lo) - base_weights[criterion]),
                            _weight_after(base_weights[criterion], lo), "down"))
        if hi < _FLIP_SCAN_MAX_MULTIPLIER:
            options.append((abs(_weight_after(base_weights[criterion], hi) - base_weights[criterion]),
                            _weight_after(base_weights[criterion], hi), "up"))
        if options:
            shift, critical, direction = min(options)
            relative = shift / base_weights[criterion] if base_weights[criterion] > 0 else math.inf
            entry.update({
                "critical_weight": round(critical, 4),
                "direction": direction,
                "absolute_shift": round(shift, 4),
                "relative_shift_pct": round(100.0 * relative, 1),
                "within_perturbation": bool(relative <= perturbation + 1e-9),
            })
            if relative < smallest_shift:
                smallest_shift = relative
                most_sensitive = criterion
        else:
            entry.update({"critical_weight": None, "relative_shift_pct": None, "within_perturbation": False})
        detail[criterion] = entry

    result["criteria"] = detail
    result["most_sensitive_criterion"] = most_sensitive
    if most_sensitive is not None:
        result["critical_weight"] = detail[most_sensitive]["critical_weight"]      # type: ignore[index]
        result["critical_shift_pct"] = detail[most_sensitive]["relative_shift_pct"]  # type: ignore[index]
    return result


def rank_plans(
    plans: Iterable[RecoveryPlan],
    weights: dict[str, float] | None = None,
    robustness: bool = True,
    perturbation: float = ROBUSTNESS_PERTURBATION,
    samples: int = ROBUSTNESS_SAMPLES,
) -> RankingResult:
    """對方案做加權多準則排名。這是純計算，沒有 LLM 參與。"""
    plan_list = list(plans)
    weights = normalize_weights(weights)

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

    result = RankingResult(ranked=ranked, weights=weights, raw_criteria=raw, normalized=normalized)
    # 4) 權重穩健性：排完名再問一次「權重動一動，推薦會不會換人」
    if robustness:
        result.robustness = robustness_scan(
            plan_list, weights=weights, perturbation=perturbation, samples=samples
        )
    return result


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


CRITERION_LABELS: dict[str, str] = {
    "safety": "工安裕度",
    "production": "產線達成率",
    "delivery": "交期",
    "recovery": "復原時間",
    "cost": "成本",
    "equipment": "設備保全",
}


def explain_robustness(robustness: dict[str, object] | None) -> str:
    """把掃描結果寫成畫面／稽核可以直接引用的一行字。"""
    if not robustness or robustness.get("stable_fraction") is None:
        return "未執行權重穩健性掃描。"
    fraction = float(robustness["stable_fraction"])           # type: ignore[arg-type]
    perturbation = float(robustness.get("perturbation") or 0.0)
    sensitive = robustness.get("most_sensitive_criterion")
    label = CRITERION_LABELS.get(str(sensitive), str(sensitive)) if sensitive else "無"
    line = f"在 ±{perturbation * 100:.0f}% 權重擾動下推薦不變比例 {fraction * 100:.0f}%，最敏感準則：{label}"
    critical = robustness.get("critical_weight")
    if sensitive and critical is not None:
        line += f"（權重由 {float(robustness['criteria'][sensitive]['weight']):.2f} 動到 {float(critical):.2f} 才翻轉）"  # type: ignore[index]
    return line + "。"


__all__ = [
    "CRITERIA_DIRECTION",
    "DEFAULT_WEIGHTS",
    "ROBUSTNESS_PERTURBATION",
    "ROBUSTNESS_SAMPLES",
    "ROBUSTNESS_SEED",
    "RankingResult",
    "extract_criteria",
    "explain_robustness",
    "is_feasible",
    "normalize_weights",
    "rank_plans",
    "robustness_scan",
    "score_plan",
    "explain_ranking",
]
