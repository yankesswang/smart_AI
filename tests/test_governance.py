"""治理層與稽核軌跡測試 —— 證明 Agent 無法繞過政策，也無法竄改紀錄。"""

from __future__ import annotations

import json

from aegismesh.audit import AuditLog
from aegismesh.domain import Action, RecoveryPlan
from aegismesh.optimizer import generate_plans
from aegismesh.policy.engine import ALLOW, DENY, REQUIRE_APPROVAL, PolicyEngine
from aegismesh.twin import DigitalTwin, SCENARIOS


def _incident(scenario_id="typhoon-fiber-cut"):
    twin = DigitalTwin()
    twin.apply_faults(SCENARIOS[scenario_id].faults)
    return twin, twin.evaluate("incident")


def test_every_yaml_rule_kind_is_implemented():
    """policies.yaml 若寫了未實作的規則種類，必須立刻炸掉而不是靜默略過。"""
    twin, incident = _incident()
    engine = PolicyEngine()
    plan = generate_plans(twin)[0]
    engine.evaluate(twin, plan, incident, plan.projected)   # 未實作的 kind 會 raise
    assert len(engine.rules) >= 7


def test_unsimulated_plan_is_denied():
    """沒經過孿生推演的計畫一律拒絕 —— 這是『先推演後執行』的強制點。"""
    twin, incident = _incident()
    bogus = RecoveryPlan(id="p-bogus", strategy="manual", summary="未推演", actions=[])
    result = PolicyEngine().evaluate(twin, bogus, incident, None)
    assert result.decision == DENY


def test_satellite_usage_requires_human_approval():
    twin, incident = _incident("earthquake-dual-loss")
    engine = PolicyEngine()
    for plan in generate_plans(twin):
        decision = engine.apply_to(twin, plan, incident)
        if any("衛星" in m for m in decision.messages):
            assert decision.decision in {REQUIRE_APPROVAL, DENY}
            return
    raise AssertionError("雙路中斷情境必定使用衛星，應觸發 POL-010")


def test_policy_decision_is_worst_of_all_findings():
    twin, incident = _incident()
    engine = PolicyEngine()
    for plan in generate_plans(twin):
        d = engine.evaluate(twin, plan, incident, plan.projected)
        effects = {f.effect for f in d.findings}
        if DENY in effects:
            assert d.decision == DENY
        elif REQUIRE_APPROVAL in effects:
            assert d.decision == REQUIRE_APPROVAL
        else:
            assert d.decision == ALLOW


def test_risk_score_is_bounded():
    twin, incident = _incident()
    engine = PolicyEngine()
    for plan in generate_plans(twin):
        d = engine.evaluate(twin, plan, incident, plan.projected)
        assert 0.0 <= d.risk_score <= 100.0


def test_priority_inversion_rule_catches_a_hand_crafted_bad_plan():
    """直接餵一個有優先級反轉的計畫，確認 POL-002／POL-003 真的擋得住。

    關鍵是把 P0 導到「可達」的 5G 路徑後才餓死它 —— 若它本來就因斷纜而失聯，
    政策不該歸咎於這個計畫（否則全網中斷時所有計畫都會被拒，系統直接死鎖）。
    """
    twin, incident = _incident()
    via_5g = ["ward-ed", "core-sw", "cpe-5g", "gnb-5g", "dc-hicloud"]
    via_sat = ["blk-adm", "core-sw", "sat-vsat", "gw-sat", "dc-hicloud"]
    bad = RecoveryPlan(
        id="p-bad", strategy="manual", summary="人為錯誤計畫",
        actions=[
            Action("reroute", "svc-ed-vitals", {"path": via_5g}),  # P0 路徑可達…
            Action("throttle", "svc-ed-vitals", {"mbps": 0.0}),    # …卻被歸零
            Action("reroute", "svc-guest", {"path": via_sat}),
            Action("admit", "svc-guest", {"mbps": 50.0}),          # P4 反而拿到頻寬
        ],
    )
    shadow = twin.clone()
    shadow.apply_plan(bad)
    bad.projected = shadow.evaluate("bad")

    assert bad.projected.services["svc-ed-vitals"].reachable, "測試前提：P0 路徑必須可達"

    result = PolicyEngine().evaluate(twin, bad, incident, bad.projected)
    assert result.decision == DENY
    assert any("POL-002" in m for m in result.messages), "應偵測到優先級反轉"
    assert any("POL-003" in m for m in result.messages), "應偵測到 P0 低於臨床最低頻寬"


def test_unreachable_critical_service_is_not_blamed_on_the_plan():
    """斷纜造成的失聯不該讓計畫被拒 —— 否則全網中斷時系統會死鎖，一個方案都執行不了。"""
    twin, incident = _incident("earthquake-dual-loss")
    engine = PolicyEngine()
    plans = generate_plans(twin)
    for plan in plans:
        engine.apply_to(twin, plan, incident)

    assert any(p.policy_decision != DENY for p in plans), (
        "雙路中斷情境下至少要有一個計畫可以進入核准流程，否則系統死鎖"
    )


def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("observe", "TelemetryAgent", {"anomalies": 2})
    log.record("plan", "PlanningAgent", {"plans": 3})
    log.record("execute", "Orchestrator", {"plan_id": "plan-01"})

    ok, msg = log.verify()
    assert ok, msg

    # 竄改中間一筆內容，雜湊鏈必須斷裂
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
    entry = json.loads(lines[1])
    entry["detail"]["plans"] = 99
    lines[1] = json.dumps(entry, ensure_ascii=False)
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, msg = AuditLog(tmp_path / "audit.jsonl").verify()
    assert not ok and "竄改" in msg


def test_audit_chain_detects_deleted_entry(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(4):
        log.record("stage", "actor", {"i": i})

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
    del lines[1]                                     # 抽掉一筆
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, _ = AuditLog(tmp_path / "audit.jsonl").verify()
    assert not ok, "刪除紀錄必須被偵測出來"


def test_audit_reopens_from_last_nonempty_entry(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    first = log.record("observe", "agent", {"value": "中文內容"})
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n")

    second = AuditLog(path).record("verify", "agent", {"ok": True})
    assert second["prev_hash"] == first["hash"]
    assert AuditLog(path).verify()[0]
    assert AuditLog(path).count() == 2


def test_shared_duct_spof_is_flagged():
    """所有救命服務都擠在同一條市政管道時，治理層必須攔下來要求人工確認。"""
    twin = DigitalTwin()
    twin.apply_scenario(SCENARIOS["typhoon-fiber-cut"])
    incident = twin.evaluate("incident")
    engine = PolicyEngine()

    flagged = False
    for plan in generate_plans(twin):
        engine.apply_to(twin, plan, incident)
        hits = [f for f in plan.policy_findings if "POL-004" in f]
        if hits:
            flagged = True
            assert "同一條實體管道" in hits[0]
            assert plan.policy_decision != ALLOW
    assert flagged, "固網斷線後全數改走 5G，其回程與固網共用管道，POL-004 應該要命中"


def test_quota_exhaustion_is_flagged_against_event_duration():
    """衛星撐不過整場事件時要示警 —— 容量看起來還很夠，配額卻早就見底。"""
    twin = DigitalTwin()
    twin.apply_scenario(SCENARIOS["earthquake-dual-loss"])
    incident = twin.evaluate("incident")
    engine = PolicyEngine()

    plan = next(p for p in generate_plans(twin) if p.strategy == "protect_critical")
    engine.apply_to(twin, plan, incident)
    hits = [f for f in plan.policy_findings if "POL-013" in f]
    assert hits, "救命優先策略透支配額，必須被 POL-013 攔下"
    assert "小時" in hits[0]
    # 使用率看起來不高，但配額其實撐不住 —— 這正是人工判斷會漏掉的地方
    assert plan.projected.links["w-sat"].utilization_pct < 90.0
    assert plan.projected.quota_hours_left["w-sat"] < twin.event_hours
