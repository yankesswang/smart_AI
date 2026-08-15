"""舞台導演：把一次真實的 Agent 閉環，編排成研究文件 §5.1 的八段劇本。

這個模組不重新實作任何邏輯 —— 它跑的就是 :func:`factory_guardian.episode.run_episode`，
和 Benchmark、Dashboard、CLI ``demo`` 走的是同一條路。它多做的只有三件事：

1. **編排**：把 Orchestrator 逐階段吐出的事件，對進劇本的八個時間段，
   每段結束時輸出「這段要證明的事」＋「當下的真實數字」。
2. **確定性**：固定 seed、固定注入時點、腳本化核准。同一指令跑兩次，
   :func:`decision_fingerprint` 必須一模一樣。指紋刻意不含任何敘述文字與計時，
   因為那兩者本來就允許不同（LLM 敘述、機器速度），把它們算進去只會製造假失敗。
3. **韌性量測**：``offline=True`` 時整場跑在「無金鑰 ＋ 廠區對外鏈路中斷」的最壞情況下，
   並量出**離線備援時間**（斷網到閉環驗證完成的實際運算耗時），
   對應研究文件 §5.3 的「Demo 穩定」那一列。

Demo 現場的節奏由 ``speed`` 控制，而節奏只影響「印完一段之後停多久」，
不影響任何決策 —— 這是可以被測試證明的（``test_stage.py``）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..audit import AuditLog
from ..config import Settings, get_settings
from ..deployment.link import cloud_link, set_cloud_link
from ..domain import ApprovalDecision, RecoveryPlan
from ..episode import EpisodeResult, run_episode
from ..policy.engine import PolicyDecision
from ..twin.scenarios import SCENARIOS, get_scenario
from .script import SCRIPT, Act

#: 舞台固定 seed。決賽只有一次機會，種子不吃環境變數。
STAGE_SEED = 20260809

#: 決賽主場景。
DEFAULT_SCENARIO = "bearing-degradation"

#: ``--speed`` 的具名節奏。值是劇本時間的倍率：1.0 = 照 §5.1 走完整四分鐘。
SPEEDS: dict[str, float] = {
    "live": 1.0,        # 現場：四分鐘
    "rehearsal": 0.25,  # 彩排：一分鐘，節奏感還在
    "fast": 0.0,        # 驗證：不等待
}

#: 每一段允許的最長「等待」秒數上限，避免 ``--speed 999`` 讓現場卡死。
MAX_HOLD_SECONDS = 600.0


def resolve_speed(value: str | float) -> float:
    """把 ``--speed`` 解析成倍率。接受具名節奏或直接給倍率。"""
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    key = str(value).strip().lower()
    if key in SPEEDS:
        return SPEEDS[key]
    try:
        return max(0.0, float(key))
    except ValueError as exc:
        raise ValueError(
            f"未知節奏 {value!r}；可用：{', '.join(SPEEDS)}，或直接給倍率（例如 0.5）"
        ) from exc


def stage_settings(
    offline: bool = False,
    seed: int = STAGE_SEED,
    audit_dir: Any = None,
    base: Settings | None = None,
) -> Settings:
    """舞台用設定：固定 seed、要求人工核准；離線模式連金鑰都不給。

    ``offline=True`` 是**最壞情況**：沒有 ``OPENAI_API_KEY``，而且開機預設就是斷網。
    這正是文件 §5.1「Demo 可靠性原則」要求準備的本機離線模式。
    """
    base = base or get_settings()
    return Settings(
        openai_api_key=None if offline else base.openai_api_key,
        openai_model=base.openai_model,
        openai_base_url=base.openai_base_url,
        allow_offline_llm=base.allow_offline_llm,
        llm_timeout_s=base.llm_timeout_s,
        audit_dir=audit_dir if audit_dir is not None else base.audit_dir,
        # 舞台一定要走人工核准 —— A6 那一段的全部意義就在這裡。
        require_approval=True,
        cloud_link_up=not offline,
        seed=seed,
        tick_seconds=base.tick_seconds,
    )


def scripted_approval(plan: RecoveryPlan, decision: PolicyDecision) -> ApprovalDecision:
    """劇本化的主管核准：確定性，但**不是** auto —— 它會被算成一次人工介入。

    現場想真的按一下的話，CLI 有 ``--manual-approval``。
    """
    return ApprovalDecision(
        plan_id=plan.plan_id,
        approved=decision.allowed,
        approver="stage-supervisor",
        reason="現場主管核准（劇本 A6：核准與工單）" if decision.allowed else "動作被 Policy 禁止",
        auto=False,
    )


# --------------------------------------------------------------------------------------
# 決策指紋
# --------------------------------------------------------------------------------------
def decision_fingerprint(result: EpisodeResult, counterfactual: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一次舞台執行壓成「所有真正重要的決策」。

    比照 ``tests/test_deployment.py::_decision_fingerprint`` 的做法，並額外納入
    KPI 與工單 —— 舞台要保證的是**整場觀眾看到的東西**都一樣，不只是決策一樣。
    刻意排除：敘述文字、latency、run_id、時間戳 —— 那些本來就會變。
    """
    loop = result.loop
    if loop is None or loop.diagnosis is None:
        return {"triggered": False, "scenario": result.scenario_id}
    top = loop.diagnosis.top
    kpi = result.kpi
    return {
        # 台上會念出來的反事實數字也算「觀眾看到的東西」，所以一起進指紋。
        "counterfactual": counterfactual,
        "scenario": result.scenario_id,
        "mode": result.mode,
        "detection_tick": loop.detection_tick,
        "confirmation_ticks": loop.confirmation_ticks,
        "fault": top.fault_id if top else None,
        "confidence": round(top.confidence, 6) if top else None,
        "affected_orders": sorted(i.order_id for i in loop.impact.affected_orders) if loop.impact else [],
        "plans": [p.plan_id for p in loop.plans],
        "safety": {p.plan_id: (p.safety.verdict.value if p.safety else None) for p in loop.plans},
        "feasible": {p.plan_id: p.feasible for p in loop.plans},
        "ranking": [p.plan_id for p in loop.ranking.ranked] if loop.ranking else [],
        "scores": {p.plan_id: round(p.score, 6) for p in loop.plans},
        "work_order": (
            {
                "priority": loop.work_order.priority,
                "skill": loop.work_order.required_skill,
                "parts": list(loop.work_order.suggested_parts),
                "repair_min": round(loop.work_order.estimated_repair_min, 4),
            }
            if loop.work_order
            else None
        ),
        "approvals": [(a.plan.plan_id, a.approval.approver, a.approval.approved) for a in loop.attempts],
        "executed": loop.executed_plan_ids,
        "verified": loop.verified,
        "escalated": loop.escalated,
        "kpi": {
            "detection_latency_min": kpi.detection_latency_min,
            "time_to_diagnose_min": kpi.time_to_diagnose_min,
            "diagnosis_correct": kpi.diagnosis_correct,
            "production_attainment_pct": round(kpi.production_attainment_pct, 4),
            "max_order_delay_min": round(kpi.max_order_delay_min, 4),
            "hazard_exposure_min": round(kpi.hazard_exposure_min, 4),
            "machine_health_final": round(kpi.machine_health_final, 4),
            "secondary_damage": kpi.secondary_damage,
            "verification_passed": kpi.verification_passed,
        },
    }


def fingerprint_hash(fingerprint: dict[str, Any]) -> str:
    payload = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# 結果結構
# --------------------------------------------------------------------------------------
@dataclass
class ActReport:
    """一段劇本演完的結果。"""

    act: Act
    facts: dict[str, Any]
    missing: list[str]
    compute_s: float          # 這一段真正花掉的運算時間（不含節奏停頓）
    at_s: float               # 從開演算起，這一段是在第幾秒印出來的

    @property
    def ok(self) -> bool:
        return not self.missing

    def values(self) -> list[tuple[str, str]]:
        """這一段要念出來的數字（標籤、格式化後的值）。"""
        return [(m.label, m.render(self.facts.get(m.key))) for m in self.act.metrics]

    def detail_lines(self) -> list[tuple[str, list[str]]]:
        out: list[tuple[str, list[str]]] = []
        for key in self.act.details:
            value = self.facts.get(key)
            if isinstance(value, list) and value:
                out.append((key, [str(v) for v in value]))
            elif isinstance(value, str) and value:
                out.append((key, [value]))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "act_id": self.act.act_id,
            "window": self.act.window,
            "screen": self.act.screen,
            "doc_claim": self.act.doc_claim,
            "proves": self.act.proves,
            "ok": self.ok,
            "missing": self.missing,
            "compute_s": round(self.compute_s, 4),
            "at_s": round(self.at_s, 3),
            "facts": self.facts,
        }


@dataclass
class StageCheck:
    """一條「這場 Demo 有沒有成功」的判準。刻意全部是結構性的，不比對任何預期數值。"""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class StageRun:
    """一場舞台 Demo 的完整結果。"""

    scenario_id: str
    speed: float
    offline: bool
    acts: list[ActReport] = field(default_factory=list)
    checks: list[StageCheck] = field(default_factory=list)
    fingerprint: dict[str, Any] = field(default_factory=dict)
    fingerprint_hash: str = ""
    wall_s: float = 0.0            # 含節奏停頓的總牆鐘時間
    compute_s: float = 0.0         # 不含節奏停頓的運算時間
    offline_failover_s: float | None = None
    audit_path: str | None = None
    audit_records: int = 0
    llm_mode: str = ""
    link_mode: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(a.ok for a in self.acts) and all(c.passed for c in self.checks)

    @property
    def required_total(self) -> int:
        return sum(len(a.required) for a in SCRIPT)

    @property
    def missing_total(self) -> int:
        seen = {a.act.act_id: a.missing for a in self.acts}
        return sum(len(seen.get(a.act_id, list(a.required))) for a in SCRIPT)

    @property
    def data_loss_pct(self) -> float:
        """研究文件 §5.3 的「資料遺失率」：該有而沒拿到的欄位佔比。"""
        total = self.required_total
        return 100.0 * self.missing_total / total if total else 0.0

    def failures(self) -> list[str]:
        out = [f"{c.name}：{c.detail}" for c in self.checks if not c.passed]
        out += [f"{a.act.act_id} 缺資料：{'、'.join(a.missing)}" for a in self.acts if a.missing]
        if self.error:
            out.insert(0, self.error)
        missing_acts = [a.act_id for a in SCRIPT if a.act_id not in {r.act.act_id for r in self.acts}]
        if missing_acts:
            out.append(f"未演出的段落：{'、'.join(missing_acts)}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "speed": self.speed,
            "offline": self.offline,
            "ok": self.ok,
            "acts": [a.to_dict() for a in self.acts],
            "checks": [c.to_dict() for c in self.checks],
            "failures": self.failures(),
            "fingerprint": self.fingerprint,
            "fingerprint_hash": self.fingerprint_hash,
            "wall_s": round(self.wall_s, 3),
            "compute_s": round(self.compute_s, 4),
            "offline_failover_s": None if self.offline_failover_s is None else round(self.offline_failover_s, 4),
            "data_loss_pct": round(self.data_loss_pct, 2),
            "audit_path": self.audit_path,
            "audit_records": self.audit_records,
            "llm_mode": self.llm_mode,
            "link_mode": self.link_mode,
            "settings": self.settings,
        }


ActObserver = Callable[[ActReport], None]


# --------------------------------------------------------------------------------------
# 導演
# --------------------------------------------------------------------------------------
class StageDirector:
    """跑一次舞台 Demo。"""

    def __init__(
        self,
        scenario_id: str = DEFAULT_SCENARIO,
        speed: str | float = "live",
        offline: bool = False,
        approval: Callable[[RecoveryPlan, PolicyDecision], ApprovalDecision] | None = None,
        observer: ActObserver | None = None,
        persist_audit: bool = True,
        seed: int = STAGE_SEED,
        settings: Settings | None = None,
        counterfactual: bool = True,
    ) -> None:
        if scenario_id not in SCENARIOS:
            raise KeyError(f"未知情境 {scenario_id}；可用：{', '.join(SCENARIOS)}")
        self.scenario_id = scenario_id
        self.speed = resolve_speed(speed)
        self.offline = offline
        self.approval = approval or scripted_approval
        self.observer = observer
        self.persist_audit = persist_audit
        self.seed = seed
        self.counterfactual = counterfactual
        self.settings = settings or stage_settings(offline=offline, seed=seed)

        self._fingerprint: dict[str, Any] = {}
        self._facts: dict[str, Any] = {}
        self._raw: dict[str, Any] = {}
        self._done: set[str] = set()
        self._reports: list[ActReport] = []
        self._last_snapshot: dict[str, Any] | None = None
        self._t0 = 0.0
        self._paused_s = 0.0
        self._act_started_s = 0.0
        self._paused_before_act = 0.0
        self._verify_compute_s: float | None = None

    # ------------------------------------------------------------------ 時間
    def _now(self) -> float:
        return time.perf_counter() - self._t0

    def _compute_now(self) -> float:
        """扣掉節奏停頓之後的耗時 —— 所有對外回報的效能數字都用這個。"""
        return self._now() - self._paused_s

    def _hold(self, act: Act) -> None:
        """把這一段撐滿劇本上的長度。``speed=0`` 就完全不等。"""
        if self.speed <= 0:
            return
        target = min(act.duration_s * self.speed, MAX_HOLD_SECONDS)
        spent = self._now() - self._act_started_s
        remaining = target - spent
        if remaining > 0:
            time.sleep(remaining)
            self._paused_s += remaining

    # ------------------------------------------------------------------ 主流程
    def run(self) -> StageRun:
        scenario = get_scenario(self.scenario_id)
        audit = AuditLog(settings=self.settings, persist=self.persist_audit)
        run = StageRun(scenario_id=self.scenario_id, speed=self.speed, offline=self.offline)

        self._t0 = time.perf_counter()
        self._act_started_s = 0.0
        previous_link = cloud_link().up
        if self.offline:
            set_cloud_link(
                False,
                reason="舞台離線備援演練：模擬廠區對外鏈路中斷（研究文件 §5.1 Demo 可靠性原則）",
                actor="stage-director",
                audit=audit,
            )
        link_down_at = self._compute_now()

        result: EpisodeResult | None = None
        try:
            result = run_episode(
                scenario,
                mode="guardian",
                settings=self.settings,
                approval=self.approval,
                on_stage=self._on_stage,
                audit=audit,
                persist_audit=self.persist_audit,
                require_approval=True,
            )
            # A8 也要在斷網狀態下演完 —— 總結那一段的反事實對照組同樣不該偷用網路，
            # 否則「全程離線」這句話就有一個註腳。
            self._emit_summary(result, audit)
        except Exception as exc:  # pragma: no cover - 舞台上真的炸了才會走到這
            run.error = f"閉環執行中止：{type(exc).__name__}: {exc}"
        finally:
            if self.offline:
                set_cloud_link(
                    previous_link,
                    reason="舞台劇本結束，恢復鏈路狀態",
                    actor="stage-director",
                    audit=audit,
                )

        if result is not None and not run.error:
            run.fingerprint = self._fingerprint
            run.fingerprint_hash = fingerprint_hash(self._fingerprint)
            run.checks = self._build_checks(result, audit)

        run.acts = list(self._reports)
        run.wall_s = self._now()
        run.compute_s = self._compute_now()
        if self.offline and self._verify_compute_s is not None:
            run.offline_failover_s = self._verify_compute_s - link_down_at
        run.audit_path = str(audit.path) if audit.path else None
        run.audit_records = len(audit.records)
        # 敘述器模式取自執行**當下**的診斷結果，而不是事後回頭問設定 ——
        # 鏈路已經恢復了，事後問只會拿到恢復後的答案。
        run.llm_mode = str(self._facts.get("diagnose.llm_mode") or self.settings.describe()["llm_mode"])
        run.link_mode = "down" if self.offline else ("up" if cloud_link().up else "down")
        run.settings = dict(self.settings.describe())
        return run

    # ------------------------------------------------------------------ 事件對應
    #: Orchestrator 的 stage → 這個 stage 會讓哪幾段劇本可以演出（順序即演出順序）。
    _ACT_TRIGGERS: dict[str, tuple[str, ...]] = {
        "tick": ("A1",),
        "detect": ("A2",),
        "diagnose": ("A3", "A4"),
        "rank": ("A5",),
        "approve": ("A6",),
        "verify": ("A7",),
    }

    def _on_stage(self, stage: str, payload: dict[str, Any]) -> None:
        """Orchestrator 的每一個階段事件都會流過這裡。

        大部分 stage 只是把原始 payload 收起來（後面某一段會用到），
        真正「演出」的是 :data:`_ACT_TRIGGERS` 裡列到的那幾個。
        """
        if stage == "tick":
            self._last_snapshot = payload.get("snapshot")
        elif stage == "impact":
            self._raw["impact"] = payload["impact"]
        elif stage == "plan":
            self._raw["plans"] = payload["plans"]
        elif stage == "execute":
            self._raw["effects"] = payload.get("effects", [])
        elif stage == "confirm":
            # confirm 不是獨立的一段劇本，但 A4 要念「續觀察了幾分鐘」。
            self._facts["evidence.confirm_waited_min"] = float(payload.get("waited_ticks", 0)) * self._tick_minutes()
            self._facts["evidence.confidence_threshold"] = payload.get("min_confidence")
        elif stage == "work_order":
            self._facts.update(self._facts_work_order(payload))
        elif stage == "policy":
            decision = payload.get("decision", {})
            self._facts["approve.risk"] = decision.get("risk")
            self._facts["approve.policy_reasons"] = list(decision.get("reasons", []))
            self._facts["approve.requires_approval"] = decision.get("requires_approval")

        builders = {
            "A1": self._facts_normal,
            "A2": self._facts_detect,
            "A3": self._facts_diagnose,
            "A4": self._facts_evidence,
            "A5": self._facts_plan,
            "A6": self._facts_approve,
            "A7": self._facts_verify,
        }
        for act_id in self._ACT_TRIGGERS.get(stage, ()):
            if act_id in self._done:
                continue
            facts = builders[act_id](payload)
            if facts is None:
                continue
            self._facts.update(facts)
            self._emit(act_id)

    def _tick_minutes(self) -> float:
        return self.settings.tick_seconds / 60.0

    def _emit(self, act_id: str) -> None:
        act = next(a for a in SCRIPT if a.act_id == act_id)
        # 這一段只帶自己要念的欄位；劇本說要哪些，就給哪些（缺的留 None，由渲染顯示「—」）。
        keys = dict.fromkeys([m.key for m in act.metrics] + list(act.details) + list(act.required))
        started_compute_s = self._act_started_s - self._paused_before_act
        report = ActReport(
            act=act,
            facts={k: self._facts.get(k) for k in keys},
            missing=act.missing(self._facts),
            compute_s=max(0.0, self._compute_now() - started_compute_s),
            at_s=self._now(),
        )
        self._done.add(act_id)
        self._reports.append(report)
        if self.observer:
            self.observer(report)
        self._hold(act)
        self._act_started_s = self._now()
        self._paused_before_act = self._paused_s

    # ------------------------------------------------------------------ 各段事實
    def _facts_normal(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        snapshot = payload.get("snapshot")
        if not snapshot:
            return None
        machines = []
        for machine in snapshot["machines"].values():
            readings = "、".join(
                f"{name} {r['value']:.1f} {r['unit']}" for name, r in list(machine["readings"].items())[:3]
            )
            machines.append(
                f"{machine['machine_id']}｜{machine['name']}｜{machine['state']}"
                f"｜健康度 {machine['health']:.0f}｜{machine['production_rate_uph']:.0f} 件/hr｜{readings}"
            )
        cameras = snapshot.get("cameras") or []
        camera = cameras[0] if cameras else {}
        return {
            "normal.tick": snapshot.get("sim_minutes"),
            "normal.factory_health": snapshot.get("factory_health"),
            "normal.production_pct": snapshot.get("production_pct"),
            "normal.machines": machines,
            "normal.person_count": camera.get("person_count"),
            "normal.hazard_zone_clear": not camera.get("person_in_hazard_zone", False),
            "normal.camera": [
                f"{camera.get('camera_id', 'CAM')}｜{camera.get('caption', '')}"
                f"（信心 {camera.get('confidence', 0):.2f}）"
            ]
            if camera
            else [],
        }

    def _facts_detect(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = payload["event"]
        scenario = get_scenario(self.scenario_id)
        injected_tick = min((i.start_tick for i in scenario.injections), default=0)
        tick_min = self._tick_minutes()
        readings = [
            f"{name}：{r['value']:.2f} {r['unit']}（{r['band']}）" for name, r in event.get("readings", {}).items()
        ]
        cameras = (self._last_snapshot or {}).get("cameras") or []
        camera = cameras[0] if cameras else {}
        return {
            "detect.event_id": event.get("event_id"),
            "detect.machine_id": event.get("machine_id"),
            "detect.at_min": event.get("sim_minutes"),
            "detect.injected_at_min": injected_tick * tick_min,
            "detect.latency_min": (event.get("tick", 0) - injected_tick) * tick_min,
            "detect.severity": str(event.get("severity", "")).upper(),
            "detect.detector": event.get("detector"),
            "detect.health": event.get("health"),
            "detect.readings": readings,
            "detect.triggers": list(event.get("triggers", [])),
            "detect.camera": [f"{camera.get('camera_id', 'CAM')}｜{camera.get('caption', '')}"] if camera else [],
        }

    def _facts_diagnose(self, payload: dict[str, Any]) -> dict[str, Any]:
        diagnosis = payload["diagnosis"]
        candidates = diagnosis.get("candidates", [])
        top = candidates[0] if candidates else {}
        modalities = sorted({e["source"] for e in top.get("evidence", [])})
        weights = diagnosis.get("weights") or {}
        return {
            "diagnose.fault_id": top.get("fault_id"),
            "diagnose.label": top.get("label"),
            "diagnose.confidence": top.get("confidence"),
            "diagnose.candidate_count": len(candidates),
            "diagnose.modalities": [f"{m}（{_modality_label(m)}）" for m in modalities],
            "diagnose.modality_count": len(modalities),
            "diagnose.signal_strength": diagnosis.get("signal_strength"),
            "diagnose.llm_mode": diagnosis.get("llm_mode"),
            "diagnose.weights": [f"{k} × {v}" for k, v in weights.items()],
            "diagnose.narrative": (diagnosis.get("narrative") or "").strip(),
        }

    def _facts_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        diagnosis = payload["diagnosis"]
        candidates = diagnosis.get("candidates", [])
        top = candidates[0] if candidates else {}
        evidence = top.get("evidence", [])
        alternatives = [
            f"{c['label']}｜信心 {c['confidence']:.3f}"
            + (f"｜{'、'.join(f'{k} {v:.3f}' for k, v in c.get('scores', {}).items())}" if c.get("scores") else "")
            for c in candidates[1:]
        ]
        sop_refs = sorted({e["reference"] for e in evidence if e["source"] in ("manual", "sop", "rag:sop")})
        return {
            "evidence.count": len(evidence),
            "evidence.items": [
                f"[{e['source']}] {e['reference']}：{e['statement'][:110]}" for e in evidence[:6]
            ],
            "evidence.alternatives": alternatives,
            "evidence.sop_refs": sop_refs,
            "evidence.data_at_min": self._facts.get("detect.at_min"),
        }

    def _facts_plan(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        ranking = payload.get("ranking") or {}
        plans = self._raw.get("plans") or []
        impact = self._raw.get("impact") or {}
        if not plans:
            return None
        by_id = {p["plan_id"]: p for p in plans}
        recommended_id = ranking.get("recommended_plan_id")
        recommended = by_id.get(recommended_id, {})
        blocked = [p for p in plans if p.get("safety") and p["safety"]["blocked"]]
        orders = impact.get("affected_orders", [])
        return {
            "plan.count": len(plans),
            "plan.recommended": f"{recommended_id}｜{recommended.get('title', '')}" if recommended_id else None,
            "plan.score": recommended.get("score"),
            "plan.projected_production_pct": (recommended.get("projection") or {}).get("production_pct"),
            "plan.projected_delay_min": (recommended.get("projection") or {}).get("max_order_delay_min"),
            "plan.blocked_count": len(blocked),
            "plan.affected_order_count": len(orders),
            "plan.total_delay_min": impact.get("total_delay_min"),
            # 台上只念得完幾張單：有風險的排前面，其餘按延遲排序，最多四張。
            "plan.impact_orders": [
                f"{o['order_id']}｜{o['product_id']}｜剩 {o['remaining']:.0f} 件"
                f"｜交期 {o['due_in_min']:.0f} min｜預估延遲 {o['delay_min']:.0f} min"
                + ("（有風險）" if o["at_risk"] else "")
                for o in sorted(orders, key=lambda o: (not o["at_risk"], -o["delay_min"], o["due_in_min"]))[:4]
            ],
            "plan.blocked": [
                f"{p['plan_id']}｜{p['title']}｜BLOCK"
                + (
                    f"｜{p['safety']['findings'][0]['rule_id']} {p['safety']['findings'][0]['message'][:100]}"
                    if p["safety"]["findings"]
                    else ""
                )
                for p in blocked
            ],
            "plan.candidates": [
                f"#{p['rank']} {p['plan_id']}｜{p['title']}"
                f"｜產能 {p['projection']['production_pct']:.0f}%"
                f"｜延遲 {p['projection']['max_order_delay_min']:.0f} min"
                f"｜成本 {p['projection']['cost_ntd']:,.0f} NTD"
                f"｜{(p['safety'] or {}).get('verdict', '—')}"
                + (f"｜分數 {p['score']:.3f}" if p["feasible"] else "｜不可行")
                for p in sorted(plans, key=lambda x: x["rank"])
            ],
        }

    def _facts_work_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        work_order = payload["work_order"]
        return {
            "work_order.id": work_order.get("work_order_id"),
            "work_order.priority": work_order.get("priority"),
            "work_order.skill": work_order.get("required_skill"),
            "work_order.repair_min": work_order.get("estimated_repair_min"),
            "work_order.completeness_pct": work_order.get("completeness_pct"),
            "work_order.parts": list(work_order.get("suggested_parts", [])),
            "work_order.sop_refs": list(work_order.get("sop_refs", [])),
        }

    def _facts_approve(self, payload: dict[str, Any]) -> dict[str, Any]:
        approval = payload["approval"]
        return {
            "approve.plan_id": approval.get("plan_id"),
            "approve.approved": approval.get("approved"),
            "approve.approver": approval.get("approver"),
            "approve.reason": approval.get("reason"),
            "approve.auto": approval.get("auto"),
        }

    def _facts_verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        report = payload["report"]
        after = report.get("after", {})
        self._verify_compute_s = self._compute_now()
        return {
            "verify.plan_id": report.get("plan_id"),
            "verify.passed": report.get("passed"),
            "verify.check_count": len(report.get("checks", [])),
            "verify.health_after": after.get("factory_health"),
            "verify.production_pct_after": after.get("production_pct"),
            "verify.checks": [
                f"{'PASS' if c['passed'] else 'FAIL'}｜{c['name']}"
                f"｜預期 {c['expected']:.1f} {c['comparator']} 實測 {c['actual']:.1f}"
                for c in report.get("checks", [])
            ],
            "verify.effects": [
                f"{'✔' if e.get('ok') else '✘'} {e.get('message', '')}" for e in (self._raw.get("effects") or [])
            ],
            "verify.narrative": (report.get("narrative") or "").strip(),
        }

    # ------------------------------------------------------------------ 收尾
    def _counterfactual(self) -> dict[str, Any] | None:
        """「如果只是告警、什麼都不做，會怎樣」—— 真的把 Baseline A 跑一遍。

        避免停機損失（文件 §5.1 最後一段要求的數字）如果拿方案投影去減，
        算出來會是 0 甚至負的 —— 因為停機維修本來就比硬跑少做幾件。
        真正的價值在「不處置會一路壞下去」，那必須真的跑一次不處置才知道。
        Baseline A 是 Benchmark 已經在用的對照組，跑一次約 0.05 秒。
        """
        if not self.counterfactual:
            return None
        try:
            baseline = run_episode(
                get_scenario(self.scenario_id),
                mode="baseline-a",
                settings=self.settings,
                audit=AuditLog(settings=self.settings, persist=False),
                persist_audit=False,
                require_approval=False,
            )
        except Exception:  # pragma: no cover - 對照組失敗不該拖垮舞台
            return None
        return {
            "mode": "baseline-a",
            "production_attainment_pct": round(baseline.kpi.production_attainment_pct, 2),
            "production_loss_ntd": round(baseline.kpi.production_loss_ntd, 0),
            "machine_health_final": round(baseline.kpi.machine_health_final, 2),
            "max_order_delay_min": round(baseline.kpi.max_order_delay_min, 2),
            "secondary_damage": baseline.kpi.secondary_damage,
        }

    def _emit_summary(self, result: EpisodeResult, audit: AuditLog) -> None:
        kpi = result.kpi
        loop = result.loop
        stages: dict[str, int] = {}
        for record in audit.records:
            stages[record.stage] = stages.get(record.stage, 0) + 1
        counterfactual = self._counterfactual()
        avoided = None
        if counterfactual is not None:
            avoided = max(0.0, counterfactual["production_loss_ntd"] - kpi.production_loss_ntd)
        self._fingerprint = decision_fingerprint(result, counterfactual)
        self._facts.update(
            {
                "summary.counterfactual": (
                    [
                        f"對照組 Baseline A（固定門檻告警、不處置，同 seed 同視野）："
                        f"產能達成 {counterfactual['production_attainment_pct']:.1f}%"
                        f"｜設備最終健康度 {counterfactual['machine_health_final']:.1f}"
                        f"｜最大交期延遲 {counterfactual['max_order_delay_min']:.0f} min"
                        f"｜二次損壞 {'發生' if counterfactual['secondary_damage'] else '未發生'}",
                        f"本次閉環：產能達成 {kpi.production_attainment_pct:.1f}%"
                        f"｜設備最終健康度 {kpi.machine_health_final:.1f}"
                        f"｜最大交期延遲 {kpi.max_order_delay_min:.0f} min"
                        f"｜二次損壞 {'發生' if kpi.secondary_damage else '未發生'}",
                    ]
                    if counterfactual
                    else []
                ),
                "summary.detection_latency_min": kpi.detection_latency_min,
                "summary.time_to_diagnose_min": kpi.time_to_diagnose_min,
                "summary.diagnosis_correct": kpi.diagnosis_correct,
                "summary.production_attainment_pct": kpi.production_attainment_pct,
                "summary.max_order_delay_min": kpi.max_order_delay_min,
                "summary.machine_health_final": kpi.machine_health_final,
                "summary.secondary_damage": kpi.secondary_damage,
                "summary.verification_passed": kpi.verification_passed,
                "summary.human_interventions": kpi.human_interventions,
                "summary.avoided_loss_ntd": avoided,
                "summary.audit_records": len(audit.records),
                "summary.audit_path": result.audit_path,
                "summary.audit_stages": [f"{k} × {v}" for k, v in sorted(stages.items())],
                "summary.link_mode": "edge-autonomous（斷網）" if self.offline else "cloud-assisted",
                "summary.compute_seconds": self._compute_now(),
                "summary.fingerprint": fingerprint_hash(self._fingerprint),
            }
        )
        self._emit("A8")

    def _build_checks(self, result: EpisodeResult, audit: AuditLog) -> list[StageCheck]:
        """全部是結構性判準：不比對任何預期數值，只問「該發生的有沒有發生」。"""
        loop = result.loop
        checks: list[StageCheck] = []
        played = {r.act.act_id for r in self._reports}
        missing_acts = [a.act_id for a in SCRIPT if a.act_id not in played]
        checks.append(
            StageCheck("八段劇本全數演出", not missing_acts, f"缺 {'、'.join(missing_acts)}" if missing_acts else "A1–A8")
        )
        checks.append(StageCheck("閉環被觸發", bool(loop and loop.triggered), "" if loop else "沒有偵測到任何事件"))
        if loop is None:
            return checks

        truth = next((v for k, v in get_scenario(self.scenario_id).ground_truth.items() if k != "SAFETY"), None)
        if truth is not None:
            top = loop.diagnosis.top if loop.diagnosis else None
            ok = bool(top and top.fault_id == truth)
            checks.append(
                StageCheck(
                    "根因診斷等於 Ground Truth",
                    ok,
                    f"研判 {top.fault_id if top else '—'}／正解 {truth}",
                )
            )
        threshold = loop.confidence_threshold
        top = loop.diagnosis.top if loop.diagnosis else None
        checks.append(
            StageCheck(
                "診斷信心度達到動設備門檻",
                bool(top and top.confidence >= threshold),
                f"信心度 {top.confidence:.3f} ≥ 門檻 {threshold:.2f}" if top else "沒有候選根因",
            )
        )
        checks.append(
            StageCheck("Safety 對每個方案都做過裁決", all(p.safety is not None for p in loop.plans), f"{len(loop.plans)} 個方案")
        )
        human = [a for a in loop.attempts if not a.approval.auto]
        checks.append(
            StageCheck(
                "高風險動作經過人工核准",
                bool(human) and all(a.approval.approved for a in human),
                "、".join(f"{a.plan.plan_id}←{a.approval.approver}" for a in human) or "沒有任何非自動核准",
            )
        )
        # 工單完整度的標準要看事故種類。設備故障知道壞在哪，零件、技能、SOP 都填得出來，
        # 所以要求 80%；純工安事件沒有壞掉的零件可以填，拿同一把尺去量它是量錯東西。
        floor = 80.0 if truth is not None else 1.0
        completeness = loop.work_order.completeness() if loop.work_order else 0.0
        checks.append(
            StageCheck(
                f"工單完整度達標（≥{floor:.0f}%）",
                bool(loop.work_order) and completeness >= floor,
                f"{completeness:.0f}%" if loop.work_order else "沒有工單",
            )
        )
        checks.append(
            StageCheck("方案真的被執行", loop.executed_plan is not None, "、".join(loop.executed_plan_ids) or "未執行")
        )
        checks.append(StageCheck("執行後驗證通過", loop.verified, "" if loop.verified else "驗證未通過"))
        checks.append(StageCheck("未升級為人工處置", not loop.escalated, loop.escalation_reason))
        required_stages = {"episode_start", "detect", "diagnose", "plan", "safety", "rank",
                           "policy", "approval", "execute", "verify", "episode_end"}
        seen_stages = {r.stage for r in audit.records}
        gap = sorted(required_stages - seen_stages)
        checks.append(StageCheck("稽核軌跡涵蓋閉環每一站", not gap, f"缺 {'、'.join(gap)}" if gap else f"{len(audit.records)} 筆"))
        if self.offline:
            degrades = [r for r in audit.records if r.stage == "degrade"]
            checks.append(
                StageCheck(
                    "斷網降級有留下稽核紀錄",
                    bool(degrades),
                    f"{len(degrades)} 筆 degrade" if degrades else "降級沒有被記錄下來",
                )
            )
            calls = [r for r in audit.records if r.stage == "llm_call"]
            checks.append(
                StageCheck(
                    "敘述全部走邊緣確定性敘述器",
                    bool(calls) and all(c.detail.get("cloud_link") == "down" for c in calls),
                    f"{len(calls)} 次敘述",
                )
            )
        return checks


def _modality_label(source: str) -> str:
    labels = {
        "sensor": "感測器",
        "vision": "影像",
        "manual": "手冊",
        "sop": "SOP",
        "history": "歷史工單",
        "rule": "規則",
        "topology": "拓撲",
        "acoustic": "聲音",
        "audio": "聲音",
    }
    if source.startswith("rag:"):
        return "RAG 檢索：" + labels.get(source[4:], source[4:])
    return labels.get(source, source)


__all__ = [
    "DEFAULT_SCENARIO",
    "MAX_HOLD_SECONDS",
    "SPEEDS",
    "STAGE_SEED",
    "ActReport",
    "StageCheck",
    "StageDirector",
    "StageRun",
    "decision_fingerprint",
    "fingerprint_hash",
    "resolve_speed",
    "scripted_approval",
    "stage_settings",
]
