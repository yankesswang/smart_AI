"""G1 動作解析(Resolution)。

把自由形式的工具呼叫映射成結構化動作描述子(ActionRequest),
解析受影響主體與範圍。LLM 只允許出現在這一層的「映射」角色,
且其輸出必須通過本模組的 schema 驗證才進得了 G2(規格 §4.3)。

G1 同時是 **provenance 的信任邊界**。整個 G0 建立在「來源鏈是真的」這個前提上,
而來源鏈如果由 LLM 自報,那它就跟 Agent 說「我是管理員」一樣沒有價值。
所以這裡定義一個契約(``attach_runtime_provenance``):

* 來源鏈的 ``tool_output`` 節點由 **harness** 依「這次 tool call 之前 Agent 讀過
  哪些工具回傳與附件」自動附上,Agent 不能少報。
* 確認節點的 ``confirmed_action_hash`` 由 **runtime** 計算,Agent 不能自報。
* LLM 自報的 ``provenance_chain`` 只能**縮小**信任,不能擴大:
  宣稱比 runtime 紀錄更可信的節點一律降級為 runtime 紀錄,並寫一條 finding。

``resolve_action`` 是最後一道清洗:未經 harness 見證的 payload
(沒有 ``_runtime_attested``)自報的 ``confirms`` / ``confirmed_action_hash`` /
``acknowledgements`` 一律清空。真實系統裡這個旗標不是 payload 的一個欄位,
而是「這段資料來自 harness 的記憶體,不是模型輸出的那段 JSON」這個事實;
在單一行程的 Demo 裡以旗標表示,並在邊界強制清洗。
"""

from __future__ import annotations

from typing import Any

from ..ontology import (
    CHANNEL_MAX_RISK,
    ActionKind,
    ActionRequest,
    Channel,
    Evidence,
    Finding,
    PrincipalRole,
    Provenance,
    RISK_ORDER,
    Severity,
    action_fingerprint,
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

# runtime 沒有紀錄、卻被 LLM 宣稱為可信通道的來源,一律降到這個通道。
# 選 user_unverified 而不是 tool_output:它確實「自稱是人下的指令」,
# 只是 runtime 無從驗證 —— 語意對得上,授權上限一樣是 low。
UNVOUCHED_CHANNEL = Channel.USER_UNVERIFIED

RUNTIME_ATTESTED_KEY = "_runtime_attested"


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


# --------------------------------------------------------------------------------------
# runtime provenance 注入契約
# --------------------------------------------------------------------------------------
def _norm_nodes(items: Any) -> list[dict[str, Any]]:
    """把 harness 給的來源清單正規化成 {channel, source_ref} 列表。"""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"source_ref": item})
        elif isinstance(item, dict):
            out.append(dict(item))
    return out


def attach_runtime_provenance(
    payload: dict[str, Any], harness_context: dict[str, Any] | None
) -> tuple[dict[str, Any], list[Finding]]:
    """以 runtime 紀錄改寫 payload 的來源鏈,回傳 (新 payload, findings)。

    ``harness_context`` 是 harness(不是模型)握有的事實:

    ``instruction_sources``
        本次 tool call 的指令來源,例如 ``[{"channel": "user_verified",
        "source_ref": "chat:s-1"}]``。這是 harness 從自己的對話狀態機讀出來的。
    ``tool_outputs``
        這次 tool call **之前** Agent 讀過的工具回傳與附件的 ref 列表。
        harness 逐一附成 ``tool_output`` 節點 —— Agent 少報也沒用。
    ``confirmations``
        使用者/系統的確認事件:``{"source_ref", "confirms", "channel",
        "action": {"kind", "params"}}``。``confirmed_action_hash`` 由本函式
        以 ``action_fingerprint(action.kind, action.params)`` 計算,
        **不接受** harness 或模型直接給的雜湊值。
    ``acknowledgements``
        結構化的「已知悉」事實(例如已於通話中告知違約金)。同樣只認 harness 的紀錄。

    信任只能縮小、不能擴大:LLM 自報的節點若宣稱的通道比 runtime 紀錄更可信,
    以 runtime 為準;runtime 完全沒有紀錄的節點降為 ``user_unverified``。
    兩種情形都寫一條 ``G1-R2`` finding(severity=info),進裁決紀錄與稽核鏈。
    """
    payload = dict(payload)
    findings: list[Finding] = []
    ctx = harness_context or {}

    reported = _norm_nodes(payload.get("provenance_chain"))

    # runtime 的權威紀錄:source_ref → channel
    authoritative: dict[str, Channel] = {}
    for item in _norm_nodes(ctx.get("instruction_sources")):
        ref = str(item.get("source_ref", ""))
        try:
            authoritative[ref] = Channel(item.get("channel", Channel.USER_VERIFIED.value))
        except ValueError:
            authoritative[ref] = UNVOUCHED_CHANNEL
    for item in _norm_nodes(ctx.get("tool_outputs")):
        authoritative[str(item.get("source_ref", ""))] = Channel.TOOL_OUTPUT

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    downgraded: list[str] = []
    unvouched: list[str] = []

    def _emit(ref: str, channel: Channel) -> None:
        if ref in seen:
            return
        seen.add(ref)
        chain.append({"channel": channel.value, "source_ref": ref})

    for item in reported:
        ref = str(item.get("source_ref", ""))
        try:
            claimed = Channel(item.get("channel", UNVOUCHED_CHANNEL.value))
        except ValueError:
            claimed = UNVOUCHED_CHANNEL
        if ref in authoritative:
            truth = authoritative[ref]
            # 只能縮小:LLM 宣稱的通道若比 runtime 紀錄「更不可信」,尊重它(更保守)。
            if RISK_ORDER[CHANNEL_MAX_RISK[claimed]] < RISK_ORDER[CHANNEL_MAX_RISK[truth]]:
                _emit(ref, claimed)
            else:
                if claimed is not truth:
                    downgraded.append(f"{ref}({claimed.value} → {truth.value})")
                _emit(ref, truth)
        else:
            if RISK_ORDER[CHANNEL_MAX_RISK[claimed]] > RISK_ORDER[CHANNEL_MAX_RISK[UNVOUCHED_CHANNEL]]:
                unvouched.append(f"{ref}({claimed.value} → {UNVOUCHED_CHANNEL.value})")
                _emit(ref, UNVOUCHED_CHANNEL)
            else:
                _emit(ref, claimed)

    # runtime 記錄到、但 LLM 沒報的來源:一律補上。少報不能洗白。
    omitted = [ref for ref in authoritative if ref not in seen]
    for ref in omitted:
        _emit(ref, authoritative[ref])

    # 確認節點:指紋由 runtime 算,不接受任何自報值。
    for item in _norm_nodes(ctx.get("confirmations")):
        action = item.get("action") or {}
        fingerprint = action_fingerprint(
            action.get("kind", payload.get("kind", "")),
            dict(action.get("params") or {}),
        )
        try:
            channel = Channel(item.get("channel", Channel.USER_VERIFIED.value))
        except ValueError:
            channel = UNVOUCHED_CHANNEL
        chain.append({
            "channel": channel.value,
            "source_ref": str(item.get("source_ref", "")),
            "confirms": str(item.get("confirms", "")),
            "confirmed_action_hash": fingerprint,
        })

    payload["provenance_chain"] = chain
    payload["acknowledgements"] = dict(ctx.get("acknowledgements") or {})
    payload[RUNTIME_ATTESTED_KEY] = True

    detail: list[Evidence] = []
    if omitted:
        detail.append(Evidence(
            "provenance", "G1-R2",
            "LLM 自報的來源鏈遺漏 runtime 記錄的來源,已補回:" + "、".join(sorted(omitted))))
    if downgraded:
        detail.append(Evidence(
            "provenance", "G1-R2",
            "LLM 宣稱的通道比 runtime 紀錄更可信,已以 runtime 為準:" + "、".join(downgraded)))
    if unvouched:
        detail.append(Evidence(
            "provenance", "G1-R2",
            "LLM 宣稱的來源 runtime 沒有紀錄,已降為 user_unverified:" + "、".join(unvouched)))
    if detail:
        findings.append(Finding(
            rule_id="G1-R2",
            title="來源鏈以 runtime 紀錄為準",
            severity=Severity.INFO,
            message=(
                "Agent 自報的指令來源鏈與 runtime 紀錄不符,已以 runtime 為準"
                "(自報只能縮小信任,不能擴大)。"),
            statute="指令來源鏈由 runtime 記錄;Agent 自報之來源鏈僅得縮小信任範圍,不得擴大。",
            evidence=tuple(detail),
        ))
    return payload, findings


# --------------------------------------------------------------------------------------
def resolve_action(payload: dict[str, Any]) -> tuple[ActionRequest | None, list[str]]:
    """把工具呼叫 payload 解析成 ActionRequest。

    回傳 (request, errors)。解析失敗時 request 為 None。
    這是 API 邊界:任何欄位錯誤都在這裡擋下,不讓畸形輸入進 G2。

    同時是信任邊界:沒有經過 ``attach_runtime_provenance`` 見證的 payload,
    其自報的 ``confirms`` / ``confirmed_action_hash`` / ``acknowledgements``
    一律清空,並寫一條 ``G1-R3`` finding。
    """
    errors: list[str] = []
    attested = bool(payload.get(RUNTIME_ATTESTED_KEY))

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
    stripped: list[str] = []
    for i, item in enumerate(payload.get("provenance_chain", [])):
        try:
            confirms = str(item.get("confirms", "") or "")
            hashed = str(item.get("confirmed_action_hash", "") or "")
            if not attested and (confirms or hashed):
                stripped.append(str(item.get("source_ref", f"unknown#{i}")))
                confirms, hashed = "", ""
            chain.append(
                Provenance(
                    channel=Channel(item["channel"]),
                    source_ref=str(item.get("source_ref", f"unknown#{i}")),
                    confirms=confirms,
                    confirmed_action_hash=hashed,
                )
            )
        except (AttributeError, KeyError, ValueError, TypeError):
            errors.append(f"provenance_chain[{i}] 格式錯誤:{item!r}")
    if errors:
        return None, errors

    acknowledgements = dict(payload.get("acknowledgements") or {})
    if not attested and acknowledgements:
        stripped.append("acknowledgements")
        acknowledgements = {}

    runtime_findings: list[Finding] = list(payload.get("_runtime_findings") or [])
    if stripped:
        runtime_findings.append(Finding(
            rule_id="G1-R3",
            title="清除 Agent 自報的確認欄位",
            severity=Severity.INFO,
            message=(
                "動作請求未經 runtime 見證,其自報的確認欄位("
                + "、".join(sorted(set(stripped)))
                + ")已清空;確認提升不成立。"),
            statute=(
                "確認事件與其動作指紋由 runtime 記錄;Agent 自報之確認欄位一律不予採認。"),
            evidence=(Evidence("provenance", "G1-R3",
                               "confirms / confirmed_action_hash / acknowledgements "
                               "只接受 runtime 見證的值"),),
        ))

    request = ActionRequest(
        kind=kind,
        params=dict(payload.get("params", {})),
        principal=str(payload.get("principal", "")),
        principal_role=role,
        agent_id=str(payload.get("agent_id", "")),
        provenance_chain=chain,
        reasoning=str(payload.get("reasoning", "")),
        context=dict(payload.get("context") or {}),
        acknowledgements=acknowledgements,
        runtime_findings=runtime_findings,
    )
    if payload.get("action_id"):
        request.action_id = str(payload["action_id"])
    if payload.get("trace_id"):
        request.trace_id = str(payload["trace_id"])

    errors = validate_request(request)
    if errors:
        return None, errors
    return request, []


__all__ = [
    "RUNTIME_ATTESTED_KEY",
    "attach_runtime_provenance",
    "resolve_action",
    "validate_request",
]
