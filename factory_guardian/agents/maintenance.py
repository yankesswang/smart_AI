"""Maintenance & Work Order Agent（規格 §4.5）。

自動產生設備、問題、Evidence、Priority、Required Skill、Suggested Parts、Estimated Repair，
並帶出對應 SOP 與安全注意事項；高風險維修動作保留人工核准與稽核。

Work Order Completeness 是 §10 的 KPI，由 ``WorkOrder.completeness()`` 直接量測，
不是自評分數。
"""

from __future__ import annotations

from ..domain import (
    AnomalyEvent,
    Diagnosis,
    Evidence,
    FactorySnapshot,
    Severity,
    WorkOrder,
)
from ..twin.faults import FAULTS
from .base import Agent


class MaintenanceAgent(Agent):
    name = "maintenance-agent"
    role = "維修任務與工單"

    def __init__(self, ctx=None) -> None:
        super().__init__(ctx)
        self.seq = 0

    def create_work_order(
        self,
        event: AnomalyEvent,
        diagnosis: Diagnosis,
        snapshot: FactorySnapshot,
        order_delay_min: float = 0.0,
    ) -> WorkOrder:
        top = diagnosis.top
        machine_id = diagnosis.machine_id
        machine = snapshot.machines[machine_id]

        with self.timed():
            self.seq += 1
            fault_id = top.fault_id if top else "unknown"
            model = FAULTS.get(fault_id)

            with self.tool("kb.sop_for_fault", f"fault={fault_id}"):
                sops = self.ctx.kb.sop_for_fault(fault_id)
            with self.tool("kb.cases_for_fault", f"fault={fault_id}"):
                cases = self.ctx.kb.cases_for_fault(fault_id, machine_id, limit=3)

            # 工時估計：手冊標準工時與近期實際工時取加權平均（歷史更貼近這台機器）
            standard = model.repair_min if model else 40.0
            if cases:
                actual = sum(c.repair_min for c in cases) / len(cases)
                estimate = 0.4 * standard + 0.6 * actual
            else:
                estimate = standard

            parts = list(model.typical_parts) if model else []
            if cases:
                for case in cases:
                    for part in case.parts_used:
                        if part not in parts:
                            parts.append(part)

            priority = self._priority(event.severity, machine.health, order_delay_min)

            evidence: list[Evidence] = []
            if top:
                evidence.extend(top.evidence[:4])
            evidence.append(
                Evidence("sensor", f"{machine_id}.health",
                         f"事件當下健康度 {machine.health:.0f}，觸發條件：{'、'.join(event.triggers)}")
            )
            for case in cases[:2]:
                evidence.append(Evidence("history", case.case_id, f"歷史實際工時 {case.repair_min:g} 分鐘。"))

            precautions = self._precautions(sops)

            work_order = WorkOrder(
                work_order_id=f"WO-{event.tick:03d}-{self.seq:02d}",
                machine_id=machine_id,
                problem=f"{top.label if top else '未確認異常'}（信心度 {top.confidence:.0%}）" if top else "未確認異常",
                priority=priority,
                required_skill=model.required_skill if model else "mechanical-tech (L2)",
                suggested_parts=parts,
                estimated_repair_min=estimate,
                sop_refs=[d.ref for d in sops] or (list(model.manual_refs) if model else []),
                evidence=evidence,
                safety_precautions=precautions,
            )

        self.log(
            "work_order",
            work_order_id=work_order.work_order_id,
            machine_id=machine_id,
            fault_id=fault_id,
            priority=priority,
            estimated_repair_min=round(estimate, 1),
            completeness_pct=round(work_order.completeness(), 1),
            parts=parts,
            sop_refs=work_order.sop_refs,
        )
        return work_order

    @staticmethod
    def _priority(severity: Severity, health: float, order_delay_min: float) -> str:
        if severity is Severity.CRITICAL or health < 60 or order_delay_min > 30:
            return "P1"
        if severity is Severity.WARNING or health < 85 or order_delay_min > 0:
            return "P2"
        return "P3"

    def _precautions(self, sops) -> list[str]:
        precautions: list[str] = []
        for doc in sops:
            for line in doc.body.split("\n"):
                if any(key in line for key in ("LOTO", "上鎖", "降至", "洩壓", "驗電", "通報")):
                    precautions.append(f"[{doc.ref}] {line.strip()}")
        for doc in self.ctx.kb.safety_sops():
            for line in doc.body.split("\n"):
                if "LOTO" in line or "防護具" in line:
                    precautions.append(f"[{doc.ref}] {line.strip()}")
        # 去重但保留順序
        seen: set[str] = set()
        unique = []
        for item in precautions:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique[:5]


__all__ = ["MaintenanceAgent"]
