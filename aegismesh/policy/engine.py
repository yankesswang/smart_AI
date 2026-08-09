"""Policy-as-Code 評估引擎。

Agent 產生的每個計畫都必須通過這裡才可能被執行。規則寫在 policies.yaml，
程式只實作有限幾種 kind —— 這讓治理邏輯可被稽核、可被法遵人員閱讀，
而不是散落在 prompt 裡面。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..domain import NetworkSnapshot, RecoveryPlan
from ..twin.engine import DigitalTwin

_POLICY_FILE = Path(__file__).with_name("policies.yaml")

DENY = "deny"
REQUIRE_APPROVAL = "require_approval"
ALLOW = "allow"

_SEVERITY_ORDER = {ALLOW: 0, REQUIRE_APPROVAL: 1, DENY: 2}


@dataclass
class Finding:
    rule_id: str
    rule_name: str
    effect: str
    weight: float
    message: str

    def render(self) -> str:
        tag = "拒絕" if self.effect == DENY else "需核准"
        return f"[{self.rule_id}｜{tag}] {self.rule_name}：{self.message}"


@dataclass
class PolicyDecision:
    decision: str
    findings: list[Finding]
    risk_score: float

    @property
    def messages(self) -> list[str]:
        return [f.render() for f in self.findings]


class PolicyEngine:
    def __init__(self, policy_file: Path | None = None) -> None:
        raw = yaml.safe_load((policy_file or _POLICY_FILE).read_text(encoding="utf-8"))
        self.version = raw.get("version", 1)
        self.rules: list[dict[str, Any]] = raw.get("rules", [])

    # -------------------------------------------------------------- 規則實作

    def _eval_rule(
        self,
        rule: dict[str, Any],
        twin: DigitalTwin,
        plan: RecoveryPlan,
        before: NetworkSnapshot,
        after: NetworkSnapshot,
    ) -> str | None:
        """命中回傳說明字串，未命中回傳 None。"""
        kind = rule["kind"]
        params = rule.get("params") or {}

        if kind == "critical_regression":
            if after.critical_availability_pct < before.critical_availability_pct - 1e-6:
                return (
                    f"關鍵業務可用率由 {before.critical_availability_pct:.0f}% "
                    f"降至 {after.critical_availability_pct:.0f}%"
                )
            return None

        if kind == "priority_inversion":
            denied = [
                sid for sid in after.services
                if after.services[sid].admitted_mbps <= 0.0 and after.services[sid].reachable
            ]
            for d in denied:
                d_pri = twin.services[d].priority
                worse = [
                    sid for sid, st in after.services.items()
                    if twin.services[sid].priority > d_pri and st.admitted_mbps > 0.0
                ]
                if worse:
                    names = "、".join(twin.services[w].name for w in worse)
                    return f"{twin.services[d].name}（P{d_pri}）零頻寬，但較低優先級的 {names} 仍有配置"
            return None

        if kind == "critical_floor":
            for sid, st in after.services.items():
                svc = twin.services[sid]
                if svc.priority == 0 and st.reachable and st.admitted_mbps < svc.slo.min_bandwidth_mbps:
                    return (
                        f"{svc.name} 配置 {st.admitted_mbps:.0f} Mbps，"
                        f"低於臨床最低需求 {svc.slo.min_bandwidth_mbps:.0f} Mbps"
                    )
            return None

        if kind == "satellite_activation":
            sat_links = {lid for lid, l in twin.links.items() if l.kind.value == "satellite"}
            using = [
                twin.services[sid].name for sid, st in after.services.items()
                if st.admitted_mbps > 0 and sat_links.intersection(st.link_ids)
            ]
            if using:
                return f"下列業務將改由衛星鏈路承載：{'、'.join(using)}"
            return None

        if kind == "cost_ceiling":
            ceiling = float(params.get("monthly_ntd", 0))
            if after.monthly_cost_ntd > ceiling:
                return f"推演月成本 NT${after.monthly_cost_ntd:,.0f} 超過上限 NT${ceiling:,.0f}"
            return None

        if kind == "cost_multiplier":
            factor = float(params.get("factor", 3.0))
            if before.monthly_cost_ntd > 0 and after.monthly_cost_ntd > before.monthly_cost_ntd * factor:
                ratio = after.monthly_cost_ntd / before.monthly_cost_ntd
                return f"成本增為事故前的 {ratio:.1f} 倍（上限 {factor:.1f} 倍）"
            return None

        if kind == "blast_radius":
            cap = int(params.get("max_actions", 6))
            if len(plan.actions) > cap:
                return f"計畫含 {len(plan.actions)} 個變更動作，超過上限 {cap}"
            return None

        if kind == "service_suspension":
            max_pri = int(params.get("max_priority", 3))
            suspended = [
                twin.services[sid].name for sid, st in after.services.items()
                if st.admitted_mbps <= 0.0 and twin.services[sid].priority <= max_pri
            ]
            if suspended:
                return f"將停用：{'、'.join(suspended)}"
            return None

        raise ValueError(f"policies.yaml 使用了未實作的規則種類：{kind}")

    # -------------------------------------------------------------- 對外介面

    def evaluate(
        self,
        twin: DigitalTwin,
        plan: RecoveryPlan,
        before: NetworkSnapshot,
        after: NetworkSnapshot | None,
    ) -> PolicyDecision:
        if after is None:
            return PolicyDecision(
                DENY,
                [Finding("POL-000", "計畫未經推演", DENY, 100, "缺少孿生推演結果，禁止執行")],
                100.0,
            )

        findings: list[Finding] = []
        for rule in self.rules:
            msg = self._eval_rule(rule, twin, plan, before, after)
            if msg:
                findings.append(
                    Finding(rule["id"], rule["name"], rule["effect"], float(rule.get("weight", 10)), msg)
                )

        decision = ALLOW
        for f in findings:
            if _SEVERITY_ORDER[f.effect] > _SEVERITY_ORDER[decision]:
                decision = f.effect

        risk = min(100.0, sum(f.weight for f in findings))
        return PolicyDecision(decision, findings, risk)

    def apply_to(
        self,
        twin: DigitalTwin,
        plan: RecoveryPlan,
        before: NetworkSnapshot,
    ) -> PolicyDecision:
        """評估並把結果寫回計畫物件，方便後續稽核與 UI 呈現。"""
        result = self.evaluate(twin, plan, before, plan.projected)
        plan.policy_decision = result.decision
        plan.policy_findings = result.messages
        plan.risk_score = result.risk_score
        return result
