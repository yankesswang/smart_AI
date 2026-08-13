"""Production Impact & Planning Agent（規格 §4.3）。

職責：
* 用 Knowledge Graph（Machine → Line → Product → Order）找出受影響的訂單。
* 估算 Downtime、Order Delay、Production Loss 與替代機台容量。
* 產生 2–4 個候選方案，每個方案的數字都由 Simulator **乾跑**算出來，不是 LLM 猜的。

關鍵設計：乾跑跑在 ``fork_as_belief()`` 建出來的「信念模型」上，
也就是 Diagnosis Agent 認為的故障，而不是 Simulator 的真實故障。
診斷錯了投影就會錯，然後由 Verification Agent 在真實孿生體上抓出來 —— 這才是完整閉環。
"""

from __future__ import annotations

from ..domain import (
    Action,
    ActionKind,
    Diagnosis,
    FactorySnapshot,
    ImpactAssessment,
    MachineState,
    OrderImpact,
    PlanProjection,
    RecoveryPlan,
)
from ..llm import SYSTEM_PROMPT
from ..twin.faults import FAULTS
from ..twin.topology import UNIT_MARGIN_NTD
from .base import Agent

# 乾跑的規劃視野（分鐘 = tick）。
# 這個值必須和 Verification Agent 的觀察窗（settle_ticks）一致：
# 拿「45 分鐘後的預測」去比「20 分鐘後的量測」，任何方案都會看起來預測失準。
PLAN_HORIZON_TICKS = 30
# 二次損壞的期望成本會乘上這個殘餘風險
RESIDUAL_RISK_HEALTH_FLOOR = 60.0


class ProductionAgent(Agent):
    name = "production-agent"
    role = "影響分析與生產調度"

    # ------------------------------------------------------------------ 影響分析
    def assess_impact(self, machine_id: str, snapshot: FactorySnapshot, twin) -> ImpactAssessment:
        topo = twin.topo
        with self.timed():
            with self.tool("kg.orders_depending_on", f"machine={machine_id}"):
                affected_orders = topo.orders_depending_on(machine_id, snapshot.orders.values())
            with self.tool("kg.downstream", f"machine={machine_id}"):
                downstream = topo.downstream(machine_id)

            machine = topo.machines[machine_id]
            impacts: list[OrderImpact] = []
            total_delay = 0.0
            for order in affected_orders:
                if order.done:
                    continue
                # 基準：機台健康時的額定速率
                rt_load = twin.runtime[machine_id].load_pct or 1.0
                healthy_rate = machine.rated_rate_uph * rt_load
                baseline_finish = 60.0 * order.remaining / healthy_rate if healthy_rate > 0 else float("inf")
                with self.tool("twin.estimate_finish", f"order={order.order_id}"):
                    projected = twin.estimate_finish_min(order.order_id)
                delay = max(0.0, projected - order.due_in_min)
                total_delay += delay
                impacts.append(
                    OrderImpact(
                        order_id=order.order_id,
                        product_id=order.product_id,
                        remaining=order.remaining,
                        baseline_finish_min=baseline_finish,
                        projected_finish_min=projected,
                        due_in_min=order.due_in_min,
                        delay_min=delay,
                        at_risk=delay > 0,
                    )
                )
            impacts.sort(key=lambda i: (-i.delay_min, i.order_id))

            nominal = twin.nominal_output_uph
            current = twin.throughput_ema_uph
            loss_rate = max(0.0, nominal - current)
            loss_units = loss_rate * (PLAN_HORIZON_TICKS * twin.tick_minutes) / 60.0
            loss_pct = 100.0 * loss_rate / nominal if nominal > 0 else 0.0

        assessment = ImpactAssessment(
            machine_id=machine_id,
            downstream_machines=downstream,
            affected_orders=impacts,
            production_loss_units=loss_units,
            production_loss_pct=loss_pct,
            total_delay_min=total_delay,
        )
        assessment.summary_points = self._impact_points(assessment, snapshot)
        assessment.narrative = self._narrate_impact(assessment, snapshot)
        self.log(
            "impact",
            machine_id=machine_id,
            downstream=downstream,
            affected_orders=[i.to_dict() for i in impacts],
            production_loss_units=round(loss_units, 1),
            production_loss_pct=round(loss_pct, 1),
            total_delay_min=round(total_delay, 1),
        )
        return assessment

    # ------------------------------------------------------------------ 方案產生
    def build_plans(
        self,
        machine_id: str,
        snapshot: FactorySnapshot,
        twin,
        diagnosis: Diagnosis | None,
        horizon_ticks: int = PLAN_HORIZON_TICKS,
    ) -> list[RecoveryPlan]:
        topo = twin.topo
        machine = topo.machines[machine_id]
        believed_fault = diagnosis.top.fault_id if diagnosis and diagnosis.top else None
        if believed_fault not in FAULTS:
            # 診斷認為沒有設備故障（例如工安事件觸發的閉環）：規劃模型就不要放故障進去。
            believed_fault = None
        observed_health = snapshot.machines[machine_id].health
        model = FAULTS.get(believed_fault) if believed_fault else None
        repair_min = model.repair_min if model else machine.repair_min

        # 找出這台機台目前的訂單與可承接的替代機台
        current_order = next(
            (o for o in snapshot.orders.values() if o.assigned_machine == machine_id and not o.done), None
        )
        alternates = [
            alt for alt in topo.alternates(machine_id)
            if current_order is not None and topo.can_produce(alt, current_order.product_id)
            and snapshot.machines[alt].state not in (MachineState.STOPPED, MachineState.MAINTENANCE)
        ]

        # 方案要配合機台目前的狀態。對一台已經在維修的機器提出「降速運轉」是沒有意義的動作，
        # 而重試時的重新規劃正好會遇到這種狀態，所以這裡必須擋掉。
        state = snapshot.machines[machine_id].state
        is_running = state in (MachineState.RUNNING, MachineState.DERATED)
        is_offline = state in (MachineState.STOPPED, MachineState.MAINTENANCE)
        # 沒有設備故障（例如工安事件觸發）就不該排維修，只需停機淨空現場。
        needs_repair = model is not None

        def repair_actions() -> list[Action]:
            if not needs_repair:
                return [Action(ActionKind.RAISE_ALERT, machine_id, {"level": "critical"},
                               "停機後需現場確認安全狀況，無設備故障徵兆故不排維修。")]
            return [
                Action(ActionKind.START_MAINTENANCE, machine_id, {"duration_min": repair_min}, "依 SOP 進行維修。"),
                Action(ActionKind.CREATE_WORK_ORDER, machine_id, {}, "建立維修工單。"),
            ]

        specs: list[tuple[str, str, str, list[Action]]] = []

        if is_running:
            specs.append((
                "PLAN-A", "維持全速運轉",
                "不採取任何設備措施，維持現有產出，僅發出告警。",
                [Action(ActionKind.RAISE_ALERT, machine_id, {"level": "warning"},
                        "維持產量優先，僅告警不介入。")],
            ))
            if state is not MachineState.DERATED:
                specs.append((
                    "PLAN-B", "降速運轉延後維修",
                    f"將 {machine_id} 切換為降速模式以減緩劣化，維持部分產出，維修延後至換班。",
                    [Action(ActionKind.RAISE_ALERT, machine_id, {"level": "warning"}, "降速以減緩劣化。"),
                     Action(ActionKind.DERATE_MACHINE, machine_id, {"factor": 0.6}, "降低轉速以降低振動與熱負荷。")],
                ))

        if not is_offline:
            specs.append((
                "PLAN-C", "立即停機維修" if needs_repair else "立即停機並淨空危險區",
                f"立即停止 {machine_id}"
                + ("並進場維修，訂單留在原機台等待復機。" if needs_repair else "並淨空現場，待安全確認後復機。"),
                [Action(ActionKind.STOP_MACHINE, machine_id, {}, "停止有風險的機台。")] + repair_actions(),
            ))

            if current_order is not None and alternates:
                alt = alternates[0]
                specs.append((
                    "PLAN-D", f"轉單至 {alt} 並處置 {machine_id}",
                    f"將訂單 {current_order.order_id} 轉移至替代機台 {alt}（換線 "
                    f"{topo.machines[alt].changeover_min:g} 分鐘），同時停機處置 {machine_id}。",
                    [Action(ActionKind.TRANSFER_ORDER, current_order.order_id,
                            {"order_id": current_order.order_id, "to_machine": alt, "load_pct": 1.0},
                            f"{alt} 有備援產能可承接。"),
                     Action(ActionKind.STOP_MACHINE, machine_id, {}, "訂單轉出後即可安全停機。")] + repair_actions(),
                ))

        if not specs:
            # 機台已經在處置中，唯一合理的方案就是維持現行處置並持續監控。
            specs.append((
                "PLAN-HOLD", "維持現行處置並持續監控",
                f"{machine_id} 目前為 {state.value}，處置已在進行中，維持現況並持續監控。",
                [Action(ActionKind.RAISE_ALERT, machine_id, {"level": "info"}, "維持現行處置。")],
            ))

        plans: list[RecoveryPlan] = []
        with self.timed():
            for plan_id, title, summary, actions in specs:
                with self.tool("twin.project", f"plan={plan_id}"):
                    projection = self._project(
                        twin, machine_id, believed_fault, observed_health, actions, horizon_ticks, model
                    )
                plans.append(
                    RecoveryPlan(plan_id=plan_id, title=title, summary=summary, actions=actions, projection=projection)
                )

        self.log(
            "plan",
            machine_id=machine_id,
            believed_fault=believed_fault,
            horizon_ticks=horizon_ticks,
            plans=[{"plan_id": p.plan_id, "title": p.title, **p.projection.to_dict()} for p in plans],
            note="投影跑在『診斷信念模型』上，不使用 Simulator ground truth。",
        )
        return plans

    def _project(
        self,
        twin,
        machine_id: str,
        believed_fault: str | None,
        observed_health: float,
        actions: list[Action],
        horizon_ticks: int,
        model,
    ) -> PlanProjection:
        belief = twin.fork_as_belief(machine_id, believed_fault, observed_health, label=f"plan:{machine_id}")
        metrics = belief.project(actions, horizon_ticks, target_machine=machine_id)

        # 殘餘風險：乾跑結束時設備仍有多少劣化沒有被處理（只用快照看得到的健康度）。
        health_after = metrics["machine_health_after"]
        residual = max(0.0, (RESIDUAL_RISK_HEALTH_FLOOR - health_after) / RESIDUAL_RISK_HEALTH_FLOOR)
        residual = max(0.0, min(1.0, residual))
        if model is not None:
            residual = min(1.0, residual * model.full_speed_risk_gain)

        # 成本 = 產能損失 × 邊際貢獻 + 維修成本 + 期望二次損壞成本
        machine = twin.topo.machines[machine_id]
        repair_cost = 0.0
        for action in actions:
            if action.kind is ActionKind.START_MAINTENANCE:
                duration = float(action.params.get("duration_min", machine.repair_min))
                repair_cost += machine.hourly_cost_ntd * duration / 60.0 + 8_000.0  # 零件與人力
            if action.kind is ActionKind.TRANSFER_ORDER:
                target = action.params.get("to_machine")
                if target in twin.topo.machines:
                    repair_cost += twin.topo.machines[target].hourly_cost_ntd * twin.topo.machines[target].changeover_min / 60.0
        secondary_cost = (model.secondary_damage_cost_ntd if model else 150_000.0) * residual
        cost = metrics["production_loss_units"] * UNIT_MARGIN_NTD + repair_cost + secondary_cost

        return PlanProjection(
            production_pct=metrics["production_pct"],
            production_pct_final=metrics["production_pct_final"],
            production_loss_units=metrics["production_loss_units"],
            max_order_delay_min=metrics["final_order_delay_min"],
            recovery_min=metrics["recovery_min"],
            cost_ntd=cost,
            residual_risk=residual,
            machine_health_after=health_after,
            peak_vibration=metrics["peak_vibration"],
            peak_temperature=metrics["peak_temperature"],
            min_health=metrics["min_health"],
            recovered=bool(metrics["recovered"]),
            hazard_while_running=bool(metrics["hazard_while_running"]),
        )

    # ------------------------------------------------------------------ 敘述
    @staticmethod
    def _impact_points(assessment: ImpactAssessment, snapshot: FactorySnapshot) -> list[dict[str, object]]:
        """影響敘述的結構化版本，欄位定義同 ``Diagnosis.summary_points``。

        受影響訂單本身已經有表格可看，這裡只放「主管要先看到的四件事」：
        傳播範圍、訂單風險、最嚴重的那一張、以及維持現況的代價。
        """
        at_risk = [i for i in assessment.affected_orders if i.at_risk]
        points: list[dict[str, object]] = [
            {
                "label": "傳播範圍",
                "value": "、".join(assessment.downstream_machines) or "無下游",
                "detail": f"目前產線達成率 {snapshot.production_pct:.0f}%。",
            },
            {
                "label": "訂單風險",
                "value": f"{len(at_risk)} / {len(assessment.affected_orders)} 張",
                "detail": "依 Knowledge Graph 追蹤依賴此機台的訂單，其中有交期風險者。",
                "tone": "alarm" if at_risk else "",
            },
        ]

        if at_risk:
            worst = at_risk[0]
            points.append(
                {
                    "label": "最嚴重訂單",
                    "value": f"{worst.order_id} 延遲 {worst.delay_min:.0f} 分",
                    "detail": (
                        f"{worst.product_id}，剩餘 {worst.remaining:.0f} 件；"
                        f"預估完成需 {worst.projected_finish_min:.0f} 分鐘，交期剩 {worst.due_in_min:.0f} 分鐘。"
                    ),
                    "tone": "key",
                }
            )

        points.append(
            {
                "label": f"維持現況 {PLAN_HORIZON_TICKS} 分鐘",
                "value": f"損失 {assessment.production_loss_units:.0f} 件",
                "detail": f"達成率缺口 {assessment.production_loss_pct:.0f}%。",
                "tone": "alarm" if assessment.production_loss_units > 40 else "",
            }
        )
        return points

    def _narrate_impact(self, assessment: ImpactAssessment, snapshot: FactorySnapshot) -> str:
        def fallback() -> str:
            at_risk = [i for i in assessment.affected_orders if i.at_risk]
            lines = [
                f"{assessment.machine_id} 異常會沿產線影響下游 {'、'.join(assessment.downstream_machines) or '（無）'}；"
                f"目前產線達成率 {snapshot.production_pct:.0f}%。",
                f"依 Knowledge Graph 追蹤，共 {len(assessment.affected_orders)} 張訂單依賴此機台，"
                f"其中 {len(at_risk)} 張有交期風險。",
            ]
            for impact in at_risk[:3]:
                lines.append(
                    f"　• {impact.order_id}（{impact.product_id}，剩餘 {impact.remaining:.0f} 件）："
                    f"預估完成需 {impact.projected_finish_min:.0f} 分鐘，交期剩 {impact.due_in_min:.0f} 分鐘，"
                    f"延遲 {impact.delay_min:.0f} 分鐘。"
                )
            lines.append(
                f"若維持現況 {PLAN_HORIZON_TICKS} 分鐘，預估產能損失 {assessment.production_loss_units:.0f} 件"
                f"（達成率缺口 {assessment.production_loss_pct:.0f}%）。"
            )
            return "\n".join(lines)

        if self.ctx.llm is None:
            return fallback()
        payload = {
            "machine": assessment.machine_id,
            "downstream": assessment.downstream_machines,
            "production_pct_now": round(snapshot.production_pct, 1),
            "orders": [i.to_dict() for i in assessment.affected_orders[:4]],
            "production_loss_units": round(assessment.production_loss_units, 1),
        }
        user = (
            "以下是排程引擎算好的生產影響數字，請用 3~5 行繁體中文寫成給生產主管看的影響說明，"
            "重點放在哪張訂單會延遲多久、以及產能缺口多大。不要杜撰數字。\n\n"
            f"{payload}"
        )
        return self.ctx.llm.narrate(SYSTEM_PROMPT, user, fallback, actor=self.name).text


__all__ = ["ProductionAgent", "PLAN_HORIZON_TICKS"]
