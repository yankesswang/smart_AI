"""Episode 執行與 KPI 量測（規格 §10）。

一個 Episode = 在一個情境上，用某一種模式跑完整段時間，然後量 KPI。

四種模式對應規格 §10.1 的對照組：

* ``baseline-a``：只有固定 Threshold 告警，不做跨資料診斷，也不採取任何行動。
* ``baseline-b``：偵測後直接停機，不做生產影響分析與替代排程。
* ``baseline-c``：**現行流程**。固定門檻告警 → 人工 90 分鐘判定根因（機台照跑、照劣化）
  → 停機維修 → 復機。不做轉單、不做任何跨資料分析。
* ``guardian``：跨 Machine / Production / Safety 的完整閉環。

為什麼要有 Baseline C：A 和 B 都是理想化的極端 —— A 是「告警完全沒被接住」，
B 是「告警完全被接住而且技師零等待」。真實工廠兩者都不是，
拿 41% 對 97% 當開場，第一個追問就會是「哪家工廠是這樣運作的？」。
Baseline C 把現行流程真正花時間的那一段放進模擬：**人工判定根因的 90 分鐘**
（來源見 docs/factory_guardian/business_case.md §3 假設參數表）。它才是該被比較的對象。

四種模式跑在**相同 seed、相同情境、相同總時長**的孿生體上，所以 KPI 可以直接比較。
Ground Truth 只在這裡用來評分，不會進入任何 Agent 的輸入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .agents.base import AgentContext
from .audit import AuditLog
from .config import Settings, get_settings
from .domain import Action, ActionKind, MachineState, Scenario, Severity
from .orchestrator import ApprovalCallback, LoopResult, Orchestrator, StageCallback, auto_approve
from .policy.engine import PolicyEngine
from .twin.engine import FactoryTwin
from .twin.faults import FAULTS
from .twin.scenarios import disturbances_of, reality_gap_of
from .twin.topology import UNIT_MARGIN_NTD

MODES = ("baseline-a", "baseline-b", "baseline-c", "guardian")

# 現行流程人工判定根因所需工時（分鐘）。
# 來源：docs/factory_guardian/business_case.md §3 假設參數表 —— 到場 15 ＋ 現場量測 30 ＋
# 查手冊與歷史工單 30 ＋ 與生產確認 15。ROI 模型和這裡引用的是同一個數字，
# 兩邊不能各講各的。
MANUAL_DIAGNOSIS_MIN = 90.0
# 工安事件解除後的復機準備時間（分鐘）：淨空現場、確認人員撤離、解除 LOTO。
HAZARD_CLEARANCE_MIN = 10.0
# 一個 Episode 內最多處置幾次。
#
# **目前是 1**：一個 Episode 只計分一次事故，四種模式因此比的是同一件事。
# 復發偵測本身已經實作好了（`MonitoringAgent.acknowledge` 重置告警狀態、
# `RESTART_GRACE_TICKS` 擋掉復機暫態），把它調成 2 就會開始處理「處置後復發」。
# 沒有直接調成 2 的理由很實際：復發會讓 Baseline B 在同一個 Episode 內停機兩次，
# 而「一次事故該被計分幾次」是另一個問題 —— 混在同一張表裡，
# 讀表的人會分不清楚產能差異是來自處置品質還是來自事故次數。
# 多事故計分留給下一版，連同它自己的表。
MAX_RESPONSES = 1
# 會改變設備狀態的動作。誤報情境下做了其中任何一個，就是一次誤動作。
EQUIPMENT_ACTION_KINDS = {"stop_machine", "start_maintenance", "derate_machine", "transfer_order"}
# 「現場看得到」的工安規則：人員闖入、煙霧、跌倒、護具不全。
# 這幾條不需要 90 分鐘判根因 —— 人就站在那裡，看得到就會按停止。
# SR-02 / SR-03（振動、溫度超過危險門檻）不在其中：那是感測器讀值，
# 現行流程對它的反應就是一般的門檻告警，不是工安應變。
PERSONNEL_SAFETY_RULES = ("SR-01", "SR-04", "SR-05", "SR-08")


@dataclass
class EpisodeKPI:
    """規格 §10 的五類 KPI，全部由實際執行量出來。"""

    # Diagnosis
    detected: bool = False
    detection_latency_min: float | None = None
    # diagnosis_correct 是**最終**診斷（重規劃後的那一個）；
    # first_diagnosis_correct 是第一次的答案。兩者的差就是重規劃救回來的部分。
    diagnosis_correct: bool | None = None
    first_diagnosis_correct: bool | None = None
    diagnosis_confidence: float | None = None
    false_positive_events: int = 0
    # 誤報率：每小時在**沒有故障的機台**上觸發幾次告警。
    # 用「每小時」而不是「百分比」：情境長度不同時，次數本身不可比。
    false_alarm_per_hour: float = 0.0
    # 誤報之後有沒有真的動到設備（停機／降速／維修／轉單）。
    # 這一格才是誤報的成本所在 —— 告警很便宜，誤動作很貴。
    false_positive_actions: int = 0
    # 處置完成之後又再次告警的次數（復發）。不完整的維修就是這樣被看見的。
    recurrences: int = 0
    # 重規劃（驗證失敗 → 重新診斷規劃）的次數，以及從失敗到通過花了多久。
    replans: int = 0
    replan_recovery_min: float | None = None
    # 閉環是否因為「無故障徵兆」或「訊號已停在新穩態」而主動放棄處置。
    abstained: bool = False

    # Maintenance
    time_to_diagnose_min: float | None = None
    work_order_completeness_pct: float | None = None

    # Production
    production_attainment_pct: float = 0.0
    production_loss_units: float = 0.0
    production_loss_ntd: float = 0.0
    max_order_delay_min: float = 0.0
    late_orders: int = 0
    recovery_min: float | None = None

    # 永續（能源／碳排）—— 全部由孿生體逐 tick 積分量出來，不是事後估算。
    energy_kwh: float = 0.0
    energy_waste_kwh: float = 0.0
    energy_waste_ntd: float = 0.0
    co2e_kg: float = 0.0
    co2e_waste_kg: float = 0.0
    energy_intensity_kwh_per_unit: float = 0.0

    # Safety
    unsafe_plans_generated: int = 0
    unsafe_plans_blocked: int = 0
    hazard_detected: bool = False
    safety_violations_executed: int = 0
    hazard_exposure_min: float = 0.0

    # Agent / 設備
    plans_considered: int = 0
    attempts: int = 0
    verification_passed: bool | None = None
    human_interventions: int = 0
    decision_latency_ms: float = 0.0
    tool_calls: int = 0
    tool_success_pct: float = 100.0
    machine_health_final: float = 100.0
    secondary_damage: bool = False

    @property
    def unsafe_block_rate_pct(self) -> float:
        if self.unsafe_plans_generated == 0:
            return 100.0
        return 100.0 * self.unsafe_plans_blocked / self.unsafe_plans_generated

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items()}
        data["unsafe_block_rate_pct"] = round(self.unsafe_block_rate_pct, 1)
        for key, value in list(data.items()):
            if isinstance(value, float):
                data[key] = round(value, 2)
        return data


@dataclass
class EpisodeResult:
    scenario_id: str
    mode: str
    horizon_ticks: int
    kpi: EpisodeKPI
    loop: LoopResult | None
    ground_truth: dict[str, str]
    final_kpi: dict[str, float]
    agent_metrics: list[dict[str, Any]] = field(default_factory=list)
    audit_path: str | None = None
    run_id: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    events_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "mode": self.mode,
            "horizon_ticks": self.horizon_ticks,
            "run_id": self.run_id,
            "ground_truth": self.ground_truth,
            "kpi": self.kpi.to_dict(),
            "final_kpi": {k: round(v, 2) for k, v in self.final_kpi.items()},
            "agent_metrics": self.agent_metrics,
            "audit_path": self.audit_path,
            "settings": self.settings,
            "events_seen": self.events_seen,
            "loop": self.loop.to_dict() if self.loop else None,
        }


def run_episode(
    scenario: Scenario,
    mode: str = "guardian",
    settings: Settings | None = None,
    approval: ApprovalCallback | None = None,
    on_stage: StageCallback | None = None,
    audit: AuditLog | None = None,
    persist_audit: bool = True,
    require_approval: bool | None = None,
) -> EpisodeResult:
    """跑完一個 Episode 並回傳 KPI。"""
    if mode not in MODES:
        raise ValueError(f"未知模式 {mode}；可用：{', '.join(MODES)}")

    settings = settings or get_settings()
    audit = audit or AuditLog(settings=settings, persist=persist_audit)
    needs_approval = settings.require_approval if require_approval is None else require_approval
    ctx = AgentContext(
        settings=settings,
        audit=audit,
        policy=PolicyEngine(require_approval=needs_approval),
    )

    twin = FactoryTwin(seed=settings.seed, tick_minutes=settings.tick_seconds / 60.0)
    twin.schedule(scenario.injections)
    # 干擾（無故障）與現實落差（徵兆偏離手冊／感測器故障）。
    # 兩者都只是孿生體的內部設定，不會產生任何給 Agent 的標籤 —— 見 twin/disturbances.py。
    disturbances = disturbances_of(scenario)
    for disturbance in disturbances:
        twin.add_disturbance(disturbance)
    gap = reality_gap_of(scenario)
    twin.apply_reality_gap(gap)
    injection_tick = min(
        (i.start_tick for i in scenario.injections),
        default=min((d.start_tick for d in disturbances), default=0),
    )

    audit.log(
        "episode_start",
        "orchestrator",
        scenario=scenario.scenario_id,
        mode=mode,
        horizon_ticks=scenario.horizon_ticks,
        settings=settings.describe(),
        disturbances=[d.to_dict() for d in disturbances],
        reality_gap=gap.to_dict() if gap else None,
        data_disclaimer="所有 Sensor / Orders / Manual / Maintenance History 皆為合成資料。",
    )

    # Baseline A 與 Baseline C 用的是同一個偵測器：固定門檻打在原始瞬時讀值上。
    # 兩者的差別不在「看到什麼」，而在「看到之後做什麼」。
    monitoring_mode = "threshold_only" if mode in ("baseline-a", "baseline-c") else "full"
    orch = Orchestrator(
        twin=twin,
        ctx=ctx,
        approval=approval or auto_approve,
        monitoring_mode=monitoring_mode,
        max_attempts=1 if mode in ("baseline-b", "baseline-c") else 2,
        # settle_ticks 不覆寫：預設值已與 ProductionAgent.PLAN_HORIZON_TICKS 對齊，
        # 預測與量測必須在同一個時間尺度上比較。
        on_stage=on_stage,
    )

    kpi = EpisodeKPI()
    # 一個 Episode 可能跑不只一次閉環（處置 → 復發 → 再處置）。
    # KPI 一律以**第一次**閉環為主體（那才是這次事故的處置決策），
    # 重規劃次數等累加型指標則跨閉環相加。
    loops: list[LoopResult] = []
    loop: LoopResult | None = None
    faulty_machines = {m for m in scenario.ground_truth if m != "SAFETY"}

    # --- Detect → Respond（可重複）------------------------------------------------
    # 為什麼要迴圈：處置完成不代表事情結束。一次不完整的維修（工單工時對這一台不夠）
    # 會讓故障在幾十分鐘後復發 —— 真實的值班台會再響一次，Benchmark 也應該讓它再響一次。
    # 只跑一次偵測，等於幫每個模式把「復發」這件事藏起來，而復發正是現況最貴的部分。
    # 四種模式吃的是同一個迴圈，所以這不是給誰的優待。
    responses = 0
    first_event = None
    safety_false_alarms: set[str] = set()
    while responses < MAX_RESPONSES and twin.tick < scenario.horizon_ticks:
        # 誰有「一直盯著危險區的眼睛」是對照組定義的一部分：
        # Baseline A 與 Baseline C 都沒有影像分析。現行流程的工安控制是**程序性**的
        # （SOP、圍籬、教育訓練、定期巡檢），不是偵測性的 —— 把 AI 攝影機送給對照組，
        # 等於假設現況已經有了我們要新增的那個能力，這個 Benchmark 就不誠實了。
        event = orch.run_until_event(
            scenario.horizon_ticks - twin.tick,
            min_severity=Severity.WARNING,
            watch_safety=mode not in ("baseline-a", "baseline-c"),
        )
        if event is None:
            break
        responses += 1
        if first_event is None:
            first_event = event
            kpi.detected = True
            kpi.detection_latency_min = (event.tick - injection_tick) * twin.tick_minutes
        else:
            kpi.recurrences += 1
            audit.log("recurrence", "orchestrator", mode=mode, machine_id=event.machine_id,
                      tick=event.tick, severity=event.severity.value,
                      note="同一台機台在處置之後再次觸發告警：前一次處置沒有把問題解決。")
        if (
            event.kind == "safety"
            and event.machine_id not in faulty_machines
            and "SAFETY" not in scenario.ground_truth
        ):
            # 工安偵測器觸發的那一次也要算 —— 只數 Monitoring Agent 的事件，
            # 會讓它憑空消失。這裡記下機台，最後和感測器告警一起去重。
            safety_false_alarms.add(event.machine_id)

        if mode == "baseline-a":
            # Baseline A：只告警，什麼都不做。
            twin.apply(Action(ActionKind.RAISE_ALERT, event.machine_id, {"level": event.severity.value},
                              "Baseline A：固定門檻告警"))
            audit.log("baseline_action", "baseline-a", machine_id=event.machine_id,
                      note="只發出告警，不做診斷、不做影響分析、不採取任何設備行動。")
            break   # 什麼都不做的模式沒有「再處置一次」這回事
        if mode == "baseline-b":
            # Baseline B：偵測後直接停機，不做影響分析與替代排程。
            for action in (
                Action(ActionKind.STOP_MACHINE, event.machine_id, {}, "Baseline B：偵測即停機"),
                Action(ActionKind.START_MAINTENANCE, event.machine_id, {}, "Baseline B：直接進場維修"),
            ):
                effect = twin.apply(action)
                audit.log("baseline_action", "baseline-b", action=action.describe(), **effect.to_dict())
            kpi.human_interventions += 1
        elif mode == "baseline-c":
            _run_baseline_c(kpi, twin, audit, event, scenario)
        else:
            loops.append(orch.handle_event(event))
        # 處置告一段落 → 把該機台的告警狀態重置（acknowledge）。
        # 沒有這一步，同一台機器在同一個嚴重度上永遠不會再告警一次，復發就看不見了。
        orch.monitoring.acknowledge(event.machine_id)

    # 誤報 = 在「沒有故障的機台」上發生的告警**事故**，一台機台算一次。
    #
    # 為什麼不逐條數告警訊息：Guardian 有 WARNING 與 CRITICAL 兩級，
    # 同一段干擾會先報 WARNING 再升級成 CRITICAL；Baseline A 只有一級，永遠只報一次。
    # 逐條數等於因為「分級比較細」而罰它，那量到的是告警設計，不是誤報。
    # 值班台真正在意的問題是「有幾台健康的機器把人叫過來」。
    kpi.false_positive_events = len(
        {e.machine_id for e in _all_events(orch) if e.machine_id not in faulty_machines}
        | safety_false_alarms
    )

    # --- 跑完剩下的時間，讓三種模式的總時長一致 ----------------------------------------
    # 三組必須跑滿同樣的 tick 數，KPI 才可比。閉環若吃掉超過 horizon 的時間，
    # 代表情境視野設得太短，會明確記錄下來而不是默默讓比較失真。
    remaining = scenario.horizon_ticks - twin.tick
    if remaining > 0:
        twin.run(remaining)
    elif remaining < 0:
        audit.log(
            "horizon_overrun", "orchestrator", mode=mode, scenario=scenario.scenario_id,
            horizon_ticks=scenario.horizon_ticks, actual_ticks=twin.tick,
            note="閉環耗時超過情境視野，跨模式 KPI 比較將不對等，請加大 horizon_ticks。",
        )

    if loops:
        loop = loops[0]
        _fill_guardian_kpi(kpi, loops, scenario, twin)
    _fill_common_kpi(kpi, twin, scenario, orch)
    _fill_false_positive_kpi(kpi, twin, scenario)
    final_kpi = twin.kpi()
    debug = twin.debug_state()
    kpi.secondary_damage = any(
        e.get("type") == "secondary_damage" for e in twin.event_log
    )
    kpi.hazard_exposure_min = twin.hazard_exposure_min
    kpi.machine_health_final = min(
        (m["health"] for mid, m in debug["machines"].items() if mid in faulty_machines),
        default=100.0,
    )

    audit.log(
        "episode_end",
        "orchestrator",
        scenario=scenario.scenario_id,
        mode=mode,
        kpi=kpi.to_dict(),
        final_kpi={k: round(v, 2) for k, v in final_kpi.items()},
        ground_truth=scenario.ground_truth,
        note="Ground Truth 僅用於評分，未曾進入任何 Agent 的輸入。",
    )

    return EpisodeResult(
        scenario_id=scenario.scenario_id,
        mode=mode,
        horizon_ticks=scenario.horizon_ticks,
        kpi=kpi,
        loop=loop,
        ground_truth=dict(scenario.ground_truth),
        final_kpi=final_kpi,
        agent_metrics=orch.agent_metrics(),
        audit_path=str(audit.path) if audit.path else None,
        run_id=audit.run_id,
        settings=settings.describe(),
        events_seen=len(_all_events(orch)),
    )


# --------------------------------------------------------------------------------------
# KPI 組裝
# --------------------------------------------------------------------------------------
def _all_events(orch: Orchestrator) -> list:
    """Monitoring Agent 至今觸發過的所有事件（含被忽略的低嚴重度事件）。"""
    return orch.monitoring.events


def _run_baseline_c(kpi: EpisodeKPI, twin: FactoryTwin, audit: AuditLog, event, scenario: Scenario) -> None:
    """Baseline C：現行流程。

    告警 → **人工判定根因 90 分鐘** → 停機維修（依實際故障的標準工時）→ 復機。

    三個刻意的建模決定，每一個都寫下理由：

    1. **90 分鐘期間機台繼續跑、繼續劣化。**
       90 分鐘量的是「判定根因」，不是「反應時間」；現行流程在判定結論出來以前，
       不會停一台還在出貨的機台。這也是為什麼二次損壞在現況下是真的會發生的事。
       這是現況的**悲觀端**；樂觀端是 Baseline B（技師零等待、立刻停機）。
       ROI 模型引用的是兩者的機率混合（docs/factory_guardian/business_case.md §2.1），不是單獨任何一個。

    2. **維修工時取自實際故障的 `repair_min`。**
       人到現場把機器拆開來看，最後總會找到真正壞的是什麼 —— 所以 Baseline C
       的診斷永遠是對的、工時永遠是準的。這個假設對現況**有利**，
       等於讓對照組佔便宜，比較結果因此是保守的。
       （這不是「Agent 偷看答案」：Baseline C 不是 Agent，它是評分用的流程模型。）

    3. **不轉單。** 現行流程沒有跨機台的產能與交期投影，訂單就留在原機台等復機。
    """
    machine_id = event.machine_id
    twin.apply(Action(ActionKind.RAISE_ALERT, machine_id, {"level": event.severity.value},
                      "Baseline C：固定門檻告警，通知現場"))

    personnel_hazard = event.kind == "safety" and any(
        t.split(":")[-1] in PERSONNEL_SAFETY_RULES for t in event.triggers
    )
    if personnel_hazard:
        # 工安事件不需要 90 分鐘判根因 —— 人就站在那裡，看得到。
        # 停機淨空現場，確認人員撤離後復機。
        twin.apply(Action(ActionKind.STOP_MACHINE, machine_id, {}, "Baseline C：現場人員按下停止"))
        audit.log("baseline_action", "baseline-c", machine_id=machine_id,
                  note="工安事件：立即停機淨空現場（假設零反應時間，對現況最有利）。")
        twin.run(min(int(HAZARD_CLEARANCE_MIN / twin.tick_minutes),
                     max(0, scenario.horizon_ticks - twin.tick)))
        _resume_machine(twin, machine_id, audit, "現場淨空、LOTO 解除後復機")
        kpi.human_interventions = 1
        kpi.time_to_diagnose_min = HAZARD_CLEARANCE_MIN
        return

    # --- 人工判定根因：最多 90 分鐘，機台照跑、照劣化 ---------------------------------
    # 判定時間不能超出情境視野：四種模式必須跑一樣長，KPI 才可比。
    # 視野內判定沒跑完，那本身就是結果 —— 不要靠多跑幾分鐘把它補完。
    wait_ticks = min(
        int(MANUAL_DIAGNOSIS_MIN / twin.tick_minutes),
        max(0, scenario.horizon_ticks - twin.tick),
    )
    audit.log("baseline_action", "baseline-c", machine_id=machine_id,
              manual_diagnosis_min=MANUAL_DIAGNOSIS_MIN,
              note="技師到場、現場量測、查手冊與歷史工單、與生產確認；期間機台維持運轉並持續劣化。")
    elapsed = 0
    for _ in range(wait_ticks):
        twin.step()
        elapsed += 1
        # 二次損壞（軸承咬死、主軸損傷）不需要判定 —— 機台自己停了，聲音整條線都聽得到。
        # 判定流程到此結束，直接進入維修。少了這一條，Baseline C 會在一台已經報廢的機器旁邊
        # 繼續「查手冊」，那不是現況，那是稻草人。
        if twin.runtime[machine_id].secondary_damage:
            audit.log("baseline_action", "baseline-c", machine_id=machine_id,
                      elapsed_min=elapsed * twin.tick_minutes,
                      note="判定期間發生二次損壞：故障自己揭曉了，判定提前結束。")
            break
    kpi.time_to_diagnose_min = elapsed * twin.tick_minutes
    kpi.human_interventions = 1

    # --- 判定完成：依實際故障停機維修 -------------------------------------------------
    fault_id = twin.runtime[machine_id].fault
    repair_min = FAULTS[fault_id].repair_min if fault_id else twin.topo.machines[machine_id].repair_min
    if fault_id is None:
        # 誤報：技師花了 90 分鐘，什麼也沒找到。機台不停，成本是那 1.5 個工時。
        audit.log("baseline_action", "baseline-c", machine_id=machine_id, verdict="no_fault_found",
                  note="人工判定結果為誤報；不停機，成本為 1.5 個技師工時。")
        return
    for action in (
        Action(ActionKind.STOP_MACHINE, machine_id, {}, "Baseline C：判定完成後停機"),
        Action(ActionKind.START_MAINTENANCE, machine_id, {"duration_min": repair_min},
               "Baseline C：依判定結果進行維修"),
    ):
        effect = twin.apply(action)
        audit.log("baseline_action", "baseline-c", action=action.describe(), **effect.to_dict())
    # 維修完成後機台會自己回到 IDLE，再由派工恢復生產（見 twin.engine._dispatch_orders）。


def _resume_machine(twin: FactoryTwin, machine_id: str, audit: AuditLog, reason: str) -> None:
    """把一台被停下來的機器交還給生產。

    孿生體沒有「復機」這個 ActionKind —— Agent 的處置一律以維修結束後自動回到 IDLE 收尾。
    Baseline C 的工安流程沒有維修這一段，所以這裡直接把狀態改回 IDLE，
    再由 `_dispatch_orders` 決定它接哪張訂單。這是**評分用的流程模型**，不是 Agent 動作，
    所以不經過 Policy 與 Safety 閘門；如果它是 Agent 做的，那兩道閘門一個都不能少。
    """
    rt = twin.runtime.get(machine_id)
    if rt is None or rt.state is not MachineState.STOPPED:
        return
    rt.state = MachineState.IDLE
    audit.log("baseline_action", "baseline-c", machine_id=machine_id, note=reason)


def _fill_false_positive_kpi(kpi: EpisodeKPI, twin: FactoryTwin, scenario: Scenario) -> None:
    """誤報 KPI。

    只有在**沒有任何故障**的情境（`ground_truth` 為空）下，「動設備」才必然是誤動作 ——
    工安情境沒有設備故障，但停機正是那裡的正解，不能算誤動作。
    """
    horizon_hours = scenario.horizon_ticks * twin.tick_minutes / 60.0
    kpi.false_alarm_per_hour = kpi.false_positive_events / horizon_hours if horizon_hours > 0 else 0.0
    if scenario.ground_truth:
        return
    kpi.false_positive_actions = sum(
        1 for e in twin.event_log
        if e.get("type") == "action" and e.get("ok") and e.get("kind") in EQUIPMENT_ACTION_KINDS
    )


def _fill_guardian_kpi(
    kpi: EpisodeKPI, loops: list[LoopResult], scenario: Scenario, twin: FactoryTwin
) -> None:
    """從閉環結果填 KPI。

    第一次閉環決定「這次事故被怎麼處置」，所以診斷、方案、驗證都取它；
    重規劃次數與人工介入是累加型的，跨閉環相加。
    """
    loop = loops[0]
    truth = next((v for k, v in scenario.ground_truth.items() if k != "SAFETY"), None)
    kpi.abstained = loop.abstained
    kpi.replans = sum(x.replans for x in loops)
    recoveries = [x.recovery_after_replan_min for x in loops if x.recovery_after_replan_min is not None]
    kpi.replan_recovery_min = min(recoveries) * twin.tick_minutes if recoveries else None
    # 初次（偵測當下的第一次比對）與最終（確認、必要時重規劃之後）分開記。
    # 指紋知識和手冊來自同一份 deltas，所以在「徵兆照手冊走」的情境上兩者一定相同；
    # 一旦現場物理偏離手冊，差別就會出現 —— 那個差距就是觀察窗與重規劃買到的東西。
    final = loop.final_diagnosis
    first = loop.first_diagnosis
    if first and first.top and truth is not None:
        kpi.first_diagnosis_correct = first.top.fault_id == truth
    if loop.diagnosis and loop.diagnosis.top:
        kpi.diagnosis_confidence = loop.diagnosis.top.confidence
        if truth is not None and final is not None and final.top is not None:
            kpi.diagnosis_correct = final.top.fault_id == truth
        # Mean Time To Diagnose = 從故障注入到「診斷可據以行動」為止，
        # 含為了累積證據而刻意等待的時間。
        kpi.time_to_diagnose_min = (
            (kpi.detection_latency_min or 0.0) + loop.confirmation_ticks * twin.tick_minutes
        )
    if loop.work_order:
        kpi.work_order_completeness_pct = loop.work_order.completeness()

    kpi.plans_considered = len(loop.plans)
    kpi.attempts = sum(len(x.attempts) for x in loops)
    kpi.human_interventions = sum(
        sum(1 for a in x.attempts if not a.approval.auto) + (1 if x.escalated else 0)
        for x in loops
    )

    blocked = [p for p in loop.plans if p.safety and p.safety.blocked]
    kpi.unsafe_plans_generated = len(blocked)
    kpi.unsafe_plans_blocked = sum(1 for p in blocked if not p.feasible)
    kpi.hazard_detected = any(h.get("rule_id") in ("SR-01", "SR-04", "SR-05", "SR-08") for h in loop.standing_hazards)
    executed = loop.executed_plan
    kpi.safety_violations_executed = 1 if (executed and executed.safety and executed.safety.blocked) else 0

    last = loop.attempts[-1] if loop.attempts else None
    if last and last.verification:
        kpi.verification_passed = last.verification.passed
    if executed:
        kpi.recovery_min = executed.projection.recovery_min


def _fill_common_kpi(kpi: EpisodeKPI, twin: FactoryTwin, scenario: Scenario, orch: Orchestrator) -> None:
    horizon_min = scenario.horizon_ticks * twin.tick_minutes
    nominal_units = twin.nominal_output_uph * horizon_min / 60.0
    produced = twin.completed_units_total
    kpi.production_attainment_pct = 100.0 * produced / nominal_units if nominal_units > 0 else 0.0
    kpi.production_loss_units = max(0.0, nominal_units - produced)
    kpi.production_loss_ntd = kpi.production_loss_units * UNIT_MARGIN_NTD

    # 永續：孿生體在整段 episode 中逐 tick 累積的能源帳，這裡只是讀出來。
    energy = twin.energy_kpi()
    kpi.energy_kwh = energy["energy_kwh"]
    kpi.energy_waste_kwh = energy["energy_waste_kwh"]
    kpi.energy_waste_ntd = energy["energy_waste_ntd"]
    kpi.co2e_kg = energy["co2e_kg"]
    kpi.co2e_waste_kg = energy["co2e_waste_kg"]
    kpi.energy_intensity_kwh_per_unit = energy["energy_intensity_kwh_per_unit"]

    delays = []
    for order in twin.orders.values():
        if order.done:
            continue
        finish = twin.estimate_finish_min(order.order_id)
        delays.append(max(0.0, finish - order.due_in_min))
    kpi.max_order_delay_min = max(delays, default=0.0)
    kpi.late_orders = sum(1 for d in delays if d > 0)

    metrics = orch.agent_metrics()
    kpi.tool_calls = sum(m["tool_calls"] for m in metrics)
    ok_weighted = sum(m["tool_calls"] * m["tool_success_pct"] for m in metrics)
    kpi.tool_success_pct = ok_weighted / kpi.tool_calls if kpi.tool_calls else 100.0
    total_decisions = sum(m["decisions"] for m in metrics)
    kpi.decision_latency_ms = (
        sum(m["decisions"] * m["avg_decision_latency_ms"] for m in metrics) / total_decisions
        if total_decisions
        else 0.0
    )


__all__ = ["EpisodeKPI", "EpisodeResult", "run_episode", "MODES", "MANUAL_DIAGNOSIS_MIN"]
