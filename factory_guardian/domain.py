"""Factory Guardian 領域模型。

設計原則：這些物件的每個數值都由 Simulator 或規則引擎「算」出來，不存在預錄結果。
LLM 只讀這些物件（並負責敘述與協調），不生成它們。

Ground Truth（真實故障標籤）刻意不放進任何會流向 Agent 的資料結構裡；
它只存在於 twin.engine 的內部狀態與 EpisodeResult 的評分區塊。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable


# --------------------------------------------------------------------------------------
# 列舉
# --------------------------------------------------------------------------------------
class MachineKind(str, Enum):
    MACHINING = "machining"      # 加工機台
    PACKAGING = "packaging"      # 包裝機台


class MachineState(str, Enum):
    RUNNING = "running"
    DERATED = "derated"          # 降速運轉
    IDLE = "idle"
    STOPPED = "stopped"
    MAINTENANCE = "maintenance"


class SignalBand(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


class SafetyVerdictKind(str, Enum):
    PASS = "PASS"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    BLOCK = "BLOCK"


class ActionKind(str, Enum):
    """對應規格 §9.1 的 Human-in-the-loop 分級。"""

    RAISE_ALERT = "raise_alert"                  # 可自動
    CREATE_WORK_ORDER = "create_work_order"      # 可自動
    UPDATE_SCHEDULE = "update_schedule"          # 低風險可自動 / 單鍵核准
    TRANSFER_ORDER = "transfer_order"            # 低風險排程變更
    DERATE_MACHINE = "derate_machine"            # 改變控制狀態 → 人工核准
    STOP_MACHINE = "stop_machine"                # 停機 → 人工核准
    START_MAINTENANCE = "start_maintenance"      # 改變控制狀態 → 人工核准
    SAFETY_OVERRIDE = "safety_override"          # 禁止


# --------------------------------------------------------------------------------------
# 感測器規格（規格書 §7.1，競賽 Digital Twin 假設值）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SignalSpec:
    """單一訊號的正常 / 警告 / 危險區間。

    以 (warning_low, warning_high) 與 (critical_low, critical_high) 表示。
    None 代表該側沒有限制（例如溫度沒有「太低」問題）。
    """

    name: str
    unit: str
    nominal: float
    warning_low: float | None = None
    warning_high: float | None = None
    critical_low: float | None = None
    critical_high: float | None = None
    # 用於健康度正規化的尺度（一個「訊號單位」的嚴重程度）
    scale: float = 1.0

    def band(self, value: float) -> SignalBand:
        if self.critical_high is not None and value > self.critical_high:
            return SignalBand.CRITICAL
        if self.critical_low is not None and value < self.critical_low:
            return SignalBand.CRITICAL
        if self.warning_high is not None and value > self.warning_high:
            return SignalBand.WARNING
        if self.warning_low is not None and value < self.warning_low:
            return SignalBand.WARNING
        return SignalBand.NORMAL

    def deviation(self, value: float) -> float:
        """偏離正常區間的程度，以 scale 正規化；0 代表完全正常。"""
        if self.warning_high is not None and value > self.warning_high:
            return (value - self.warning_high) / self.scale
        if self.warning_low is not None and value < self.warning_low:
            return (self.warning_low - value) / self.scale
        return 0.0

    def describe_range(self) -> str:
        parts = []
        if self.warning_low is not None or self.warning_high is not None:
            lo = f"{self.warning_low:g}" if self.warning_low is not None else "-"
            hi = f"{self.warning_high:g}" if self.warning_high is not None else "-"
            parts.append(f"normal {lo}~{hi}{self.unit}")
        if self.critical_high is not None:
            parts.append(f"critical >{self.critical_high:g}{self.unit}")
        if self.critical_low is not None:
            parts.append(f"critical <{self.critical_low:g}{self.unit}")
        return "；".join(parts)


@dataclass(frozen=True)
class SensorReading:
    signal: str
    value: float
    unit: str
    band: SignalBand

    def to_dict(self) -> dict[str, Any]:
        return {"signal": self.signal, "value": round(self.value, 3), "unit": self.unit, "band": self.band.value}


# --------------------------------------------------------------------------------------
# 工廠拓撲物件
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Machine:
    machine_id: str
    name: str
    kind: MachineKind
    line_id: str
    # 額定產能（件 / 小時）
    rated_rate_uph: float
    # 可生產的產品
    products: tuple[str, ...] = ()
    # 該機台可用的訊號規格
    signals: tuple[SignalSpec, ...] = ()
    # 從其他機台轉單過來需要的換線時間（分鐘）
    changeover_min: float = 0.0
    # 維修一次的標準工時（分鐘）
    repair_min: float = 40.0
    # 每小時運轉成本（NTD）
    hourly_cost_ntd: float = 900.0

    def signal(self, name: str) -> SignalSpec | None:
        for spec in self.signals:
            if spec.name == name:
                return spec
        return None


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    # 需要經過的機台階段：每個階段是可互相替代的機台集合
    routing: tuple[tuple[str, ...], ...] = ()


@dataclass
class Order:
    order_id: str
    product_id: str
    quantity: int
    due_in_min: float           # 距離交期還有幾分鐘（Demo 用相對時間）
    priority: int = 2           # 1 = 最高
    produced: float = 0.0       # 連續量：模擬器以「件/小時 × 分鐘」累積
    assigned_machine: str | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.produced)

    @property
    def done(self) -> bool:
        return self.produced >= self.quantity - 1e-6

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "produced": round(self.produced, 1),
            "remaining": round(self.remaining, 1),
            "due_in_min": round(self.due_in_min, 1),
            "priority": self.priority,
            "assigned_machine": self.assigned_machine,
        }


# --------------------------------------------------------------------------------------
# Simulator 快照（Agent 可見的世界）
# --------------------------------------------------------------------------------------
@dataclass
class MachineSnapshot:
    machine_id: str
    name: str
    state: MachineState
    readings: dict[str, SensorReading]
    health: float                     # 0~100
    production_rate_uph: float
    utilization_pct: float
    queue: int = 0
    # 剩餘維修時間（分鐘），僅在 MAINTENANCE 狀態有意義
    maintenance_remaining_min: float = 0.0
    # 機台是否在線（RUNNING / DERATED / IDLE）。停機與維修中的機台
    # 感測器讀值會掉到接近零，那不是異常，是「沒在跑」——
    # 拿運轉中的門檻去判讀它，會把一台正在被修的機器一直判成 CRITICAL。
    online: bool = True

    @property
    def worst_band(self) -> SignalBand:
        order = {SignalBand.NORMAL: 0, SignalBand.WARNING: 1, SignalBand.CRITICAL: 2}
        worst = SignalBand.NORMAL
        for reading in self.readings.values():
            if order[reading.band] > order[worst]:
                worst = reading.band
        return worst

    def value(self, signal: str) -> float | None:
        reading = self.readings.get(signal)
        return reading.value if reading else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "name": self.name,
            "state": self.state.value,
            "online": self.online,
            "health": round(self.health, 1),
            "production_rate_uph": round(self.production_rate_uph, 1),
            "utilization_pct": round(self.utilization_pct, 1),
            "queue": self.queue,
            "worst_band": self.worst_band.value,
            "maintenance_remaining_min": round(self.maintenance_remaining_min, 1),
            "readings": {k: v.to_dict() for k, v in self.readings.items()},
        }


@dataclass
class CameraObservation:
    """Safety Camera 的一格觀測（由模擬 VLM 或真實 VLM 產生）。"""

    camera_id: str
    zone_id: str
    machine_id: str | None
    person_count: int
    person_in_hazard_zone: bool
    ppe_compliant: bool
    fall_detected: bool
    smoke_detected: bool
    confidence: float
    caption: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FactorySnapshot:
    """Agent 唯一能看到的世界狀態。這裡沒有任何 Ground Truth 欄位。"""

    tick: int
    sim_minutes: float
    machines: dict[str, MachineSnapshot]
    orders: dict[str, Order]
    cameras: list[CameraObservation]
    factory_health: float
    production_pct: float             # 相對於名目產能的達成率
    ambient_temp_c: float = 28.0
    label: str = "live"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "sim_minutes": round(self.sim_minutes, 1),
            "label": self.label,
            "factory_health": round(self.factory_health, 1),
            "production_pct": round(self.production_pct, 1),
            "ambient_temp_c": round(self.ambient_temp_c, 1),
            "machines": {k: v.to_dict() for k, v in self.machines.items()},
            "orders": {k: v.to_dict() for k, v in self.orders.items()},
            "cameras": [c.to_dict() for c in self.cameras],
        }


# --------------------------------------------------------------------------------------
# Agent 產出物
# --------------------------------------------------------------------------------------
@dataclass
class Evidence:
    """任何結論都必須附證據，且證據要能指回來源。"""

    source: str          # sensor / manual / history / rule / vision / topology
    reference: str       # 例如 "MAN-A-3.2" 或 "MH-014"
    statement: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "reference": self.reference, "statement": self.statement, "weight": round(self.weight, 3)}


@dataclass
class AnomalyEvent:
    event_id: str
    machine_id: str
    tick: int
    sim_minutes: float
    severity: Severity
    triggers: list[str]
    health: float
    readings: dict[str, SensorReading]
    detector: str = "monitoring-agent"
    # "equipment" = 感測器偵測到的設備異常；"safety" = 影像/環境偵測到的工安事件。
    kind: str = "equipment"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "machine_id": self.machine_id,
            "tick": self.tick,
            "sim_minutes": round(self.sim_minutes, 1),
            "severity": self.severity.value,
            "kind": self.kind,
            "triggers": self.triggers,
            "health": round(self.health, 1),
            "detector": self.detector,
            "readings": {k: v.to_dict() for k, v in self.readings.items()},
        }


@dataclass
class RootCauseCandidate:
    fault_id: str
    label: str
    confidence: float
    evidence: list[Evidence]
    recommended_actions: list[str] = field(default_factory=list)
    # 排名的四個原始訊號與加權後的合分（manual / differential / prior / docs / combined）。
    # 信心度本身是 softmax 後的結果，看不出它是怎麼來的；沒有這份明細，
    # 畫面上就只剩一個「88%」，講不出「88% 是手冊區間符合度換來的」。
    scores: dict[str, float] = field(default_factory=dict)
    # 逐項明細：哪個訊號落在手冊區間內、哪條鑑別規則成立。
    # 這是「可稽核」的實質內容 —— 評審能逐條核對每個數字對應手冊的哪一行。
    range_hits: list[dict[str, Any]] = field(default_factory=list)
    diff_hits: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fault_id": self.fault_id,
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "evidence": [e.to_dict() for e in self.evidence],
            "recommended_actions": self.recommended_actions,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "range_hits": self.range_hits,
            "diff_hits": self.diff_hits,
        }


@dataclass
class Diagnosis:
    machine_id: str
    candidates: list[RootCauseCandidate]
    narrative: str
    latency_ms: float = 0.0
    llm_mode: str = "offline"
    # --- 推理過程（讓前端能重現排名是怎麼算出來的）---------------------------------
    signal_strength: float = 0.0                                   # 觀測偏離向量的長度
    weights: dict[str, float] = field(default_factory=dict)        # 四個排名訊號的權重
    thresholds: dict[str, float] = field(default_factory=dict)     # 訊號強度的兩個門檻
    observations: list[dict[str, Any]] = field(default_factory=list)  # 每個訊號的觀測值與偏離量
    baseline_ref: str = ""                                         # 判讀所依據的交機驗收記錄
    # 敘述的結構化版本：前端直接排成條列，不必再去切 narrative 這段長文。
    # 每筆為 {"label": 中文短標題, "value": 主要數值/結論, "detail": 補充說明}。
    summary_points: list[dict[str, Any]] = field(default_factory=list)

    @property
    def top(self) -> RootCauseCandidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "top_fault_id": self.top.fault_id if self.top else None,
            "top_confidence": round(self.top.confidence, 3) if self.top else 0.0,
            "candidates": [c.to_dict() for c in self.candidates],
            "narrative": self.narrative,
            "latency_ms": round(self.latency_ms, 1),
            "llm_mode": self.llm_mode,
            "signal_strength": round(self.signal_strength, 3),
            "weights": self.weights,
            "thresholds": self.thresholds,
            "observations": self.observations,
            "baseline_ref": self.baseline_ref,
            "summary_points": self.summary_points,
        }


@dataclass
class OrderImpact:
    order_id: str
    product_id: str
    remaining: int
    baseline_finish_min: float
    projected_finish_min: float
    due_in_min: float
    delay_min: float
    at_risk: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "product_id": self.product_id,
            "remaining": self.remaining,
            "baseline_finish_min": round(self.baseline_finish_min, 1),
            "projected_finish_min": round(self.projected_finish_min, 1),
            "due_in_min": round(self.due_in_min, 1),
            "delay_min": round(self.delay_min, 1),
            "at_risk": self.at_risk,
        }


@dataclass
class ImpactAssessment:
    machine_id: str
    downstream_machines: list[str]
    affected_orders: list[OrderImpact]
    production_loss_units: float
    production_loss_pct: float
    total_delay_min: float
    narrative: str = ""
    # 同 Diagnosis.summary_points：影響摘要的結構化版本，供前端條列。
    summary_points: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "downstream_machines": self.downstream_machines,
            "affected_orders": [o.to_dict() for o in self.affected_orders],
            "production_loss_units": round(self.production_loss_units, 1),
            "production_loss_pct": round(self.production_loss_pct, 1),
            "total_delay_min": round(self.total_delay_min, 1),
            "narrative": self.narrative,
            "summary_points": self.summary_points,
        }


@dataclass
class Action:
    kind: ActionKind
    target: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def describe(self) -> str:
        detail = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.kind.value}({self.target}{', ' + detail if detail else ''})"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "target": self.target, "params": self.params, "rationale": self.rationale}


@dataclass
class PlanProjection:
    """由 Simulator 的乾跑（dry-run）算出的方案結果，不是 LLM 猜的。"""

    production_pct: float            # 整段規劃視野的平均達成率（用於方案排名）
    production_pct_final: float      # 視野結束時的達成率（用於和執行後的量測值對照）
    production_loss_units: float
    max_order_delay_min: float
    recovery_min: float
    cost_ntd: float
    residual_risk: float          # 0~1，設備繼續劣化 / 二次損壞的風險
    machine_health_after: float
    # --- 軌跡（整段乾跑期間的極值）：Safety Agent 用來判斷「這個方案會走到哪裡」---
    peak_vibration: float = 0.0
    peak_temperature: float = 0.0
    min_health: float = 100.0
    recovered: bool = True
    hazard_while_running: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "production_pct": round(self.production_pct, 1),
            "production_pct_final": round(self.production_pct_final, 1),
            "production_loss_units": round(self.production_loss_units, 1),
            "max_order_delay_min": round(self.max_order_delay_min, 1),
            "recovery_min": round(self.recovery_min, 1),
            "recovered": self.recovered,
            "cost_ntd": round(self.cost_ntd, 0),
            "residual_risk": round(self.residual_risk, 3),
            "machine_health_after": round(self.machine_health_after, 1),
            "peak_vibration": round(self.peak_vibration, 2),
            "peak_temperature": round(self.peak_temperature, 1),
            "min_health": round(self.min_health, 1),
            "hazard_while_running": self.hazard_while_running,
        }


@dataclass
class SafetyFinding:
    rule_id: str
    verdict: SafetyVerdictKind
    message: str
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "verdict": self.verdict.value,
            "message": self.message,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class SafetyReview:
    plan_id: str
    verdict: SafetyVerdictKind
    findings: list[SafetyFinding]
    required_sop: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict is SafetyVerdictKind.BLOCK

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "verdict": self.verdict.value,
            "blocked": self.blocked,
            "required_sop": self.required_sop,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class RecoveryPlan:
    plan_id: str
    title: str
    summary: str
    actions: list[Action]
    projection: PlanProjection
    # 以下欄位在後續 pipeline 補上
    safety: SafetyReview | None = None
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    rank: int = 0
    feasible: bool = True
    infeasible_reason: str = ""

    @property
    def requires_approval(self) -> bool:
        from .policy.engine import ACTION_POLICY  # 局部匯入避免循環相依

        return any(ACTION_POLICY[a.kind].requires_approval for a in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "title": self.title,
            "summary": self.summary,
            "actions": [a.to_dict() for a in self.actions],
            "projection": self.projection.to_dict(),
            "safety": self.safety.to_dict() if self.safety else None,
            "score": round(self.score, 4),
            "score_breakdown": {k: round(v, 4) for k, v in self.score_breakdown.items()},
            "rank": self.rank,
            "feasible": self.feasible,
            "infeasible_reason": self.infeasible_reason,
            "requires_approval": self.requires_approval,
        }


@dataclass
class WorkOrder:
    work_order_id: str
    machine_id: str
    problem: str
    priority: str                 # P1 / P2 / P3
    required_skill: str
    suggested_parts: list[str]
    estimated_repair_min: float
    sop_refs: list[str]
    evidence: list[Evidence]
    safety_precautions: list[str] = field(default_factory=list)
    status: str = "open"

    def completeness(self) -> float:
        """Work Order Completeness KPI：規格 §10 要求可量測。"""
        fields = [
            bool(self.machine_id),
            bool(self.problem),
            bool(self.priority),
            bool(self.required_skill),
            bool(self.suggested_parts),
            self.estimated_repair_min > 0,
            bool(self.sop_refs),
            bool(self.evidence),
            bool(self.safety_precautions),
        ]
        return 100.0 * sum(fields) / len(fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.work_order_id,
            "machine_id": self.machine_id,
            "problem": self.problem,
            "priority": self.priority,
            "required_skill": self.required_skill,
            "suggested_parts": self.suggested_parts,
            "estimated_repair_min": round(self.estimated_repair_min, 1),
            "sop_refs": self.sop_refs,
            "safety_precautions": self.safety_precautions,
            "evidence": [e.to_dict() for e in self.evidence],
            "status": self.status,
            "completeness_pct": round(self.completeness(), 1),
        }


@dataclass
class ApprovalDecision:
    plan_id: str
    approved: bool
    approver: str
    reason: str = ""
    auto: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "approved": self.approved,
            "approver": self.approver,
            "reason": self.reason,
            "auto": self.auto,
        }


@dataclass
class VerificationCheck:
    name: str
    expected: float
    actual: float
    tolerance: float
    passed: bool
    comparator: str = ">="

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected": round(self.expected, 2),
            "actual": round(self.actual, 2),
            "tolerance": round(self.tolerance, 2),
            "comparator": self.comparator,
            "passed": self.passed,
        }


@dataclass
class VerificationReport:
    plan_id: str
    passed: bool
    checks: list[VerificationCheck]
    before: dict[str, float]
    after: dict[str, float]
    narrative: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "before": {k: round(v, 2) for k, v in self.before.items()},
            "after": {k: round(v, 2) for k, v in self.after.items()},
            "narrative": self.narrative,
        }


# --------------------------------------------------------------------------------------
# 情境定義
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FaultInjection:
    """把故障注入 Simulator：只改變狀態與 Sensor 生成規則，不把標籤傳給 Agent。"""

    fault_id: str
    machine_id: str
    start_tick: int
    ramp_ticks: int = 8
    max_progress: float = 1.0


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    title: str
    description: str
    injections: tuple[FaultInjection, ...]
    ground_truth: dict[str, str]     # machine_id -> fault_id
    horizon_ticks: int = 24
    expected_narrative: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "description": self.description,
            "horizon_ticks": self.horizon_ticks,
            "injections": [
                {
                    "fault_id": i.fault_id,
                    "machine_id": i.machine_id,
                    "start_tick": i.start_tick,
                    "ramp_ticks": i.ramp_ticks,
                }
                for i in self.injections
            ],
        }


def worst_severity(items: Iterable[Severity]) -> Severity:
    worst = Severity.INFO
    for s in items:
        if s.rank > worst.rank:
            worst = s
    return worst


__all__ = [name for name in dir() if not name.startswith("_")]
