"""五道關卡(規格 §3.1–§3.2)。

G0 來源信任 → G1 動作解析 → G2 政策裁決 → G3 後果預演 → G4 人工核准 → G5 執行與封存。
每一道關卡都可以否決,且否決本身也是稽核事件。
"""

from .g0_provenance import evaluate_trust
from .g1_resolution import resolve_action
from .g2_policy import ACTION_POLICY, GateRule, PolicyEngine
from .g3_projection import project_consequences
from .g4_approval import ApprovalQueue, PendingApproval
from .g5_audit import AuditChain

__all__ = [
    "ACTION_POLICY",
    "ApprovalQueue",
    "AuditChain",
    "GateRule",
    "PendingApproval",
    "PolicyEngine",
    "evaluate_trust",
    "project_consequences",
    "resolve_action",
]
