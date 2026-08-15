"""Cloud link 狀態 —— Edge / Cloud 分層的執行期開關。

MEC 的核心承諾只有一句話：**廠內網路對外中斷時，工廠的自主閉環仍然跑得完**。
這個模組把那句承諾從提案書的形容詞變成一個可切換、可稽核、可測試的執行期狀態。

這個系統本來就已經做到了那件事 —— 沒有 ``OPENAI_API_KEY`` 時，
``llm.py`` 會自動退回確定性離線敘述器，而偵測、診斷、安全裁決、核准、執行與驗證
本來就都是不需要任何雲端服務的確定性運算。這裡做的是**替它命名並讓它可以被驗證**：

* link **up**：CLOUD 層可用（LLM 敘述、雲端知識庫、事件報告、模型更新）。
* link **down**：
  - EDGE 層完全不受影響 —— Monitoring / Diagnosis 指紋比對 / Safety 硬規則 /
    Policy Engine / Digital Twin / Vision 推論全部照跑。
  - CLOUD 層降級：LLM 敘述改走邊緣確定性敘述器（**數字與證據完全相同**，
    只是文字較制式）；事件報告與模型更新在邊緣排隊，鏈路恢復後補送。
  - 每一次降級都會寫進稽核軌跡（stage=``degrade``），
    這是「可稽核」原則往網路韌性的延伸：降級不是靜悄悄發生的。

切換方式：
* 開機預設：環境變數 ``FG_CLOUD_LINK``（``0`` 代表開機即為斷網模式）。
* 執行期：``POST /api/deployment/link``，或 Dashboard 上的 SIMULATE WAN OUTAGE 按鈕。
* 程式內：``set_cloud_link()`` / ``cloud_link_down()`` context manager。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

#: 稽核軌跡裡代表「雲端能力降級」的 stage 名稱。
DEGRADE_STAGE = "degrade"
#: 稽核軌跡裡代表「鏈路狀態變更」的 stage 名稱。
LINK_STAGE = "cloud_link"


@dataclass
class CloudLinkState:
    """廠區對外（MEC → hicloud）鏈路的當下狀態。"""

    up: bool
    reason: str = "開機預設值"
    actor: str = "system"
    changed_at: str = ""
    transitions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "up": self.up,
            "status": "up" if self.up else "down",
            "reason": self.reason,
            "actor": self.actor,
            "changed_at": self.changed_at,
            "transitions": self.transitions,
            "mode": "cloud-assisted" if self.up else "edge-autonomous",
            "note": (
                "雲端鏈路正常：LLM 敘述、雲端知識庫與事件報告可用。"
                if self.up
                else "雲端鏈路中斷：閉環完全在 MEC 邊緣執行，僅雲端敘述與報告降級。"
            ),
        }


_LOCK = threading.RLock()
_STATE: CloudLinkState | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _boot_default() -> bool:
    """開機預設值來自 ``Settings.cloud_link_up``（``FG_CLOUD_LINK``）。

    這裡刻意延遲匯入 config：``Settings.describe()`` 需要回報**當下**的鏈路狀態，
    因此 config 也會反向延遲匯入本模組。兩邊都在函式內匯入，就不會有匯入迴圈。
    """
    from ..config import get_settings

    return bool(get_settings().cloud_link_up)


def cloud_link() -> CloudLinkState:
    """取得目前的鏈路狀態（第一次呼叫時依設定初始化）。"""
    global _STATE
    with _LOCK:
        if _STATE is None:
            up = _boot_default()
            _STATE = CloudLinkState(up=up, reason="開機預設值（FG_CLOUD_LINK）", changed_at=_now())
        return _STATE


def is_cloud_up() -> bool:
    """雲端鏈路是否可用。這是 EDGE / CLOUD 分層在程式碼裡唯一的判斷點。"""
    return cloud_link().up


def set_cloud_link(
    up: bool,
    reason: str = "",
    actor: str = "operator",
    audit: Any = None,
) -> CloudLinkState:
    """切換鏈路狀態並寫入稽核軌跡。

    ``audit`` 是選用的 :class:`~factory_guardian.audit.AuditLog`；Demo 現場由 API 帶入
    session 的稽核記錄器，讓「評審按下斷網」這件事本身就留在不可否認的紀錄裡。
    """
    with _LOCK:
        state = cloud_link()
        changed = state.up != up
        state.up = up
        state.reason = reason or ("鏈路恢復" if up else "模擬廠區對外鏈路中斷")
        state.actor = actor
        state.changed_at = _now()
        if changed:
            state.transitions += 1
        snapshot = state.to_dict()

    if audit is not None and changed:
        audit.log(
            LINK_STAGE,
            actor,
            link="up" if up else "down",
            reason=snapshot["reason"],
            transitions=snapshot["transitions"],
            note=(
                "雲端鏈路恢復：CLOUD 層能力重新啟用，邊緣期間累積的事件可補送。"
                if up
                else "雲端鏈路中斷：閉環轉為 MEC 邊緣自主模式，控制路徑不受影響。"
            ),
        )
    return cloud_link()


def reset_cloud_link() -> CloudLinkState:
    """把鏈路狀態重設回設定的開機預設值（測試與 ``get_settings(refresh=True)`` 使用）。"""
    global _STATE
    with _LOCK:
        _STATE = None
        return cloud_link()


@contextmanager
def cloud_link_down(reason: str = "測試：模擬廠區對外鏈路中斷", actor: str = "test") -> Iterator[CloudLinkState]:
    """在區塊內模擬斷網，離開時恢復原狀態。"""
    previous = cloud_link()
    prev_up, prev_reason, prev_actor = previous.up, previous.reason, previous.actor
    try:
        yield set_cloud_link(False, reason=reason, actor=actor)
    finally:
        set_cloud_link(prev_up, reason=prev_reason, actor=prev_actor)


__all__ = [
    "CloudLinkState",
    "DEGRADE_STAGE",
    "LINK_STAGE",
    "cloud_link",
    "cloud_link_down",
    "is_cloud_up",
    "reset_cloud_link",
    "set_cloud_link",
]
