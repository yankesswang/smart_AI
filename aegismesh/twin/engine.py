"""數位孿生引擎：真正做流量、壅塞、SLA 與成本計算的地方。

設計原則（對應提案「LLM 負責理解，演算法負責可驗證決策」）：
  * 這裡完全沒有 LLM。所有數字都由確定性模型算出，可重現、可稽核。
  * clone() 讓 Simulation Agent 能在「影子孿生」推演，不影響線上狀態 —
    這就是「先推演、後執行」的技術實作。
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field

import networkx as nx

from ..domain import (
    Action,
    Fault,
    Link,
    LinkState,
    LinkState_Snapshot,
    NetworkSnapshot,
    Node,
    RecoveryPlan,
    Service,
    ServiceState,
)
from .topology import build_links, build_nodes, build_services

# 一個月的秒數 ÷ 8 bits ÷ 1000 → Mbps 換算成 GB/月
_MBPS_TO_GB_PER_MONTH = 2_592_000 / 8 / 1000  # = 324.0

# 對外線路的白話名稱（給人看的介面用，稽核軌跡仍存 LinkKind 原值）
WAN_LABEL = {"fiber": "固網", "5g": "5G", "satellite": "衛星"}

# 鏈路的白話名稱。送進 LLM 的事實清單若只給 `w-fiber` 這種代號，
# 模型就會照抄進敘述裡，畫面上最顯眼的那段文字反而變成最難懂的部分。
_LINK_LABEL = {
    "lan": "院內線路", "fiber": "固網主線", "5g": "5G 線路",
    "satellite": "衛星線路", "backbone": "電信骨幹",
}


@dataclass
class RoutingTable:
    """service_id → (節點路徑, 允入頻寬 Mbps)。這是 Agent 唯一能改動的控制面。"""

    paths: dict[str, list[str]] = field(default_factory=dict)
    admitted: dict[str, float] = field(default_factory=dict)


class DigitalTwin:
    def __init__(
        self,
        nodes: list[Node] | None = None,
        links: list[Link] | None = None,
        services: list[Service] | None = None,
    ) -> None:
        self.nodes = {n.id: n for n in (nodes or build_nodes())}
        self.links = {l.id: l for l in (links or build_links())}
        self.services = {s.id: s for s in (services or build_services())}
        self.routing = RoutingTable()
        self.tick = 0
        # 災害需求倍率與已耗用的配額：兩者都是「情境狀態」而非拓樸定義
        self.demand: dict[str, float] = {}
        self.quota_used_gb: dict[str, float] = {}
        self.event_hours: float = 12.0
        self._graph = self._build_graph()
        self.reset_routing()

    # ------------------------------------------------------------- 圖與路徑

    def _build_graph(self) -> nx.Graph:
        g = nx.Graph()
        for node in self.nodes.values():
            g.add_node(node.id, kind=node.kind.value, name=node.name)
        for link in self.links.values():
            g.add_edge(link.src, link.dst, link_id=link.id, weight=link.base_latency_ms)
        return g

    def link_between(self, a: str, b: str) -> Link | None:
        data = self._graph.get_edge_data(a, b)
        return self.links[data["link_id"]] if data else None

    def path_links(self, path: list[str]) -> list[Link]:
        out: list[Link] = []
        for a, b in zip(path, path[1:]):
            link = self.link_between(a, b)
            if link is None:
                return []
            out.append(link)
        return out

    def candidate_paths(self, service_id: str, k: int = 4) -> list[list[str]]:
        """列出前 k 條可行路徑，搜尋時就排除不可用鏈路。

        舊作法先列舉 ``k * 3`` 條路徑再過濾；當多條較短路徑同時中斷時，
        可能漏掉排序較後但仍可用的路徑，並浪費時間探索已知不可用的鏈路。
        """
        svc = self.services[service_id]
        usable_graph = nx.subgraph_view(
            self._graph,
            filter_edge=lambda a, b: self.links[self._graph[a][b]["link_id"]].is_usable,
        )
        try:
            gen = nx.shortest_simple_paths(usable_graph, svc.src, svc.dst, weight="weight")
            return list(itertools.islice(gen, k))
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

    # ------------------------------------------------------------- 控制面操作

    def reset_routing(self) -> None:
        """預設：每個業務走最低延遲的可用路徑，並允入其完整所需頻寬。"""
        self.routing = RoutingTable()
        for sid in self.services:
            paths = self.candidate_paths(sid)
            self.routing.paths[sid] = paths[0] if paths else []
            self.routing.admitted[sid] = self.required_mbps(sid)

    def apply_scenario(self, scenario) -> None:
        """注入災害：線路故障 ＋ 需求變動 ＋ 事件持續時間。"""
        self.demand = dict(scenario.demand)
        self.event_hours = scenario.duration_hours
        self.apply_faults(scenario.faults)

    def apply_faults(self, faults: list[Fault]) -> None:
        for f in faults:
            link = self.links.get(f.link_id)
            if link is None:
                raise KeyError(f"未知鏈路：{f.link_id}")
            link.state = f.state
            link.netem_extra_latency_ms = f.extra_latency_ms
            link.netem_extra_loss_pct = f.extra_loss_pct
            link.netem_capacity_factor = f.capacity_factor

    def clear_faults(self) -> None:
        for link in self.links.values():
            link.state = LinkState.UP
            link.netem_extra_latency_ms = 0.0
            link.netem_extra_loss_pct = 0.0
            link.netem_capacity_factor = 1.0
        self.demand = {}
        self.quota_used_gb = {}

    # --------------------------------------------------- 需求／配額／共同風險

    def required_mbps(self, service_id: str) -> float:
        """含災害需求倍率的完整需求頻寬。

        災害不只打斷線路，也改變需求：避難收容湧入會讓訪客 Wi-Fi 需求暴增，
        大量傷患會讓生命徵象串流變多。倍率存在孿生上而不是改 Service 物件，
        因為 clone() 共用 Service 定義，就地修改會污染線上狀態。
        """
        return self.services[service_id].slo.required_bandwidth_mbps * self.demand.get(service_id, 1.0)

    def min_mbps(self, service_id: str) -> float:
        return self.services[service_id].slo.min_bandwidth_mbps * self.demand.get(service_id, 1.0)

    def quota_hours_left(self, load: dict[str, float] | None = None) -> dict[str, float]:
        """有配額的線路照目前流量還能撐幾小時。

        這是把「耗竭性資源」變成可判斷數字的關鍵一步：光看「衛星 75% 使用率」
        看不出問題，換算成「3.1 小時後配額歸零，但災害還要 12 小時」才看得出來。
        """
        load = self._link_load() if load is None else load
        out: dict[str, float] = {}
        for lid, link in self.links.items():
            if link.quota_gb is None:
                continue
            used = max(load.get(lid, 0.0), 0.0)
            remaining = max(link.quota_gb - self.quota_used_gb.get(lid, 0.0), 0.0)
            # Mbps → GB/小時：×3600 秒 ÷ 8 bits ÷ 1000
            gb_per_hour = used * 3600 / 8 / 1000
            out[lid] = float("inf") if gb_per_hour <= 1e-9 else remaining / gb_per_hour
        return out

    def shared_duct_risk(self, path_a: list[str], path_b: list[str]) -> set[str]:
        """兩條路徑共用的管道。非空 = 帳面上的備援其實會一起斷。"""
        ducts = lambda p: {l.duct for l in self.path_links(p) if l.duct}
        return ducts(path_a) & ducts(path_b)

    def link_label(self, link_id: str) -> str:
        """鏈路的白話名稱，例如 `w-fiber` → 「固網主線」。

        同類型鏈路不只一條時（院內四條 LAN），補上目的節點以免混淆。
        """
        link = self.links.get(link_id)
        if link is None:
            return link_id
        base = _LINK_LABEL.get(link.kind.value, link.kind.value)
        same_kind = [l for l in self.links.values() if l.kind is link.kind]
        if len(same_kind) > 1:
            return f"{base}（{self.nodes[link.dst].name}）"
        return base

    def describe_action(self, action: Action) -> str:
        """給人看的動作說明：用業務名稱與線路種類，不用內部代號。

        `Action.describe()` 輸出的是 `將 svc-ed-vitals 改走 ward-ed → core-sw → …`，
        對維運工程師精確，但對非技術讀者等於天書。這裡改用「急診生命徵象即時串流
        改走 5G」這種講法 —— 原始 ID 與完整路徑仍完整保留在 `RecoveryPlan.to_dict()`
        寫進稽核軌跡的 `target` / `params` 裡，可讀性不會犧牲掉精確性。
        """
        svc = self.services.get(action.target)
        name = svc.name if svc else action.target
        mbps = f"{float(action.params.get('mbps', 0.0)):g}"

        if action.type == "reroute":
            wan = WAN_LABEL.get(action.params.get("wan", ""), "其他線路")
            # 中英文之間補空格是繁中排版慣例：「改走 5G」對、「改走固網」也對
            return f"「{name}」改走{' ' if wan[:1].isascii() else ''}{wan}"
        if action.type == "throttle":
            was = f"{float(action.params.get('was_mbps', 0.0)):g}"
            return f"「{name}」限速至 {mbps} Mbps（原 {was} Mbps）"
        if action.type == "admit":
            return f"「{name}」保障頻寬 {mbps} Mbps"
        if action.type == "activate_link":
            link = self.links.get(action.target)
            return f"啟用備援線路：{WAN_LABEL.get(link.kind.value, action.target) if link else action.target}"
        return action.describe()

    def apply_plan(self, plan: RecoveryPlan) -> list[str]:
        """把計畫套用到控制面，回傳實際生效的動作描述（供稽核與畫面）。"""
        applied: list[str] = []
        for action in plan.actions:
            if action.type == "reroute":
                path = action.params.get("path") or []
                if path and self.path_links(path):
                    self.routing.paths[action.target] = list(path)
                    applied.append(self.describe_action(action))
            elif action.type in {"throttle", "admit"}:
                mbps = float(action.params.get("mbps", 0.0))
                self.routing.admitted[action.target] = mbps
                applied.append(self.describe_action(action))
            elif action.type == "activate_link":
                link = self.links.get(action.target)
                if link and link.state is LinkState.DOWN:
                    link.state = LinkState.UP
                    applied.append(self.describe_action(action))
        return applied

    def clone(self) -> "DigitalTwin":
        """影子孿生：Simulation Agent 在這上面推演，線上狀態零風險。"""
        twin = DigitalTwin.__new__(DigitalTwin)
        twin.nodes = self.nodes                      # 節點不可變，可共用
        twin.links = copy.deepcopy(self.links)
        twin.services = self.services                # 業務定義不可變，可共用
        twin.routing = copy.deepcopy(self.routing)
        twin.tick = self.tick
        # 需求倍率與已耗用配額屬於情境狀態，影子孿生必須帶著走，
        # 否則推演會以為配額還是滿的、需求還是平常水準。
        twin.demand = dict(self.demand)
        twin.quota_used_gb = dict(self.quota_used_gb)
        twin.event_hours = self.event_hours
        twin._graph = self._graph                    # 拓樸不變，可共用
        return twin

    # ------------------------------------------------------------- 量測計算

    def _link_load(self) -> dict[str, float]:
        load = {lid: 0.0 for lid in self.links}
        for sid, path in self.routing.paths.items():
            if not path:
                continue
            mbps = self.routing.admitted.get(sid, 0.0)
            for link in self.path_links(path):
                load[link.id] += mbps
        return load

    @staticmethod
    def _congested_metrics(link: Link, load_mbps: float) -> tuple[float, float, float]:
        """回傳 (延遲 ms, 損失率 %, 使用率 %)。

        以 M/M/1 排隊近似模擬壅塞：利用率越高，延遲非線性上升；
        超過 100% 的部分視為丟包。這是網路工程的標準一階近似。
        """
        capacity = link.effective_capacity_mbps
        base_latency = link.base_latency_ms + link.netem_extra_latency_ms
        base_loss = link.base_loss_pct + link.netem_extra_loss_pct

        if capacity <= 0:
            return float("inf"), 100.0, 100.0

        rho = load_mbps / capacity
        if rho <= 1.0:
            # 排隊延遲：base * rho/(1-rho)，並夾住避免趨近無窮
            queue = base_latency * (rho / max(1.0 - rho, 0.05))
            latency = base_latency + min(queue, base_latency * 19)
            # 接近滿載時開始出現尾端丟包
            loss = base_loss + (max(0.0, rho - 0.85) / 0.15) ** 2 * 1.5
        else:
            latency = base_latency * 20
            loss = base_loss + 100.0 * (rho - 1.0) / rho
        return latency, min(loss, 100.0), rho * 100.0

    def evaluate(self, label: str = "current") -> NetworkSnapshot:
        load = self._link_load()
        link_metrics: dict[str, tuple[float, float, float]] = {}
        link_snaps: dict[str, LinkState_Snapshot] = {}

        for lid, link in self.links.items():
            latency, loss, util = self._congested_metrics(link, load[lid])
            link_metrics[lid] = (latency, loss, util)
            link_snaps[lid] = LinkState_Snapshot(
                link_id=lid,
                state=link.state.value,
                utilization_pct=util,
                load_mbps=load[lid],
                capacity_mbps=link.effective_capacity_mbps,
                latency_ms=latency if latency != float("inf") else 9999.0,
                loss_pct=loss,
            )

        svc_states: dict[str, ServiceState] = {}
        for sid, svc in self.services.items():
            svc_states[sid] = self._evaluate_service(svc, link_metrics)

        crit = [s for sid, s in svc_states.items() if self.services[sid].is_critical]
        crit_avail = 100.0 * sum(1 for s in crit if s.slo_met) / len(crit) if crit else 100.0

        # 加權可用率：優先級越高權重越大（權重 = 1/(priority+1)）
        total_w = sum(1.0 / (s.priority + 1) for s in self.services.values())
        weighted = sum(
            (1.0 / (self.services[sid].priority + 1)) * (1.0 if st.slo_met else 0.0)
            for sid, st in svc_states.items()
        )
        weighted_avail = 100.0 * weighted / total_w if total_w else 100.0

        met = sum(1 for s in svc_states.values() if s.slo_met)
        compliance = 100.0 * met / len(svc_states) if svc_states else 100.0

        cost = sum(
            load[lid] * _MBPS_TO_GB_PER_MONTH * link.cost_per_gb
            for lid, link in self.links.items()
        )

        return NetworkSnapshot(
            tick=self.tick,
            label=label,
            services=svc_states,
            links=link_snaps,
            critical_availability_pct=crit_avail,
            weighted_availability_pct=weighted_avail,
            slo_compliance_pct=compliance,
            monthly_cost_ntd=cost,
            quota_hours_left=self.quota_hours_left(load),
        )

    def _evaluate_service(
        self, svc: Service, link_metrics: dict[str, tuple[float, float, float]]
    ) -> ServiceState:
        path = self.routing.paths.get(svc.id) or []
        admitted = self.routing.admitted.get(svc.id, 0.0)
        state = ServiceState(service_id=svc.id, path=path or None, admitted_mbps=admitted)

        links = self.path_links(path) if path else []
        if not path or not links or any(not l.is_usable for l in links):
            state.reachable = False
            state.violations = ["無可用路徑（鏈路中斷）"]
            return state

        state.reachable = True
        state.link_ids = [l.id for l in links]
        latency = 0.0
        survive = 1.0
        for link in links:
            l_ms, l_loss, _ = link_metrics[link.id]
            latency += l_ms
            survive *= 1.0 - l_loss / 100.0
        state.latency_ms = latency
        state.loss_pct = (1.0 - survive) * 100.0

        v: list[str] = []
        need = self.min_mbps(svc.id)
        if admitted < need:
            v.append(f"頻寬 {admitted:.0f} < 最低需求 {need:.0f} Mbps")
        if latency > svc.slo.max_latency_ms:
            v.append(f"延遲 {latency:.0f}ms > SLO {svc.slo.max_latency_ms:.0f}ms")
        if state.loss_pct > svc.slo.max_loss_pct:
            v.append(f"丟包 {state.loss_pct:.2f}% > SLO {svc.slo.max_loss_pct:.2f}%")
        state.violations = v
        state.slo_met = not v
        return state

    # ------------------------------------------------------------- 輔助

    def topology_dict(self) -> dict:
        return {
            "nodes": [
                {"id": n.id, "name": n.name, "kind": n.kind.value, "site": n.site}
                for n in self.nodes.values()
            ],
            "links": [
                {
                    "id": l.id, "src": l.src, "dst": l.dst, "kind": l.kind.value,
                    "capacity_mbps": l.capacity_mbps, "base_latency_ms": l.base_latency_ms,
                    "cost_per_gb": l.cost_per_gb, "state": l.state.value,
                }
                for l in self.links.values()
            ],
            "services": [
                {
                    "id": s.id, "name": s.name, "src": s.src, "dst": s.dst,
                    "priority": s.priority, "critical": s.is_critical,
                    "required_mbps": s.slo.required_bandwidth_mbps,
                    "max_latency_ms": s.slo.max_latency_ms,
                    "clinical_note": s.clinical_note,
                }
                for s in self.services.values()
            ],
        }
