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

from dataclasses import dataclass, field

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
    SafetyFinding,
    SafetyReview,
    SafetyVerdictKind,
)
from ..llm import SYSTEM_PROMPT
from ..optimizer import score_plan
from ..policy.engine import SafetyContext
from ..twin.engine import DERATE_LOAD_FACTOR
from ..twin.faults import FAULTS
from ..twin.topology import UNIT_MARGIN_NTD
from .base import Agent

# 乾跑的規劃視野（分鐘 = tick）。
# 這個值必須和 Verification Agent 的觀察窗（settle_ticks）一致：
# 拿「45 分鐘後的預測」去比「20 分鐘後的量測」，任何方案都會看起來預測失準。
PLAN_HORIZON_TICKS = 30
# 二次損壞的期望成本會乘上這個殘餘風險
RESIDUAL_RISK_HEALTH_FLOOR = 60.0

# --------------------------------------------------------------------------------------
# 方案參數搜尋（見 build_plans）
#
# 為什麼要有這一段：四個方案原本是固定模板，PLAN-B 的降速比例寫死 0.6、
# PLAN-C/D 一律「立刻停機」。那些數字沒有經過任何比較 —— 評審問「為什麼是 0.6 不是 0.4」
# 的時候，唯一誠實的答案是「因為當初就這樣寫」。
#
# 所以每個模板家族改成掃描一小組參數，每個變體都在**信念模型**上乾跑、
# 都送進 Safety 預測規則篩一次，只有分數最高且沒有被 BLOCK 的變體代表這個家族進排名；
# 掃描過的變體與分數全部留在 RecoveryPlan.variants 與稽核軌跡裡。
# --------------------------------------------------------------------------------------
# PLAN-B：降速深度。
DERATE_GRID: tuple[float, ...] = (0.4, 0.5, 0.6, 0.7, 0.8)
# 平手時要選哪一個：孿生體的降速模型是**單一固定工作點**（twin.engine.DERATE_LOAD_FACTOR），
# 所以方案上寫的 factor 必須和模擬器真的會跑的負載點一致，否則畫面上的數字是假的。
DERATE_DEFAULT = DERATE_LOAD_FACTOR
# PLAN-C / PLAN-D：維修啟動延後幾分鐘（延後 = 先把手上這批做完再停機）。
MAINTENANCE_DELAY_GRID_MIN: tuple[float, ...] = (0.0, 10.0, 20.0)
# 延後停機目前**不可執行**：Orchestrator 是把 plan.actions 一次下到設備，
# 動作字彙裡沒有「N 分鐘後再停機」。讓一個執行層做不到的變體去代表家族，
# 等於讓方案的預測值和實際執行永遠對不起來 —— 那是 Verification Agent 一定會抓到的謊。
# 所以延後變體照跑照記分（它是有價值的對照），但不得被選為家族代表。
# 要放行它，執行層必須先支援延後動作，屆時把這個旗標打開即可。
ALLOW_DEFERRED_EXECUTION = False


@dataclass(frozen=True)
class PlanVariant:
    """一個模板家族裡的參數變體。"""

    variant_id: str
    label: str
    params: dict[str, float]
    title: str
    summary: str
    actions: tuple[Action, ...]
    # 延後幾分鐘才把動作下下去（先讓機台把手上這批做完）。
    delay_min: float = 0.0
    # 執行層現在做不做得到（見 ALLOW_DEFERRED_EXECUTION）。
    executable: bool = True
    default: bool = False


@dataclass
class PlanFamily:
    plan_id: str
    variants: list[PlanVariant] = field(default_factory=list)


# 會讓目標機台停止產出的動作（和 SafetyAgent 用同一份定義）。
_STOPPERS = {ActionKind.STOP_MACHINE, ActionKind.START_MAINTENANCE}


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

        def delay_note(delay_min: float) -> str:
            if delay_min <= 0:
                return ""
            return f"（先讓機台把手上這批做完約 {delay_min:g} 分鐘再停機）"

        families: list[PlanFamily] = []

        if is_running:
            families.append(PlanFamily("PLAN-A", [PlanVariant(
                variant_id="PLAN-A",
                label="維持全速",
                params={},
                title="維持全速運轉",
                summary="不採取任何設備措施，維持現有產出，僅發出告警。",
                actions=(Action(ActionKind.RAISE_ALERT, machine_id, {"level": "warning"},
                                "維持產量優先，僅告警不介入。"),),
                default=True,
            )]))
            if state is not MachineState.DERATED:
                # PLAN-B 家族：降速深度。
                families.append(PlanFamily("PLAN-B", [
                    PlanVariant(
                        variant_id=f"PLAN-B@derate={factor:g}",
                        label=f"降速至額定 {factor * 100:.0f}%",
                        params={"derate_factor": factor},
                        title="降速運轉延後維修",
                        summary=f"將 {machine_id} 切換為降速模式（額定負載 {factor * 100:.0f}%）以減緩劣化，"
                                "維持部分產出，維修延後至換班。",
                        actions=(
                            Action(ActionKind.RAISE_ALERT, machine_id, {"level": "warning"}, "降速以減緩劣化。"),
                            Action(ActionKind.DERATE_MACHINE, machine_id, {"factor": factor},
                                   "降低轉速以降低振動與熱負荷。"),
                        ),
                        default=abs(factor - DERATE_DEFAULT) < 1e-9,
                    )
                    for factor in DERATE_GRID
                ]))

        if not is_offline:
            # PLAN-C 家族：停機時點（立即 / 先做完手上這批）。
            families.append(PlanFamily("PLAN-C", [
                PlanVariant(
                    variant_id=f"PLAN-C@delay={delay:g}",
                    label="立即停機" if delay <= 0 else f"延後 {delay:g} 分鐘停機",
                    params={"maintenance_delay_min": delay},
                    title=("立即停機維修" if needs_repair else "立即停機並淨空危險區") if delay <= 0
                          else (f"延後 {delay:g} 分鐘停機維修" if needs_repair
                                else f"延後 {delay:g} 分鐘停機並淨空危險區"),
                    summary=f"停止 {machine_id}{delay_note(delay)}"
                            + ("並進場維修，訂單留在原機台等待復機。" if needs_repair
                               else "並淨空現場，待安全確認後復機。"),
                    actions=tuple(
                        [Action(ActionKind.STOP_MACHINE, machine_id, {}, "停止有風險的機台。")] + repair_actions()
                    ),
                    delay_min=delay,
                    executable=delay <= 0 or ALLOW_DEFERRED_EXECUTION,
                    default=delay <= 0,
                )
                for delay in MAINTENANCE_DELAY_GRID_MIN
            ]))

            if current_order is not None and alternates:
                alt = alternates[0]
                families.append(PlanFamily("PLAN-D", [
                    PlanVariant(
                        variant_id=f"PLAN-D@delay={delay:g}",
                        label="轉單後立即停機" if delay <= 0 else f"轉單後延後 {delay:g} 分鐘停機",
                        params={"maintenance_delay_min": delay},
                        title=f"轉單至 {alt} 並處置 {machine_id}",
                        summary=f"將訂單 {current_order.order_id} 轉移至替代機台 {alt}（換線 "
                                f"{topo.machines[alt].changeover_min:g} 分鐘），"
                                f"同時停機處置 {machine_id}{delay_note(delay)}。",
                        actions=tuple(
                            [Action(ActionKind.TRANSFER_ORDER, current_order.order_id,
                                    {"order_id": current_order.order_id, "to_machine": alt, "load_pct": 1.0},
                                    f"{alt} 有備援產能可承接。"),
                             Action(ActionKind.STOP_MACHINE, machine_id, {}, "訂單轉出後即可安全停機。")]
                            + repair_actions()
                        ),
                        delay_min=delay,
                        executable=delay <= 0 or ALLOW_DEFERRED_EXECUTION,
                        default=delay <= 0,
                    )
                    for delay in MAINTENANCE_DELAY_GRID_MIN
                ]))

        if not families:
            # 機台已經在處置中，唯一合理的方案就是維持現行處置並持續監控。
            families.append(PlanFamily("PLAN-HOLD", [PlanVariant(
                variant_id="PLAN-HOLD",
                label="維持現行處置",
                params={},
                title="維持現行處置並持續監控",
                summary=f"{machine_id} 目前為 {state.value}，處置已在進行中，維持現況並持續監控。",
                actions=(Action(ActionKind.RAISE_ALERT, machine_id, {"level": "info"}, "維持現行處置。"),),
                default=True,
            )]))

        plans: list[RecoveryPlan] = []
        search_log: list[dict] = []
        with self.timed():
            for family in families:
                plan, records = self._search_family(
                    family, twin, machine_id, snapshot, believed_fault, observed_health, horizon_ticks, model
                )
                plans.append(plan)
                if len(family.variants) > 1:
                    plan.variants = records
                    search_log.append({"plan_id": family.plan_id, "variants": records})

        self.log(
            "plan",
            machine_id=machine_id,
            believed_fault=believed_fault,
            horizon_ticks=horizon_ticks,
            plans=[{"plan_id": p.plan_id, "title": p.title, **p.projection.to_dict()} for p in plans],
            note="投影跑在『診斷信念模型』上，不使用 Simulator ground truth。",
        )
        if search_log:
            self.log(
                "plan_search",
                machine_id=machine_id,
                dry_runs=sum(len(f.variants) for f in families),
                families=search_log,
                note="每個模板家族掃描一小組參數，全部在信念模型上乾跑並過 Safety 預測規則；"
                     "只有分數最高且未被 BLOCK 的可執行變體代表家族進排名。純計算，LLM 未參與。",
            )
        return plans

    # ------------------------------------------------------------------ 參數搜尋
    def _search_family(
        self,
        family: PlanFamily,
        twin,
        machine_id: str,
        snapshot: FactorySnapshot,
        believed_fault: str | None,
        observed_health: float,
        horizon_ticks: int,
        model,
    ) -> tuple[RecoveryPlan, list[dict]]:
        """把一個家族的所有變體乾跑一遍，回傳代表方案與掃描紀錄。

        代表的挑選規則（由嚴到寬，全部是規則，沒有 LLM）：

        1. 被 Safety 預測規則 BLOCK 的變體出局 —— 硬限制先於分數。
        2. 執行層做不到的變體（目前是「延後停機」）出局，理由見 ALLOW_DEFERRED_EXECUTION。
        3. 剩下的取加權分數最高者；分數相同（差距 < 1e-9）時取模板預設值 ——
           孿生體對某些參數是不敏感的（例如降速只有一個固定工作點），
           這時候硬要挑一個「贏家」等於無中生有製造鑑別力，不如誠實地回到預設。
        4. 全部出局時仍然回傳預設變體，讓它以 BLOCK 的身分留在方案矩陣上被看見。
        """
        candidates: list[tuple[PlanVariant, RecoveryPlan, float]] = []
        for variant in family.variants:
            with self.tool("twin.project", f"variant={variant.variant_id}"):
                projection = self._project(
                    twin, machine_id, believed_fault, observed_health,
                    list(variant.actions), horizon_ticks, model, delay_min=variant.delay_min,
                )
            plan = RecoveryPlan(
                plan_id=family.plan_id, title=variant.title, summary=variant.summary,
                actions=list(variant.actions), projection=projection,
            )
            plan.safety = self._prescreen_safety(plan, snapshot, machine_id, variant)
            score, _ = score_plan(plan)
            candidates.append((variant, plan, score))

        eligible = [
            item for item in candidates
            if item[1].safety is not None
            and item[1].safety.verdict is not SafetyVerdictKind.BLOCK
            and item[0].executable
        ]
        default_index = next((i for i, item in enumerate(candidates) if item[0].default), 0)
        if eligible:
            best_score = max(score for _, _, score in eligible)
            winners = [item for item in eligible if best_score - item[2] < 1e-9]
            chosen = next((item for item in winners if item[0].default), winners[0])
        else:
            chosen = candidates[default_index]

        records = [
            {
                "variant_id": variant.variant_id,
                "label": variant.label,
                "params": {k: round(v, 4) for k, v in variant.params.items()},
                "score": round(score, 4),
                "safety_verdict": plan.safety.verdict.value if plan.safety else "PASS",
                "blocked": bool(plan.safety and plan.safety.verdict is SafetyVerdictKind.BLOCK),
                "executable": variant.executable,
                "selected": variant is chosen[0],
                "production_pct": round(plan.projection.production_pct, 1),
                "max_order_delay_min": round(plan.projection.max_order_delay_min, 1),
                "recovery_min": round(plan.projection.recovery_min, 1),
                "cost_ntd": round(plan.projection.cost_ntd, 0),
                "peak_vibration": round(plan.projection.peak_vibration, 2),
                "min_health": round(plan.projection.min_health, 1),
                "residual_risk": round(plan.projection.residual_risk, 3),
            }
            for variant, plan, score in candidates
        ]
        scores = [score for _, _, score in candidates]
        if len(candidates) > 1 and max(scores) - min(scores) < 1e-9:
            for record in records:
                record["indistinguishable"] = True

        winner = chosen[1]
        # 掃描時掛上去的是**預篩**裁決，不是正式審查。正式的 Safety 審查由 Safety Agent 做
        # （它還會檢查接手訂單的機台），所以這裡把預篩結果拿掉，避免被誤當成最終裁決。
        winner.safety = None
        return winner, records

    def _prescreen_safety(
        self, plan: RecoveryPlan, snapshot: FactorySnapshot, machine_id: str, variant: PlanVariant
    ) -> SafetyReview:
        """用 Policy Engine 對變體做 Safety 預篩（含 SR-02P / SR-03P / SR-13 預測型規則）。

        兩段評估，因為一個「延後停機」的變體在延後窗內是真的還在跑：

        * 動作語意：這個方案最終讓機台停下來還是繼續跑（LOTO、Safety Override 這類規則看這個）。
        * 延後窗語意：延後的那幾分鐘機台仍在產出，所以要拿方案乾跑的峰值再過一次預測型規則 ——
          否則「先做完這批」就會變成一個可以繞過安全預測的漏洞。

        這裡只做**篩選**，不改任何裁決邏輯；正式裁決仍由 Safety Agent 執行。
        """
        policy = self.ctx.policy
        kinds = {action.kind for action in plan.actions}
        keeps_running = not (kinds & _STOPPERS)
        findings: list[SafetyFinding] = []
        if policy is not None:
            findings.extend(policy.evaluate_safety(SafetyContext(
                snapshot=snapshot,
                machine_id=machine_id,
                action_kinds=kinds,
                keeps_machine_running=keeps_running,
                keeps_full_speed=keeps_running and ActionKind.DERATE_MACHINE not in kinds,
                plan_id=variant.variant_id,
                projection=plan.projection,
            )))
            if variant.delay_min > 0:
                seen = {finding.rule_id for finding in findings}
                hold = policy.evaluate_safety(SafetyContext(
                    snapshot=snapshot,
                    machine_id=machine_id,
                    action_kinds=set(),          # 延後窗內還沒有任何動作被執行
                    keeps_machine_running=True,
                    keeps_full_speed=ActionKind.DERATE_MACHINE not in kinds,
                    plan_id=f"{variant.variant_id}:hold",
                    projection=plan.projection,
                ))
                findings.extend(f for f in hold if f.rule_id not in seen)
        verdict = policy.combine(findings) if policy is not None else SafetyVerdictKind.PASS
        return SafetyReview(plan_id=variant.variant_id, verdict=verdict, findings=findings)

    def _project(
        self,
        twin,
        machine_id: str,
        believed_fault: str | None,
        observed_health: float,
        actions: list[Action],
        horizon_ticks: int,
        model,
        delay_min: float = 0.0,
    ) -> PlanProjection:
        belief = twin.fork_as_belief(machine_id, believed_fault, observed_health, label=f"plan:{machine_id}")
        delay_ticks = int(round(delay_min / belief.tick_minutes)) if delay_min > 0 else 0
        delay_ticks = max(0, min(delay_ticks, horizon_ticks - 1))
        if delay_ticks:
            # 「先把手上這批做完再停機」＝ 前 delay_ticks 分鐘什麼都不做，之後才把動作下下去。
            # 孿生體的 project() 一次跑完一段，所以這裡分兩段跑再合併 —— 合併規則見 _merge_metrics。
            hold = belief.project([], delay_ticks, target_machine=machine_id)
            rest = belief.project(actions, horizon_ticks - delay_ticks, target_machine=machine_id)
            metrics = _merge_metrics(hold, rest, delay_ticks, horizon_ticks - delay_ticks, belief.tick_minutes)
        else:
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


def _merge_metrics(
    hold: dict[str, float], rest: dict[str, float], hold_ticks: int, rest_ticks: int, tick_minutes: float
) -> dict[str, float]:
    """把「延後窗」與「動作執行後」兩段乾跑合併成單一段的指標。

    合併規則對應每個指標的物理意義，不是隨手取平均：

    * 達成率是**時間平均** → 依兩段長度加權；
    * 損失件數是累積量 → 相加；
    * 峰值 / 最低值 / 危險曝露是整段的極值 → 取極值；
    * 復原時間定義成「達成率最後一次低於門檻的時點」→ 第二段若曾低於門檻，
      要把延後窗的長度加回去；第二段整段都在門檻之上時，才輪到延後窗的紀錄。
    """
    total = max(1, hold_ticks + rest_ticks)
    rest_recovery = rest["recovery_min"]
    if rest_recovery > 0:
        recovery_min = hold_ticks * tick_minutes + rest_recovery
    else:
        recovery_min = hold["recovery_min"]
    return {
        "production_pct": (hold["production_pct"] * hold_ticks + rest["production_pct"] * rest_ticks) / total,
        "production_pct_final": rest["production_pct_final"],
        "production_loss_units": hold["production_loss_units"] + rest["production_loss_units"],
        "max_order_delay_min": max(hold["max_order_delay_min"], rest["max_order_delay_min"]),
        "final_order_delay_min": rest["final_order_delay_min"],
        "recovery_min": recovery_min,
        "recovered": rest["recovered"],
        "machine_health_after": rest["machine_health_after"],
        "min_health": min(hold["min_health"], rest["min_health"]),
        "peak_vibration": max(hold["peak_vibration"], rest["peak_vibration"]),
        "peak_temperature": max(hold["peak_temperature"], rest["peak_temperature"]),
        "hazard_while_running": max(hold["hazard_while_running"], rest["hazard_while_running"]),
        "factory_health_after": rest["factory_health_after"],
        "health_before": hold["health_before"],
    }


__all__ = [
    "ProductionAgent",
    "PLAN_HORIZON_TICKS",
    "DERATE_GRID",
    "DERATE_DEFAULT",
    "MAINTENANCE_DELAY_GRID_MIN",
    "ALLOW_DEFERRED_EXECUTION",
    "PlanVariant",
    "PlanFamily",
]
