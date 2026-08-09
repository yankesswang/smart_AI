"""端到端閉環測試 —— 在完全沒有 OPENAI_API_KEY 的環境下也必須通過。"""

from __future__ import annotations

import pytest

from aegismesh.audit import AuditLog
from aegismesh.config import Settings
from aegismesh.orchestrator import Orchestrator, auto_approve, auto_reject
from aegismesh.twin import SCENARIOS


def _offline_settings(tmp_path) -> Settings:
    """刻意不給金鑰：驗證離線退化路徑，並確保測試不會打外部 API。"""
    return Settings(
        openai_api_key=None,
        openai_model="gpt-4o",
        openai_base_url=None,
        allow_offline_llm=True,
        audit_dir=tmp_path,
        require_approval=True,
    )


def _orch(tmp_path, name="audit.jsonl") -> Orchestrator:
    settings = _offline_settings(tmp_path)
    return Orchestrator(settings=settings, audit_log=AuditLog(tmp_path / name))


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_closed_loop_recovers_critical_services(tmp_path, scenario_id):
    orch = _orch(tmp_path, f"{scenario_id}.jsonl")
    result = orch.run(SCENARIOS[scenario_id], approval_fn=auto_approve)

    assert result.incident.critical_availability_pct < 100.0, "情境必須先造成傷害"
    assert result.succeeded, f"{scenario_id}：閉環未能恢復關鍵業務"
    assert result.final.critical_availability_pct == 100.0
    assert result.executed_plan is not None


def test_loop_runs_all_seven_stages(tmp_path):
    orch = _orch(tmp_path)
    seen: list[str] = []
    orch.run(SCENARIOS["typhoon-fiber-cut"], approval_fn=auto_approve,
             on_stage=lambda kind, _: seen.append(kind))

    for stage in ("baseline", "incident", "plans", "approval", "execute"):
        assert stage in seen, f"缺少閉環階段：{stage}"

    recorded = {e["stage"] for e in orch.audit.entries()}
    assert {"baseline", "fault_injection", "observe", "assess",
            "plan+simulate", "govern", "approve", "execute", "verify"} <= recorded


def test_rejected_approval_halts_execution(tmp_path):
    """人工拒絕核准 → 網路必須維持事故狀態，一個動作都不能執行。"""
    orch = _orch(tmp_path)
    result = orch.run(SCENARIOS["typhoon-fiber-cut"], approval_fn=auto_reject)

    assert result.executed_plan is None
    assert not result.approved
    assert not result.succeeded
    assert result.halted_reason and "核准" in result.halted_reason
    assert result.final.critical_availability_pct == result.incident.critical_availability_pct


def test_audit_chain_intact_after_full_loop(tmp_path):
    orch = _orch(tmp_path)
    orch.run(SCENARIOS["earthquake-dual-loss"], approval_fn=auto_approve)
    ok, msg = orch.verify_audit()
    assert ok, msg
    assert len(orch.audit.entries()) >= 9


def test_runs_fully_offline_without_api_key(tmp_path):
    """評審現場斷網也要跑得完：所有 Agent 都必須產出敘述。"""
    orch = _orch(tmp_path)
    assert not orch.llm.online
    result = orch.run(SCENARIOS["typhoon-fiber-cut"], approval_fn=auto_approve)

    assert result.succeeded
    assert len(result.agent_results) >= 5
    for r in result.agent_results:
        assert r.narrative.strip(), f"{r.agent} 沒有產出敘述"
        assert r.llm_mode == "offline"


def test_agent_facts_are_deterministic_across_runs(tmp_path):
    """同一情境跑兩次，引擎算出的事實必須完全一致（LLM 不參與決策的證明）。"""
    a = _orch(tmp_path, "a.jsonl").run(SCENARIOS["typhoon-fiber-cut"], approval_fn=auto_approve)
    b = _orch(tmp_path, "b.jsonl").run(SCENARIOS["typhoon-fiber-cut"], approval_fn=auto_approve)

    assert a.summary()["critical_availability_pct"] == b.summary()["critical_availability_pct"]
    assert a.executed_plan.id == b.executed_plan.id
    assert a.final.to_dict()["services"] == b.final.to_dict()["services"]


def test_verification_compares_prediction_against_measurement(tmp_path):
    """孿生推演的預測必須與執行後實測一致，否則『先推演後執行』不成立。"""
    orch = _orch(tmp_path)
    result = orch.run(SCENARIOS["typhoon-fiber-cut"], approval_fn=auto_approve)

    verify = next(r for r in result.agent_results if r.stage == "verify")
    assert verify.facts["prediction_accurate"], (
        f"推演與實測不符：{verify.facts['prediction_drift']}"
    )


def test_concurrent_runs_do_not_share_an_audit_file(tmp_path):
    """同一秒內的兩次獨立事故必須寫入不同軌跡檔。

    共用檔案會讓查核人員讀到一條從未發生過的連續事故時間線 ——
    雜湊鏈仍會驗證通過，所以這是靜默的稽核污染，必須用測試守住。
    """
    settings = _offline_settings(tmp_path)
    a = Orchestrator(settings=settings)
    b = Orchestrator(settings=settings)

    assert a.audit.path != b.audit.path

    a.run(SCENARIOS["typhoon-fiber-cut"], approval_fn=auto_approve)
    b.run(SCENARIOS["backbone-brownout"], approval_fn=auto_approve)

    for orch, expected in ((a, "typhoon-fiber-cut"), (b, "backbone-brownout")):
        scenarios = {
            e["detail"]["scenario"]
            for e in orch.audit.entries() if e["stage"] == "fault_injection"
        }
        assert scenarios == {expected}, f"軌跡檔混入了其他事故：{scenarios}"
