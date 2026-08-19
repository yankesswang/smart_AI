"""AgentGate 治理管線:G0 → G1 → G2 → G3 → G4 → G5 的編排。

每一道關卡都可以否決,且否決本身也是稽核事件——被擋下的動作和被執行的動作
留下同等完整的紀錄(規格 §3.1)。

關卡歸因:G0-R1(權限升級阻斷)優先於 G2 規則。同一個動作可能同時違反
G0 與 G2(例如上傳文件夾帶的批次匯出),但根因是「指令從不可信通道進來」,
歸因給 G0 才能讓稽核看見攻擊的入口。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .gates.g0_provenance import evaluate_trust, trust_evidence
from .gates.g1_resolution import validate_request
from .gates.g2_policy import PolicyEngine
from .gates.g3_projection import project_consequences, projection_evidence
from .gates.g4_approval import ApprovalQueue, PendingApproval
from .gates.g5_audit import AuditChain
from .metrics import LiveMetrics
from .ontology import (
    ActionRequest,
    Evidence,
    Finding,
    GateVerdict,
    Severity,
    risk_exceeds,
)

# 核准政策:回傳 True=核准 / False=駁回 / None=留在佇列等真人
ApprovalPolicy = Callable[[PendingApproval], "bool | None"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class GateConfig:
    enable_g0: bool = True                 # 消融 B3−G0 用
    enable_g3: bool = True                 # 消融 B3−G3 用
    gate_enabled: bool = True              # A/B 對照:False = B0(無治理,直接執行)
    execute: bool = True                   # 放行後是否真的寫入影子後台
    approval_policy: ApprovalPolicy | None = None   # None = 等待真人簽核
    persist_dir: Path | None = None
    auto_approver: str = "benchmark-auto"  # approval_policy 決定時記錄的核准者


@dataclass
class AgentGatePipeline:
    """五道關卡的執行體。一個實例 = 一個受治理的環境(影子後台 + 稽核鏈 + 佇列)。"""

    shadow: Any
    config: GateConfig = field(default_factory=GateConfig)
    engine: PolicyEngine = field(default_factory=PolicyEngine)
    audit: AuditChain = None  # type: ignore[assignment]
    queue: ApprovalQueue = field(default_factory=ApprovalQueue)
    metrics: LiveMetrics = field(default_factory=LiveMetrics)
    # 事件時鐘。線上路徑就是「現在」;回填當班歷史時由 console.OpsSimulator
    # 暫時換成事件發生的時間,好讓稽核鏈與佇列的時間戳是真的分佈在這一小時裡,
    # 而不是全部擠在服務啟動的那一秒。
    clock: Callable[[], str] = _now_iso

    def __post_init__(self) -> None:
        if self.audit is None:
            self.audit = AuditChain(persist_dir=self.config.persist_dir)

    # ------------------------------------------------------------------ 主流程
    def evaluate(self, request: ActionRequest) -> GateVerdict:
        t0 = time.perf_counter()
        stamp = {"case_id": str(request.context.get("case_id", "")), "ts": self.clock()}

        # A/B 對照:治理層關閉 = B0,Agent 直接執行(Demo 的「沒有它會怎樣」)
        if not self.config.gate_enabled:
            result = self.shadow.execute(request) if self.config.execute else {"ok": True}
            verdict = GateVerdict(
                action_id=request.action_id, allowed=True, requires_approval=False,
                risk="ungoverned", status="executed", gate_blocked_at=None,
                reasons=["治理層已關閉(B0 對照模式):動作未經任何關卡直接執行。"],
                execution_result=result,
            )
            verdict.decision_latency_ms = (time.perf_counter() - t0) * 1000
            self.metrics.record(verdict)
            return verdict

        # ---- G1 動作解析(schema 驗證;API 邊界已解析,這裡守最後一道)----
        g1_errors = validate_request(request)
        if g1_errors:
            return self._blocked(request, "G1", g1_errors, [], None, None, t0)

        # ---- G0 來源信任 ----
        trust = evaluate_trust(request.provenance_chain)
        self.audit.append(
            trace_id=request.trace_id, action_id=request.action_id,
            stage="g0_trust", actor="agentgate", **stamp,
            risk_cap=trust.risk_cap, min_trust=trust.min_trust,
            chain=[p.to_dict() for p in request.provenance_chain],
        )

        # ---- G2 政策裁決(確定性規則;同時作為 G0-R1 的風險求值器)----
        decision = self.engine.adjudicate(request, self.shadow)

        # G0-R1 權限升級阻斷:動作風險不得高於來源鏈最低信任通道可授權的等級。
        if self.config.enable_g0 and risk_exceeds(decision.risk, trust.risk_cap):
            weakest = trust.weakest_link
            finding = Finding(
                rule_id="G0-R1", title="權限升級阻斷", severity=Severity.BLOCK,
                message=(
                    f"動作風險 {decision.risk} 超過指令來源鏈可授權上限 {trust.risk_cap}"
                    f"(最弱環節:{weakest.channel.value} / {weakest.source_ref})。"
                    if weakest else
                    f"動作風險 {decision.risk} 超過空來源鏈可授權上限 {trust.risk_cap}。"
                ),
                statute="動作的風險等級,不得高於其指令來源鏈中最低信任通道所能授權的等級。",
                evidence=tuple(trust_evidence(trust)),
            )
            return self._blocked(
                request, "G0", [finding.message],
                decision.findings + [finding], trust, None, t0, risk=decision.risk,
            )

        self.audit.append(
            trace_id=request.trace_id, action_id=request.action_id,
            stage="g2_adjudication", actor="policy-engine", **stamp,
            risk=decision.risk, allowed=decision.allowed,
            requires_approval=decision.requires_approval,
            rules_triggered=[f.rule_id for f in decision.findings],
        )
        if not decision.allowed:
            block_msgs = [f.message for f in decision.findings
                          if f.severity is Severity.BLOCK] or decision.reasons
            return self._blocked(request, "G2", block_msgs, decision.findings,
                                 trust, None, t0, risk=decision.risk)

        # ---- G3 後果預演(影子環境乾跑)----
        projection = None
        findings = list(decision.findings)
        requires_approval = decision.requires_approval
        if self.config.enable_g3:
            projection, g3_findings = project_consequences(request, self.shadow)
            self.audit.append(
                trace_id=request.trace_id, action_id=request.action_id,
                stage="g3_projection", actor="shadow-env", **stamp,
                projection=projection.to_dict(),
                rules_triggered=[f.rule_id for f in g3_findings],
            )
            findings.extend(g3_findings)
            for f in g3_findings:
                if f.severity is Severity.BLOCK:
                    return self._blocked(request, "G3", [f.message], findings,
                                         trust, projection, t0, risk=decision.risk)
                if f.severity is Severity.APPROVAL_REQUIRED:
                    requires_approval = True

        # ---- 證據包組裝 ----
        evidence: list[Evidence] = trust_evidence(trust)
        if projection is not None:
            evidence.extend(projection_evidence(projection))
        for f in findings:
            evidence.extend(f.evidence)
        if request.reasoning:
            evidence.append(Evidence(
                "agent", request.agent_id,
                f"Agent 推理摘要(未經驗證的模型輸出):{request.reasoning}", weight=0.0,
            ))

        # ---- G4 人工核准 ----
        if requires_approval:
            pending = PendingApproval(
                request=request, risk=decision.risk, findings=findings,
                projection=projection, trust=trust, evidence=evidence,
                created_at=stamp["ts"],
            )
            self.queue.enqueue(pending)
            self.audit.append(
                trace_id=request.trace_id, action_id=request.action_id,
                stage="g4_pending", actor="agentgate", **stamp,
                approval_id=pending.approval_id, risk=decision.risk,
                rules_triggered=[f.rule_id for f in findings],
            )
            if self.config.approval_policy is not None:
                auto = self.config.approval_policy(pending)
                if auto is not None:
                    verdict, _ = self.decide_approval(
                        pending.approval_id, auto,
                        approver_id=self.config.auto_approver,
                        credential="__policy__",
                        reason="benchmark 核准政策自動簽核",
                        _policy_mode=True,
                    )
                    assert verdict is not None
                    verdict.decision_latency_ms = (time.perf_counter() - t0) * 1000
                    self.metrics.record(verdict)
                    return verdict
            verdict = GateVerdict(
                action_id=request.action_id, allowed=True, requires_approval=True,
                risk=decision.risk, status="pending_approval", gate_blocked_at=None,
                reasons=decision.reasons + ["等待人工核准(G4)。"],
                findings=findings, projection=projection, trust=trust,
                evidence=evidence, approval_id=pending.approval_id,
                audit_ref=self._last_hash(),
            )
            verdict.decision_latency_ms = (time.perf_counter() - t0) * 1000
            self.metrics.record(verdict)
            return verdict

        # ---- G5 執行與封存 ----
        result = self.shadow.execute(request) if self.config.execute else {"ok": True}
        record = self.audit.append(
            trace_id=request.trace_id, action_id=request.action_id,
            stage="g5_executed", actor="agentgate", **stamp,
            risk=decision.risk, result_ok=bool(result.get("ok")),
            rules_triggered=[f.rule_id for f in findings],
        )
        verdict = GateVerdict(
            action_id=request.action_id, allowed=True, requires_approval=False,
            risk=decision.risk, status="executed", gate_blocked_at=None,
            reasons=decision.reasons, findings=findings, projection=projection,
            trust=trust, evidence=evidence, audit_ref=record.hash,
            execution_result=result,
        )
        verdict.decision_latency_ms = (time.perf_counter() - t0) * 1000
        self.metrics.record(verdict)
        return verdict

    # ------------------------------------------------------------------ G3 單獨預演
    def simulate(self, request: ActionRequest) -> dict[str, Any]:
        """僅執行 G3 預演,不進入核准佇列(POST /api/gate/simulate)。"""
        errors = validate_request(request)
        if errors:
            return {"ok": False, "errors": errors}
        projection, findings = project_consequences(request, self.shadow)
        return {
            "ok": True,
            "projection": projection.to_dict(),
            "findings": [f.to_dict() for f in findings],
        }

    # ------------------------------------------------------------------ G4 簽核
    def decide_approval(
        self, approval_id: str, approved: bool, approver_id: str,
        credential: str, reason: str, _policy_mode: bool = False,
    ) -> tuple[GateVerdict | None, str]:
        """人工(或 benchmark 政策)簽核。核准 → 執行並封存;駁回 → 留證。"""
        now = self.clock()
        if _policy_mode:
            item = self.queue.get(approval_id)
            if item is None or item.status != "pending":
                return None, f"找不到待核准項目 {approval_id}"
            item.status = "approved" if approved else "rejected"
            item.approver_id = approver_id
            item.approver_name = approver_id
            item.reason = reason
        else:
            item, error = self.queue.decide(approval_id, approved, approver_id,
                                            credential, reason, at=now)
            if item is None:
                return None, error

        request = item.request
        stamp = {"case_id": str(request.context.get("case_id", "")), "ts": now}
        stage = "g4_approved" if approved else "g4_rejected"
        self.audit.append(
            trace_id=request.trace_id, action_id=request.action_id,
            stage=stage, actor=item.approver_name or approver_id, **stamp,
            approval_id=approval_id, approver_id=approver_id, reason=reason,
            sla_seconds=item.sla_seconds, elapsed_seconds=round(item.age_seconds(), 1),
        )
        if not approved:
            verdict = GateVerdict(
                action_id=request.action_id, allowed=False, requires_approval=True,
                risk=item.risk, status="rejected", gate_blocked_at="G4",
                reasons=[f"核准者駁回:{reason}" if reason else "核准者駁回。"],
                findings=item.findings, projection=item.projection, trust=item.trust,
                evidence=item.evidence, approval_id=approval_id,
                audit_ref=self._last_hash(),
            )
            self.metrics.record(verdict, is_approval_outcome=True)
            return verdict, ""

        result = self.shadow.execute(request) if self.config.execute else {"ok": True}
        record = self.audit.append(
            trace_id=request.trace_id, action_id=request.action_id,
            stage="g5_executed", actor="agentgate", **stamp,
            risk=item.risk, approval_id=approval_id,
            result_ok=bool(result.get("ok")),
        )
        verdict = GateVerdict(
            action_id=request.action_id, allowed=True, requires_approval=True,
            risk=item.risk, status="executed", gate_blocked_at=None,
            reasons=[f"經 {item.approver_name or approver_id} 核准後執行。"],
            findings=item.findings, projection=item.projection, trust=item.trust,
            evidence=item.evidence, approval_id=approval_id,
            audit_ref=record.hash, execution_result=result,
        )
        self.metrics.record(verdict, is_approval_outcome=True)
        return verdict, ""

    # ------------------------------------------------------------------ 內部
    def _blocked(
        self, request: ActionRequest, gate: str, reasons: list[str],
        findings: list[Finding], trust: Any, projection: Any,
        t0: float, risk: str = "unresolved",
    ) -> GateVerdict:
        record = self.audit.append(
            trace_id=request.trace_id, action_id=request.action_id,
            stage="g5_blocked", actor="agentgate",
            case_id=str(request.context.get("case_id", "")), ts=self.clock(),
            gate=gate, risk=risk, reasons=reasons,
            rules_triggered=[f.rule_id for f in findings],
        )
        evidence: list[Evidence] = []
        if trust is not None:
            evidence.extend(trust_evidence(trust))
        for f in findings:
            evidence.extend(f.evidence)
        verdict = GateVerdict(
            action_id=request.action_id, allowed=False, requires_approval=False,
            risk=risk, status="blocked", gate_blocked_at=gate,
            reasons=reasons, findings=findings, projection=projection,
            trust=trust, evidence=evidence, audit_ref=record.hash,
        )
        verdict.decision_latency_ms = (time.perf_counter() - t0) * 1000
        self.metrics.record(verdict)
        return verdict

    def _last_hash(self) -> str:
        return self.audit.records[-1].hash if self.audit.records else ""


__all__ = ["AgentGatePipeline", "ApprovalPolicy", "GateConfig"]
