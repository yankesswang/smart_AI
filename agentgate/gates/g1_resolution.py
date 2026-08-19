"""G1 動作解析(Resolution)。

把自由形式的工具呼叫映射成結構化動作描述子(ActionRequest),
解析受影響主體與範圍。LLM 只允許出現在這一層的「映射」角色,
且其輸出必須通過本模組的 schema 驗證才進得了 G2(規格 §4.3)。
"""

from __future__ import annotations

from typing import Any

from ..ontology import (
    ActionKind,
    ActionRequest,
    Channel,
    PrincipalRole,
    Provenance,
)

# 各動作的參數 schema:(必填欄位, 驗證器)
_REQUIRED_PARAMS: dict[ActionKind, list[str]] = {
    ActionKind.READ_ACCOUNT: [],
    ActionKind.READ_BULK: [],
    ActionKind.ISSUE_REFUND: ["amount"],
    ActionKind.CHANGE_PLAN: ["new_plan_id"],
    ActionKind.SUSPEND_SERVICE: [],
    ActionKind.REISSUE_SIM: [],
    ActionKind.POLICY_OVERRIDE: [],
}


def validate_request(request: ActionRequest) -> list[str]:
    """schema 驗證。回傳錯誤訊息列表;空列表 = 通過。"""
    errors: list[str] = []
    params = request.params
    for field_name in _REQUIRED_PARAMS[request.kind]:
        if field_name not in params:
            errors.append(f"缺少必要參數 {field_name}(動作 {request.kind.value})")
    if request.kind is ActionKind.ISSUE_REFUND and "amount" in params:
        try:
            amount = float(params["amount"])
            if amount <= 0:
                errors.append("退費金額必須為正數")
        except (TypeError, ValueError):
            errors.append("退費金額必須為數值")
    if request.kind is ActionKind.READ_BULK:
        filters = params.get("filters")
        if filters is not None and not isinstance(filters, dict):
            errors.append("filters 必須為物件")
        declared = params.get("declared_count")
        if declared is not None and (not isinstance(declared, int) or declared < 0):
            errors.append("declared_count 必須為非負整數")
    if not request.principal:
        errors.append("缺少 principal(代表誰執行)")
    if not request.agent_id:
        errors.append("缺少 agent_id")
    return errors


def resolve_action(payload: dict[str, Any]) -> tuple[ActionRequest | None, list[str]]:
    """把工具呼叫 payload 解析成 ActionRequest。

    回傳 (request, errors)。解析失敗時 request 為 None。
    這是 API 邊界:任何欄位錯誤都在這裡擋下,不讓畸形輸入進 G2。
    """
    errors: list[str] = []

    kind_raw = payload.get("kind", "")
    try:
        kind = ActionKind(kind_raw)
    except ValueError:
        return None, [f"未知動作種類 {kind_raw!r};合法值:{[k.value for k in ActionKind]}"]

    role_raw = payload.get("principal_role", PrincipalRole.CUSTOMER.value)
    try:
        role = PrincipalRole(role_raw)
    except ValueError:
        return None, [f"未知 principal_role {role_raw!r}"]

    chain: list[Provenance] = []
    for i, item in enumerate(payload.get("provenance_chain", [])):
        try:
            chain.append(
                Provenance(
                    channel=Channel(item["channel"]),
                    source_ref=str(item.get("source_ref", f"unknown#{i}")),
                )
            )
        except (KeyError, ValueError, TypeError):
            errors.append(f"provenance_chain[{i}] 格式錯誤:{item!r}")
    if errors:
        return None, errors

    request = ActionRequest(
        kind=kind,
        params=dict(payload.get("params", {})),
        principal=str(payload.get("principal", "")),
        principal_role=role,
        agent_id=str(payload.get("agent_id", "")),
        provenance_chain=chain,
        reasoning=str(payload.get("reasoning", "")),
        context=dict(payload.get("context") or {}),
    )
    if payload.get("action_id"):
        request.action_id = str(payload["action_id"])
    if payload.get("trace_id"):
        request.trace_id = str(payload["trace_id"])

    errors = validate_request(request)
    if errors:
        return None, errors
    return request, []


__all__ = ["resolve_action", "validate_request"]
