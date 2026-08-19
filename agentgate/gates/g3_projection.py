"""G3 後果預演(Projection)— 差異化核心之二(規格 §4.4)。

不是問「這個動作現在合法嗎」,而是問「執行後系統會變成什麼樣」。
預演在影子環境乾跑,寫入不生效;affected_count 是實際撈出來的,
不是 Agent 自己申報的——申報與實測的落差本身就是攻擊訊號(範圍逃逸)。
"""

from __future__ import annotations

from ..ontology import (
    ActionKind,
    ActionRequest,
    Evidence,
    Finding,
    Projection,
    Severity,
)
from ..shadow import ShadowTelecomEnv


def project_consequences(
    request: ActionRequest, shadow: ShadowTelecomEnv
) -> tuple[Projection, list[Finding]]:
    """乾跑動作,回傳預演結果與預演觸發的規則。"""
    projection = shadow.project(request)
    findings: list[Finding] = []

    # AG-31 範圍逃逸:申報筆數與影子環境實測不符 → 直接攔下
    if request.kind is ActionKind.READ_BULK:
        declared = request.params.get("declared_count")
        if declared is not None and projection.affected_count > declared:
            findings.append(
                Finding(
                    rule_id="AG-31",
                    title="範圍逃逸(申報與實測不符)",
                    severity=Severity.BLOCK,
                    message=(
                        f"批次查詢申報 {declared} 筆,但影子環境實測將撈出 "
                        f"{projection.affected_count} 筆。申報範圍與實際後果不符,攔下。"
                    ),
                    statute="批次動作之實際影響範圍不得超過申報範圍;預演實測超出即攔下。",
                    evidence=(
                        Evidence("projection", "affected_count",
                                 f"影子環境實測 {projection.affected_count} 筆 "
                                 f"> 申報 {declared} 筆"),
                    ),
                )
            )

    # AG-30 不可回復的動作應當永遠需要人工核准,無論金額大小(規格 §4.4)
    if not projection.reversible and request.kind is not ActionKind.POLICY_OVERRIDE:
        findings.append(
            Finding(
                rule_id="AG-30",
                title="不可回復動作需人工核准",
                severity=Severity.APPROVAL_REQUIRED,
                message=f"{request.kind.value} 執行後不可回復,無論規模一律需人工核准。",
                statute="不可回復的動作應當永遠需要人工核准,無論金額大小。",
                evidence=(
                    Evidence("projection", "reversible", "預演結果:reversible = False"),
                ),
            )
        )
    return projection, findings


def projection_evidence(projection: Projection) -> list[Evidence]:
    """把預演結果轉成證據,供核准介面呈現。"""
    items = [
        Evidence("projection", "affected_count",
                 f"受影響主體 {projection.affected_count} 個"),
        Evidence("projection", "reversible",
                 ("可回復" + (f"(時窗 {projection.reversal_window_hours} 小時)"
                              if projection.reversal_window_hours else ""))
                 if projection.reversible else "不可回復"),
    ]
    if projection.financial_delta:
        items.append(
            Evidence("projection", "financial_delta",
                     f"金流影響 {projection.financial_delta:+,.0f} 元/月"
                     if projection.financial_delta else "無金流影響")
        )
    if projection.pii_fields_exposed:
        items.append(
            Evidence("projection", "pii_fields",
                     "觸及個資欄位:" + ", ".join(projection.pii_fields_exposed))
        )
    return items


__all__ = ["project_consequences", "projection_evidence"]
