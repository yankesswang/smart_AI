"""Policy Engine：動作權限、Safety 硬規則與人工核准。

對應規格：
* §9.1 Human-in-the-loop 規則表（哪些動作可自動、哪些必須人工核准、哪些禁止）。
* §4.4 Safety Policy：高風險條件下阻止「為了產量繼續運轉」。

這一層是**規則**，不是 LLM。任何方案都必須先通過它才進得了核准與執行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..domain import (
    ActionKind,
    CameraObservation,
    Evidence,
    FactorySnapshot,
    MachineSnapshot,
    PlanProjection,
    SafetyFinding,
    SafetyVerdictKind,
)

# 危險門檻（規格 §7.1）
VIBRATION_CRITICAL = 7.0
TEMPERATURE_FIRE_RISK = 90.0
CURRENT_CRITICAL = 14.0


# --------------------------------------------------------------------------------------
# 動作權限（規格 §9.1）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionPolicy:
    kind: ActionKind
    risk: str                    # low / medium / high / forbidden
    requires_approval: bool
    forbidden: bool = False
    note: str = ""


ACTION_POLICY: dict[ActionKind, ActionPolicy] = {
    ActionKind.RAISE_ALERT: ActionPolicy(ActionKind.RAISE_ALERT, "low", False, note="產生告警：可自動"),
    ActionKind.CREATE_WORK_ORDER: ActionPolicy(ActionKind.CREATE_WORK_ORDER, "low", False, note="產生工單：可自動"),
    ActionKind.UPDATE_SCHEDULE: ActionPolicy(ActionKind.UPDATE_SCHEDULE, "low", False, note="更新模擬排程：低風險可自動"),
    ActionKind.TRANSFER_ORDER: ActionPolicy(ActionKind.TRANSFER_ORDER, "low", False, note="訂單轉移：低風險排程變更，可自動或單鍵核准"),
    ActionKind.DERATE_MACHINE: ActionPolicy(ActionKind.DERATE_MACHINE, "high", True, note="改變機台控制狀態：人工核准"),
    ActionKind.STOP_MACHINE: ActionPolicy(ActionKind.STOP_MACHINE, "high", True, note="停止機台：人工核准"),
    ActionKind.START_MAINTENANCE: ActionPolicy(ActionKind.START_MAINTENANCE, "high", True, note="進入維修狀態：人工核准"),
    ActionKind.SAFETY_OVERRIDE: ActionPolicy(ActionKind.SAFETY_OVERRIDE, "forbidden", True, forbidden=True, note="Safety Override：系統禁止"),
}


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    risk: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "risk": self.risk,
            "reasons": self.reasons,
        }


# --------------------------------------------------------------------------------------
# Safety 硬規則
# --------------------------------------------------------------------------------------
@dataclass
class SafetyContext:
    """Safety 規則評估時看得到的東西。

    ``projection`` 是這個方案在模擬器上乾跑出來的軌跡。有了它，Safety Agent 判斷的是
    「這個方案會把設備帶到哪裡」，而不只是「現在的數值危不危險」——
    早期偵測的價值就在這裡：振動現在才 4.2 mm/s，但維持全速會走到 9 mm/s。
    """

    snapshot: FactorySnapshot
    machine_id: str
    action_kinds: set[ActionKind]
    keeps_machine_running: bool     # 這個方案是否讓目標機台繼續產出（含降速）
    keeps_full_speed: bool          # 是否維持全速
    plan_id: str = ""
    projection: PlanProjection | None = None
    # 到達危險門檻的預估剩餘時間（Monitoring Agent 用可觀測歷史算的，見 prediction/threshold.py）。
    #
    # 這三個欄位**只當證據，不當判準**：預測型規則的裁決仍然以方案乾跑的峰值為主。
    # 理由是責任分界 —— 乾跑峰值是「這個方案會把設備帶到哪裡」，那是方案自己的後果；
    # TTT 是「不介入的話還剩多久」，它讓同一條 BLOCK 讀起來有急迫性，
    # 但如果讓它參與裁決，安全裁決就會跟著一個外推值浮動，那是把預測誤當成事實。
    time_to_threshold_min: float | None = None
    time_to_threshold_signal: str = ""
    time_to_threshold_runtime: str = ""

    @property
    def machine(self) -> MachineSnapshot:
        return self.snapshot.machines[self.machine_id]

    @property
    def camera(self) -> CameraObservation | None:
        for cam in self.snapshot.cameras:
            if cam.machine_id == self.machine_id:
                return cam
        return self.snapshot.cameras[0] if self.snapshot.cameras else None


@dataclass(frozen=True)
class SafetyRule:
    rule_id: str
    title: str
    verdict: SafetyVerdictKind
    sop_ref: str
    check: Callable[[SafetyContext], tuple[bool, str, list[Evidence]]]

    def evaluate(self, ctx: SafetyContext) -> SafetyFinding | None:
        triggered, message, evidence = self.check(ctx)
        if not triggered:
            return None
        return SafetyFinding(rule_id=self.rule_id, verdict=self.verdict, message=message, evidence=evidence)


def _sensor_evidence(machine: MachineSnapshot, signal: str) -> list[Evidence]:
    reading = machine.readings.get(signal)
    if reading is None:
        return []
    return [
        Evidence(
            source="sensor",
            reference=f"{machine.machine_id}.{signal}",
            statement=f"{signal} = {reading.value:.2f} {reading.unit}（{reading.band.value}）",
        )
    ]


def _ttt_evidence(ctx: SafetyContext) -> list[Evidence]:
    """把 TTT 掛成一條**額外證據**。

    只在規則已經觸發之後才加，所以它不改變任何裁決，只讓同一條 BLOCK 多一句
    「而且不介入的話還剩幾分鐘」——那是現場真正要拿去做決定的資訊。
    """
    if ctx.time_to_threshold_min is None:
        return []
    signal = ctx.time_to_threshold_signal or "訊號"
    runtime = ctx.time_to_threshold_runtime or "unknown"
    if ctx.time_to_threshold_min <= 0.0:
        statement = f"{signal} 已經在危險區內（TTT = 0 分鐘；runtime: {runtime}）。"
    else:
        statement = (
            f"依可觀測歷史外推，若不介入，{signal} 約 {ctx.time_to_threshold_min:.0f} 分鐘後"
            f"踩到危險門檻（runtime: {runtime}）。"
        )
    return [Evidence(
        "prediction", f"{ctx.machine_id}.time_to_threshold",
        statement + "此為證據，非裁決依據；裁決仍以方案乾跑峰值為準。",
    )]


def _rule_person_in_hazard_zone(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    cam = ctx.camera
    if cam is None or not cam.person_in_hazard_zone:
        return False, "", []
    if not ctx.keeps_machine_running:
        return False, "", []
    return (
        True,
        f"Camera {cam.camera_id} 偵測到人員進入 {ctx.machine_id} 運轉中危險區（信心 {cam.confidence:.2f}），"
        "本方案仍讓機台維持產出，違反 SOP-SF-01 規範 1。",
        [
            Evidence("vision", cam.camera_id, cam.caption, weight=cam.confidence),
            Evidence("sop", "SOP-SF-01#1", "機台處於 RUNNING 或 DERATED 狀態時，危險區域禁止任何人員進入。"),
        ],
    )


def _rule_ppe(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    cam = ctx.camera
    if cam is None or cam.ppe_compliant or cam.person_count == 0:
        return False, "", []
    return (
        True,
        f"Camera {cam.camera_id} 偵測到人員個人防護具不完整，該人員不得進行機台作業（SOP-SF-01 規範 2）。",
        [Evidence("vision", cam.camera_id, cam.caption, weight=cam.confidence),
         Evidence("sop", "SOP-SF-01#2", "進入作業區必須配戴完整個人防護具。")],
    )


def _rule_vibration_full_speed(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    machine = ctx.machine
    vib = machine.value("vibration")
    if vib is None or not ctx.keeps_full_speed:
        return False, "", []
    if vib <= VIBRATION_CRITICAL:
        return False, "", []
    return (
        True,
        f"{ctx.machine_id} 振動 {vib:.2f} mm/s 已超過危險門檻 {VIBRATION_CRITICAL:g} mm/s，"
        "本方案維持全速運轉，有旋轉件破損與飛散風險（MAN-A-3.2）。",
        _sensor_evidence(machine, "vibration")
        + [Evidence("manual", "MAN-A-3.2", "進入危險區間（>7 mm/s）不建議繼續全速運轉。")],
    )


def _rule_projected_vibration(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    """預測型規則：現在還沒超標，但這個方案會把振動帶進危險區。"""
    proj = ctx.projection
    if proj is None or not ctx.keeps_machine_running:
        return False, "", []
    if proj.peak_vibration <= VIBRATION_CRITICAL:
        return False, "", []
    current = ctx.machine.value("vibration") or 0.0
    if current > VIBRATION_CRITICAL:
        return False, "", []   # 已由 SR-02 涵蓋，不重複告警
    return (
        True,
        f"模擬顯示本方案會讓 {ctx.machine_id} 振動由目前 {current:.2f} mm/s 升至 "
        f"{proj.peak_vibration:.2f} mm/s，超過危險門檻 {VIBRATION_CRITICAL:g} mm/s。"
        "在振動持續上升的情況下維持產出，等同於為了產量接受旋轉件破損風險。",
        [
            Evidence("simulation", f"{ctx.plan_id}.peak_vibration",
                     f"乾跑期間振動峰值 {proj.peak_vibration:.2f} mm/s，最低健康度 {proj.min_health:.0f}。"),
            Evidence("manual", "MAN-A-3.2", "進入危險區間（>7 mm/s）不建議繼續全速運轉。"),
        ] + _ttt_evidence(ctx),
    )


def _rule_temperature_running(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    machine = ctx.machine
    temp = machine.value("temperature")
    if temp is None or not ctx.keeps_machine_running:
        return False, "", []
    if temp <= TEMPERATURE_FIRE_RISK:
        return False, "", []
    return (
        True,
        f"{ctx.machine_id} 溫度 {temp:.1f}°C 超過 {TEMPERATURE_FIRE_RISK:g}°C，有冷卻液氣化與起火風險，"
        "屬立即停機事件（SOP-SF-01 規範 4）。",
        _sensor_evidence(machine, "temperature")
        + [Evidence("sop", "SOP-SF-01#4", "偵測到煙霧、異常高溫（>90°C）或人員跌倒，屬立即停機事件。")],
    )


def _rule_projected_temperature(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    """預測型規則：這個方案會把溫度帶到起火風險區。"""
    proj = ctx.projection
    if proj is None or not ctx.keeps_machine_running:
        return False, "", []
    if proj.peak_temperature <= TEMPERATURE_FIRE_RISK:
        return False, "", []
    current = ctx.machine.value("temperature") or 0.0
    if current > TEMPERATURE_FIRE_RISK:
        return False, "", []
    return (
        True,
        f"模擬顯示本方案會讓 {ctx.machine_id} 溫度由目前 {current:.1f}°C 升至 "
        f"{proj.peak_temperature:.1f}°C，進入冷卻液氣化與起火風險區。",
        [
            Evidence("simulation", f"{ctx.plan_id}.peak_temperature",
                     f"乾跑期間溫度峰值 {proj.peak_temperature:.1f}°C。"),
            Evidence("sop", "SOP-SF-01#4", "異常高溫（>90°C）屬立即停機事件。"),
        ] + _ttt_evidence(ctx),
    )


def _rule_smoke(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    cam = ctx.camera
    if cam is None or not cam.smoke_detected or not ctx.keeps_machine_running:
        return False, "", []
    return (
        True,
        f"Camera {cam.camera_id} 偵測到煙霧，任何維持運轉的方案皆不得執行（SOP-SF-01 規範 4）。",
        [Evidence("vision", cam.camera_id, "偵測到煙霧訊號。", weight=cam.confidence),
         Evidence("sop", "SOP-SF-01#4", "偵測到煙霧屬立即停機事件。")],
    )


def _rule_fall(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    cam = ctx.camera
    if cam is None or not cam.fall_detected:
        return False, "", []
    return (
        True,
        f"Camera {cam.camera_id} 偵測到人員跌倒，立即停機並通報。",
        [Evidence("vision", cam.camera_id, cam.caption, weight=cam.confidence)],
    )


def _rule_maintenance_requires_loto(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    if ActionKind.START_MAINTENANCE not in ctx.action_kinds:
        return False, "", []
    return (
        True,
        f"{ctx.machine_id} 進入維修前必須完成 LOTO 上鎖掛牌並確認機台已停機（SOP-SF-01 規範 3）。"
        "本動作需人工核准與稽核。",
        [Evidence("sop", "SOP-SF-01#3", "任何維修作業前必須完成 LOTO 上鎖掛牌。")],
    )


def _rule_electrical_work_needs_full_stop(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    if ActionKind.START_MAINTENANCE not in ctx.action_kinds:
        return False, "", []
    if not ctx.keeps_machine_running:
        return False, "", []
    return (
        True,
        f"本方案同時要求 {ctx.machine_id} 維修與維持運轉，違反 LOTO 前提，無法執行。",
        [Evidence("sop", "SOP-MT-04#1", "機台必須完全停機，不得以降速方式進行電氣作業。")],
    )


def _rule_projected_current(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    """馬達過載時維持全速有繞組燒毀與電氣火災風險（MAN-A-5.3 明文禁止）。"""
    proj = ctx.projection
    if proj is None or not ctx.keeps_full_speed:
        return False, "", []
    current = ctx.machine.value("current") or 0.0
    if max(current, 0.0) <= CURRENT_CRITICAL and proj.min_health > 40.0:
        return False, "", []
    if proj.min_health > 40.0:
        return False, "", []
    return (
        True,
        f"模擬顯示本方案會讓 {ctx.machine_id} 健康度掉到 {proj.min_health:.0f}（目前電流 {current:.2f} A），"
        "持續全速運轉有驅動器過熱與馬達繞組燒毀風險。",
        [
            Evidence("simulation", f"{ctx.plan_id}.min_health",
                     f"乾跑期間最低健康度 {proj.min_health:.0f}，設備已進入不可接受的劣化區。"),
            Evidence("manual", "MAN-A-5.3", "處置建議：不可持續全速運轉。應停機檢查驅動器與刀具狀態。"),
        ] + _ttt_evidence(ctx),
    )


def _rule_safety_override(ctx: SafetyContext) -> tuple[bool, str, list[Evidence]]:
    if ActionKind.SAFETY_OVERRIDE not in ctx.action_kinds:
        return False, "", []
    return (
        True,
        "方案包含 Safety Override，為系統禁止動作。",
        [Evidence("policy", "SOP-SF-01#5", "Safety Override 為系統禁止動作，任何角色皆不得執行。")],
    )


SAFETY_RULES: tuple[SafetyRule, ...] = (
    SafetyRule("SR-01", "人員進入運轉中危險區", SafetyVerdictKind.BLOCK, "SOP-SF-01", _rule_person_in_hazard_zone),
    SafetyRule("SR-02", "振動超過危險門檻仍全速運轉", SafetyVerdictKind.BLOCK, "MAN-A-3.2", _rule_vibration_full_speed),
    SafetyRule("SR-02P", "方案預測將使振動進入危險區", SafetyVerdictKind.BLOCK, "MAN-A-3.2", _rule_projected_vibration),
    SafetyRule("SR-03", "高溫仍維持運轉", SafetyVerdictKind.BLOCK, "SOP-SF-01", _rule_temperature_running),
    SafetyRule("SR-03P", "方案預測將使溫度進入起火風險區", SafetyVerdictKind.BLOCK, "SOP-SF-01", _rule_projected_temperature),
    SafetyRule("SR-04", "偵測到煙霧仍維持運轉", SafetyVerdictKind.BLOCK, "SOP-SF-01", _rule_smoke),
    SafetyRule("SR-05", "偵測到人員跌倒", SafetyVerdictKind.BLOCK, "SOP-SF-01", _rule_fall),
    SafetyRule("SR-06", "維修同時維持運轉違反 LOTO", SafetyVerdictKind.BLOCK, "SOP-MT-04", _rule_electrical_work_needs_full_stop),
    SafetyRule("SR-07", "Safety Override 禁止", SafetyVerdictKind.BLOCK, "SOP-SF-01", _rule_safety_override),
    SafetyRule("SR-13", "方案預測將使設備劣化到不可接受", SafetyVerdictKind.BLOCK, "MAN-A-5.3", _rule_projected_current),
    SafetyRule("SR-08", "PPE 不完整", SafetyVerdictKind.APPROVAL_REQUIRED, "SOP-SF-01", _rule_ppe),
    SafetyRule("SR-09", "維修需 LOTO 與人工核准", SafetyVerdictKind.APPROVAL_REQUIRED, "SOP-SF-01", _rule_maintenance_requires_loto),
)


class PolicyEngine:
    """動作權限判定 + Safety 規則評估。"""

    def __init__(self, require_approval: bool = True, rules: tuple[SafetyRule, ...] = SAFETY_RULES) -> None:
        self.require_approval = require_approval
        self.rules = rules

    # -- 動作權限 ---------------------------------------------------------------------
    def evaluate_actions(self, action_kinds: list[ActionKind]) -> PolicyDecision:
        reasons: list[str] = []
        risk = "low"
        allowed = True
        needs_approval = False
        order = {"low": 0, "medium": 1, "high": 2, "forbidden": 3}
        for kind in action_kinds:
            policy = ACTION_POLICY[kind]
            if order[policy.risk] > order[risk]:
                risk = policy.risk
            if policy.forbidden:
                allowed = False
                reasons.append(f"{kind.value}：{policy.note}")
                continue
            if policy.requires_approval:
                needs_approval = True
                reasons.append(f"{kind.value}：{policy.note}")
        if not self.require_approval and allowed:
            # 自動核准模式（腳本化 Demo / Benchmark）；仍會完整記錄在稽核軌跡。
            needs_approval = False
            reasons.append("執行於自動核准模式（FG_REQUIRE_APPROVAL=0），核准動作已記錄於稽核軌跡。")
        return PolicyDecision(allowed=allowed, requires_approval=needs_approval, risk=risk, reasons=reasons)

    # -- Safety 規則 ------------------------------------------------------------------
    def evaluate_safety(self, ctx: SafetyContext) -> list[SafetyFinding]:
        findings: list[SafetyFinding] = []
        for rule in self.rules:
            finding = rule.evaluate(ctx)
            if finding is not None:
                findings.append(finding)
        return findings

    @staticmethod
    def combine(findings: list[SafetyFinding]) -> SafetyVerdictKind:
        if any(f.verdict is SafetyVerdictKind.BLOCK for f in findings):
            return SafetyVerdictKind.BLOCK
        if any(f.verdict is SafetyVerdictKind.APPROVAL_REQUIRED for f in findings):
            return SafetyVerdictKind.APPROVAL_REQUIRED
        return SafetyVerdictKind.PASS

    def describe(self) -> dict[str, object]:
        return {
            "require_approval": self.require_approval,
            "rules": [{"rule_id": r.rule_id, "title": r.title, "verdict": r.verdict.value, "sop_ref": r.sop_ref} for r in self.rules],
            "action_policy": [
                {"action": k.value, "risk": p.risk, "requires_approval": p.requires_approval, "forbidden": p.forbidden, "note": p.note}
                for k, p in ACTION_POLICY.items()
            ],
        }


__all__ = [
    "ACTION_POLICY",
    "ActionPolicy",
    "PolicyDecision",
    "PolicyEngine",
    "SafetyContext",
    "SafetyRule",
    "SAFETY_RULES",
]
