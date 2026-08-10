"""工廠拓撲與 Knowledge Graph。

規格 §6.1 的最小依賴：Machine A / B → Machine C → Order。
A 異常時，Production Agent 才能真正計算「轉移到 B」之後的產能與交期。

Knowledge Graph 用 networkx 建成 Machine → Line → Product → Order 的有向圖，
Impact Agent 靠走訪這張圖找出受影響的訂單，而不是硬編關係。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import networkx as nx

from ..domain import Machine, MachineKind, Order, Product, SignalSpec

# --------------------------------------------------------------------------------------
# 感測器規格（規格書 §7.1 表格；競賽 Digital Twin 假設值，非任何真實設備商規格）
# scale 的選法：讓「剛好踩到 critical 門檻」的偏離量正規化後等於 1.0。
# --------------------------------------------------------------------------------------
TEMPERATURE = SignalSpec(
    name="temperature", unit="°C", nominal=62.0,
    warning_high=70.0, critical_high=80.0, scale=10.0,
)
VIBRATION = SignalSpec(
    name="vibration", unit="mm/s", nominal=2.4,
    warning_high=4.0, critical_high=7.0, scale=3.0,
)
CURRENT = SignalSpec(
    name="current", unit="A", nominal=10.2,
    warning_low=8.0, warning_high=12.0, critical_high=14.0, scale=2.0,
)
RPM_PCT = SignalSpec(
    name="rpm_pct", unit="%", nominal=99.0,
    warning_low=95.0, critical_low=85.0, scale=10.0,
)

MACHINING_SIGNALS: tuple[SignalSpec, ...] = (TEMPERATURE, VIBRATION, CURRENT, RPM_PCT)
PACKAGING_SIGNALS: tuple[SignalSpec, ...] = (TEMPERATURE, CURRENT, RPM_PCT)

# 健康度權重：振動與溫度最能代表機械劣化。
HEALTH_WEIGHTS: dict[str, float] = {
    "vibration": 0.35,
    "temperature": 0.30,
    "current": 0.20,
    "rpm_pct": 0.15,
}
# 健康度的「全壞」尺度：所有訊號同時踩到 critical 門檻時 S = 1.0，
# 除以 1.6 讓健康度在明顯超過 critical 時才逼近 0。
HEALTH_FULL_SCALE = 1.6

# 每單位產品的邊際貢獻（NTD），用於把產能損失換算成金額。
UNIT_MARGIN_NTD = 185.0


@dataclass(frozen=True)
class ProductionLine:
    line_id: str
    name: str
    # 依序的製程階段；每個階段是「可互相替代」的機台集合
    stages: tuple[tuple[str, ...], ...]


@dataclass
class FactoryTopology:
    machines: dict[str, Machine]
    products: dict[str, Product]
    lines: dict[str, ProductionLine]
    initial_orders: list[Order]
    graph: nx.DiGraph = field(default_factory=nx.DiGraph)

    # -- 查詢 -------------------------------------------------------------------------
    def machine(self, machine_id: str) -> Machine:
        return self.machines[machine_id]

    def line_of(self, machine_id: str) -> ProductionLine | None:
        return self.lines.get(self.machines[machine_id].line_id)

    def stage_index(self, machine_id: str) -> int | None:
        line = self.line_of(machine_id)
        if line is None:
            return None
        for idx, stage in enumerate(line.stages):
            if machine_id in stage:
                return idx
        return None

    def alternates(self, machine_id: str) -> list[str]:
        """同一製程階段中可以互相替代的其他機台。"""
        line = self.line_of(machine_id)
        idx = self.stage_index(machine_id)
        if line is None or idx is None:
            return []
        return [m for m in line.stages[idx] if m != machine_id]

    def downstream(self, machine_id: str) -> list[str]:
        """後續製程階段的所有機台（Machine A → Machine C）。"""
        line = self.line_of(machine_id)
        idx = self.stage_index(machine_id)
        if line is None or idx is None:
            return []
        result: list[str] = []
        for stage in line.stages[idx + 1:]:
            result.extend(stage)
        return result

    def can_produce(self, machine_id: str, product_id: str) -> bool:
        return product_id in self.machines[machine_id].products

    def machines_for_product(self, product_id: str) -> list[str]:
        return [mid for mid, m in self.machines.items() if product_id in m.products and m.kind is MachineKind.MACHINING]

    # -- Knowledge Graph --------------------------------------------------------------
    def build_graph(self, orders: Iterable[Order] | None = None) -> nx.DiGraph:
        g = nx.DiGraph()
        for line in self.lines.values():
            g.add_node(line.line_id, kind="line", name=line.name)
        for mid, m in self.machines.items():
            g.add_node(mid, kind="machine", name=m.name, machine_kind=m.kind.value, rated_rate_uph=m.rated_rate_uph)
            g.add_edge(mid, m.line_id, relation="belongs_to")
        # 製程流：階段 i 的機台 → 階段 i+1 的機台
        for line in self.lines.values():
            for idx in range(len(line.stages) - 1):
                for src in line.stages[idx]:
                    for dst in line.stages[idx + 1]:
                        g.add_edge(src, dst, relation="feeds")
        for pid, p in self.products.items():
            g.add_node(pid, kind="product", name=p.name)
            for stage in p.routing:
                for mid in stage:
                    if mid in g:
                        g.add_edge(mid, pid, relation="produces")
        for order in (orders if orders is not None else self.initial_orders):
            g.add_node(order.order_id, kind="order", quantity=order.quantity, priority=order.priority)
            g.add_edge(order.product_id, order.order_id, relation="fulfills")
        self.graph = g
        return g

    def orders_depending_on(self, machine_id: str, orders: Iterable[Order]) -> list[Order]:
        """走 Knowledge Graph 找出依賴這台機器的訂單（含經由下游機台的依賴）。"""
        if not self.graph:
            self.build_graph(orders)
        reachable = nx.descendants(self.graph, machine_id) if machine_id in self.graph else set()
        order_ids = {n for n in reachable if self.graph.nodes[n].get("kind") == "order"}
        return [o for o in orders if o.order_id in order_ids]

    def graph_json(self) -> dict[str, list[dict[str, object]]]:
        if not self.graph:
            self.build_graph()
        return {
            "nodes": [{"id": n, **{k: v for k, v in d.items()}} for n, d in self.graph.nodes(data=True)],
            "edges": [{"source": u, "target": v, "relation": d.get("relation", "")} for u, v, d in self.graph.edges(data=True)],
        }


# --------------------------------------------------------------------------------------
# 競賽 Demo 的工廠：三台虛擬設備、一條產線、兩種產品
# --------------------------------------------------------------------------------------
def build_factory() -> FactoryTopology:
    machine_a = Machine(
        machine_id="M-A",
        name="Machine A｜CNC 主要加工機",
        kind=MachineKind.MACHINING,
        line_id="LINE-1",
        rated_rate_uph=120.0,
        products=("P-100",),
        signals=MACHINING_SIGNALS,
        changeover_min=10.0,
        repair_min=40.0,
        hourly_cost_ntd=1250.0,
    )
    machine_b = Machine(
        machine_id="M-B",
        name="Machine B｜CNC 替代加工機",
        kind=MachineKind.MACHINING,
        line_id="LINE-1",
        rated_rate_uph=140.0,
        products=("P-100", "P-200"),
        signals=MACHINING_SIGNALS,
        changeover_min=12.0,
        repair_min=45.0,
        hourly_cost_ntd=1180.0,
    )
    machine_c = Machine(
        machine_id="M-C",
        name="Machine C｜後段包裝機",
        kind=MachineKind.PACKAGING,
        line_id="LINE-1",
        rated_rate_uph=200.0,
        products=("P-100", "P-200"),
        signals=PACKAGING_SIGNALS,
        changeover_min=0.0,
        repair_min=25.0,
        hourly_cost_ntd=620.0,
    )

    line = ProductionLine(
        line_id="LINE-1",
        name="Line 1｜精密零件產線",
        stages=(("M-A", "M-B"), ("M-C",)),
    )

    products = {
        "P-100": Product("P-100", "精密軸承座 P-100", routing=(("M-A", "M-B"), ("M-C",))),
        "P-200": Product("P-200", "通用連接件 P-200", routing=(("M-B",), ("M-C",))),
    }

    # MES-like synthetic orders（規格 §7.2：10–20 筆）
    orders = [
        Order("ORD-A001", "P-100", quantity=240, due_in_min=165, priority=1, assigned_machine="M-A"),
        Order("ORD-B004", "P-200", quantity=150, due_in_min=260, priority=2, assigned_machine="M-B"),
        Order("ORD-A002", "P-100", quantity=180, due_in_min=420, priority=2),
        Order("ORD-A003", "P-100", quantity=300, due_in_min=560, priority=2),
        Order("ORD-B005", "P-200", quantity=200, due_in_min=610, priority=3),
        Order("ORD-A004", "P-100", quantity=120, due_in_min=700, priority=2),
        Order("ORD-B006", "P-200", quantity=260, due_in_min=760, priority=3),
        Order("ORD-A005", "P-100", quantity=210, due_in_min=880, priority=2),
        Order("ORD-B007", "P-200", quantity=140, due_in_min=940, priority=3),
        Order("ORD-A006", "P-100", quantity=330, due_in_min=1080, priority=3),
        Order("ORD-B008", "P-200", quantity=175, due_in_min=1160, priority=3),
        Order("ORD-A007", "P-100", quantity=260, due_in_min=1290, priority=3),
    ]

    topo = FactoryTopology(
        machines={m.machine_id: m for m in (machine_a, machine_b, machine_c)},
        products=products,
        lines={line.line_id: line},
        initial_orders=orders,
    )
    topo.build_graph()
    return topo


__all__ = [
    "FactoryTopology",
    "ProductionLine",
    "build_factory",
    "TEMPERATURE",
    "VIBRATION",
    "CURRENT",
    "RPM_PCT",
    "MACHINING_SIGNALS",
    "PACKAGING_SIGNALS",
    "HEALTH_WEIGHTS",
    "HEALTH_FULL_SCALE",
    "UNIT_MARGIN_NTD",
]
