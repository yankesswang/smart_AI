"""AegisMesh 領域模型。

所有數值都是引擎實際計算出來的，不存在預錄結果；LLM 只讀這些物件、不生成它們。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NodeKind(str, Enum):
    WARD = "ward"            # 院內科別／病房接取
    CORE = "core"            # 院內核心交換
    CPE = "cpe"              # 企業端設備（固網／5G）
    VSAT = "vsat"            # 衛星終端
    POP = "pop"              # 中華電信固網 POP
    GNB = "gnb"              # 5G 基地台
    SATGW = "satgw"          # 衛星閘道（海地星空）
    DC = "dc"                # HiCloud 醫療雲／資料中心


class LinkKind(str, Enum):
    LAN = "lan"
    FIBER = "fiber"
    MOBILE_5G = "5g"
    SATELLITE = "satellite"
    BACKBONE = "backbone"


class LinkState(str, Enum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class Severity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Node:
    id: str
    name: str
    kind: NodeKind
    site: str = "hospital"


@dataclass
class Link:
    """一條實體／邏輯鏈路。

    netem_* 欄位是刻意保留的：本機以純 Python 模擬，未來搬到 containerlab 時
    可直接翻譯成 `tc qdisc add dev X root netem delay Yms loss Z%`。
    """

    id: str
    src: str
    dst: str
    kind: LinkKind
    capacity_mbps: float
    base_latency_ms: float
    base_loss_pct: float = 0.0
    cost_per_gb: float = 0.0          # 相對成本，衛星 >> 5G > 固網
    state: LinkState = LinkState.UP
    # 共同風險群組（SRLG）：同一條實體管道／同一個機房／同一組電源。
    # 這是電信實務裡最常被忽略的東西 —— 帳面上的「備援路徑」若與主路徑
    # 共用管道，一鏟子下去兩條一起斷，備援等於不存在。
    duct: str | None = None
    # 事件期間的流量配額（GB）。None = 不限量。
    # 衛星是耗竭性資源：現在多用一分，幾小時後就少一分。
    quota_gb: float | None = None
    # 故障注入疊加值（tc/netem 對應）
    netem_extra_latency_ms: float = 0.0
    netem_extra_loss_pct: float = 0.0
    netem_capacity_factor: float = 1.0  # 0.0~1.0，模擬壅塞造成的可用頻寬縮減

    @property
    def effective_capacity_mbps(self) -> float:
        if self.state is LinkState.DOWN:
            return 0.0
        return max(0.0, self.capacity_mbps * self.netem_capacity_factor)

    @property
    def is_usable(self) -> bool:
        return self.state is not LinkState.DOWN and self.effective_capacity_mbps > 0


@dataclass
class ServiceSLO:
    max_latency_ms: float
    max_loss_pct: float
    min_bandwidth_mbps: float          # 低於此值視為無法提供服務
    required_bandwidth_mbps: float     # 完整體驗所需頻寬


@dataclass
class Service:
    """一個關鍵業務。priority 越小越重要（0 = 生命關鍵）。"""

    id: str
    name: str
    src: str
    dst: str
    priority: int
    slo: ServiceSLO
    clinical_note: str = ""

    @property
    def is_critical(self) -> bool:
        """關鍵業務（P0＋P1）。P1 可降級但仍屬關鍵。"""
        return self.priority <= 1

    @property
    def is_life_critical(self) -> bool:
        """生命關鍵（P0）。斷線直接危及病人安全，任何情況下都不能犧牲。

        與 is_critical 分開，是為了讓系統有「犧牲可降級的 P1、保住 P0 的
        後半場」這個選項。兩者綁在同一條紅線上時，唯一合規的做法就是把
        配額燒光救眼前 —— 那正是人工經驗法則會做的事。
        """
        return self.priority == 0


@dataclass
class ServiceState:
    """引擎針對單一業務算出的即時狀態。"""

    service_id: str
    path: list[str] | None                  # 節點序列；None = 無可用路徑
    link_ids: list[str] = field(default_factory=list)
    admitted_mbps: float = 0.0
    latency_ms: float = float("inf")
    loss_pct: float = 100.0
    reachable: bool = False
    slo_met: bool = False
    violations: list[str] = field(default_factory=list)

    @property
    def health(self) -> Severity:
        if not self.reachable:
            return Severity.CRITICAL
        if not self.slo_met:
            return Severity.WARNING
        return Severity.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "path": self.path,
            "link_ids": self.link_ids,
            "admitted_mbps": round(self.admitted_mbps, 2),
            "latency_ms": None if self.latency_ms == float("inf") else round(self.latency_ms, 2),
            "loss_pct": round(self.loss_pct, 3),
            "reachable": self.reachable,
            "slo_met": self.slo_met,
            "health": self.health.value,
            "violations": self.violations,
        }


@dataclass
class LinkState_Snapshot:
    link_id: str
    state: str
    utilization_pct: float
    load_mbps: float
    capacity_mbps: float
    latency_ms: float
    loss_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "state": self.state,
            "utilization_pct": round(self.utilization_pct, 1),
            "load_mbps": round(self.load_mbps, 1),
            "capacity_mbps": round(self.capacity_mbps, 1),
            "latency_ms": round(self.latency_ms, 2),
            "loss_pct": round(self.loss_pct, 3),
        }


@dataclass
class NetworkSnapshot:
    """某一時刻的完整孿生量測結果。所有欄位皆由引擎計算。"""

    tick: int
    label: str
    services: dict[str, ServiceState]
    links: dict[str, LinkState_Snapshot]
    critical_availability_pct: float
    life_critical_availability_pct: float
    weighted_availability_pct: float
    slo_compliance_pct: float
    monthly_cost_ntd: float
    # 有配額的線路照此流量還能撐幾小時（inf = 沒在用）
    quota_hours_left: dict[str, float] = field(default_factory=dict)

    def violated_services(self) -> list[str]:
        return [sid for sid, st in self.services.items() if not st.slo_met]

    def down_links(self) -> list[str]:
        return [lid for lid, ls in self.links.items() if ls.state == LinkState.DOWN.value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "label": self.label,
            "critical_availability_pct": round(self.critical_availability_pct, 2),
            "life_critical_availability_pct": round(self.life_critical_availability_pct, 2),
            "weighted_availability_pct": round(self.weighted_availability_pct, 2),
            "slo_compliance_pct": round(self.slo_compliance_pct, 2),
            "monthly_cost_ntd": round(self.monthly_cost_ntd, 0),
            "quota_hours_left": {
                k: (None if v == float("inf") else round(v, 2))
                for k, v in self.quota_hours_left.items()
            },
            "services": {k: v.to_dict() for k, v in self.services.items()},
            "links": {k: v.to_dict() for k, v in self.links.items()},
        }


# ---------------------------------------------------------------- 事件與計畫


@dataclass
class Fault:
    """一次故障注入（對應真實環境的 tc/netem 或斷纜）。"""

    link_id: str
    state: LinkState
    extra_latency_ms: float = 0.0
    extra_loss_pct: float = 0.0
    capacity_factor: float = 1.0
    description: str = ""

    def netem_command(self, iface: str = "eth0", capacity_mbps: float | None = None) -> str:
        """輸出「可直接貼進 shell 執行」的 Linux tc 指令。

        這是本機孿生搬到 containerlab／Mininet 的落地介面，所以語法必須是真的合法 ——
        capacity_mbps 由呼叫端從鏈路基準容量帶入，才能把 capacity_factor 換算成
        netem 需要的絕對速率（netem 沒有「打幾折」這種寫法）。
        """
        if self.state is LinkState.DOWN:
            return f"ip link set dev {iface} down   # {self.link_id}"

        parts = [f"tc qdisc replace dev {iface} root netem"]
        if self.extra_latency_ms:
            parts.append(f"delay {self.extra_latency_ms:g}ms")
        if self.extra_loss_pct:
            parts.append(f"loss {self.extra_loss_pct:g}%")
        if self.capacity_factor < 1.0:
            if capacity_mbps is not None:
                parts.append(f"rate {self.capacity_factor * capacity_mbps:g}mbit")
            else:
                # 沒有容量資訊時不要輸出假語法，明講需要什麼
                parts.append(f"rate <{self.capacity_factor:g}×基準容量>mbit")
        return " ".join(parts) + f"   # {self.link_id}"


@dataclass
class TimelineStep:
    """事件推演的一個時段。

    faults 是「此刻起的完整線路狀態」而非增量 —— 搶修完成只要在下一段
    不列出該故障即可，不需要另外設計一種「修復事件」。
    """

    hour: float                      # 這個時段開始的時刻（事件起算，小時）
    label: str
    faults: list[Fault] = field(default_factory=list)
    demand: dict[str, float] = field(default_factory=dict)


@dataclass
class Scenario:
    id: str
    name: str
    narrative: str
    faults: list[Fault]
    # 事件預估持續時間（小時）。衛星配額要撐過這段時間，
    # 「現在誰上衛星」因此變成跨時間的資源配置，而不是當下的取捨。
    duration_hours: float = 12.0
    # 各業務的需求倍率。災害不只打斷線路，也改變需求：
    # 避難收容湧入 → 訪客 Wi-Fi 暴增；大量傷患 → 生命徵象串流變多。
    demand: dict[str, float] = field(default_factory=dict)
    # 多時段推演。空的話視為「整場事件維持 faults/demand 的單一時段」。
    timeline: list[TimelineStep] = field(default_factory=list)

    def steps(self) -> list[TimelineStep]:
        """一律以時段清單的形式取用，呼叫端不必分兩種情況處理。"""
        if self.timeline:
            return sorted(self.timeline, key=lambda s: s.hour)
        return [TimelineStep(0.0, self.name, list(self.faults), dict(self.demand))]


@dataclass
class Action:
    """復原計畫中的單一動作。type 決定引擎如何套用。"""

    type: str          # reroute | throttle | activate_link | restore | admit
    target: str        # service_id 或 link_id
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def describe(self) -> str:
        if self.type == "reroute":
            return f"將 {self.target} 改走 {' → '.join(self.params.get('path', []))}"
        if self.type == "throttle":
            return f"將 {self.target} 頻寬限制至 {self.params.get('mbps')} Mbps"
        if self.type == "activate_link":
            return f"啟用備援鏈路 {self.target}"
        if self.type == "admit":
            return f"保障 {self.target} 頻寬 {self.params.get('mbps')} Mbps"
        return f"{self.type} {self.target}"


@dataclass
class RecoveryPlan:
    id: str
    strategy: str            # protect_critical | lowest_cost | balanced
    summary: str
    actions: list[Action]
    projected: NetworkSnapshot | None = None
    # Policy Agent 填入
    policy_decision: str = "pending"     # allow | require_approval | deny
    policy_findings: list[str] = field(default_factory=list)
    # 同一批發現的結構化版本。核准畫面要能自己決定怎麼排版
    # （例如只顯示具體事由、把規則編號縮到角落），拿到拼好的字串就做不到。
    policy_findings_detail: list[dict[str, Any]] = field(default_factory=list)
    risk_score: float = 0.0
    # Planning/Simulation Agent 填入
    score: float = 0.0
    llm_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "summary": self.summary,
            "actions": [
                {"type": a.type, "target": a.target, "params": a.params, "describe": a.describe()}
                for a in self.actions
            ],
            "policy_decision": self.policy_decision,
            "policy_findings": self.policy_findings,
            "policy_findings_detail": self.policy_findings_detail,
            "risk_score": round(self.risk_score, 2),
            "score": round(self.score, 2),
            "llm_rationale": self.llm_rationale,
            "projected": self.projected.to_dict() if self.projected else None,
        }
