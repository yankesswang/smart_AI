"""最佳化器不變式測試。

這些測試守住的是「醫院可以接受的分配結果」，不是「程式沒有崩潰」。
"""

from __future__ import annotations

import pytest

from aegismesh.optimizer import generate_plans, score_plan
from aegismesh.twin import DigitalTwin, SCENARIOS


def _incident_twin(scenario_id: str) -> tuple[DigitalTwin, object]:
    twin = DigitalTwin()
    twin.apply_faults(SCENARIOS[scenario_id].faults)
    return twin, twin.evaluate("incident")


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_scenario_yields_a_plan(scenario_id):
    twin, _ = _incident_twin(scenario_id)
    plans = generate_plans(twin)
    assert plans, f"{scenario_id} 必須至少產生一個候選計畫"
    assert all(p.projected is not None for p in plans), "每個計畫都必須經過孿生推演"


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_best_plan_restores_all_critical_services(scenario_id):
    twin, incident = _incident_twin(scenario_id)
    plans = generate_plans(twin)
    for p in plans:
        p.score = score_plan(incident, p.projected, p)
    best = max(plans, key=lambda p: p.score)
    assert best.projected.critical_availability_pct == 100.0, (
        f"{scenario_id}：最佳計畫必須讓生命關鍵業務全數恢復"
    )


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_no_priority_inversion_in_any_plan(scenario_id):
    """不得出現『訪客 Wi-Fi 有頻寬、批價系統掛零』這種對醫院不可接受的分配。"""
    twin, _ = _incident_twin(scenario_id)
    for plan in generate_plans(twin):
        services = plan.projected.services
        denied = {
            sid for sid, st in services.items()
            if st.admitted_mbps <= 0.0 and st.reachable
        }
        for d in denied:
            d_pri = twin.services[d].priority
            offenders = [
                sid for sid, st in services.items()
                if twin.services[sid].priority > d_pri and st.admitted_mbps > 0.0
            ]
            assert not offenders, (
                f"{plan.id}：{d}（P{d_pri}）零頻寬，但較低優先級的 {offenders} 仍有配置"
            )


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_no_link_is_oversubscribed(scenario_id):
    """計畫不得產生超載鏈路 —— 超載代表丟包，等於把問題換個地方發生。"""
    twin, _ = _incident_twin(scenario_id)
    for plan in generate_plans(twin):
        for lid, ls in plan.projected.links.items():
            assert ls.utilization_pct <= 100.0, f"{plan.id}：鏈路 {lid} 超載 {ls.utilization_pct:.0f}%"


def test_admitted_bandwidth_never_exceeds_requirement():
    twin, _ = _incident_twin("typhoon-fiber-cut")
    for plan in generate_plans(twin):
        for sid, st in plan.projected.services.items():
            cap = twin.services[sid].slo.required_bandwidth_mbps
            assert st.admitted_mbps <= cap + 1e-6, f"{sid} 配置超過需求"


def test_score_rewards_critical_recovery_over_cost():
    twin, incident = _incident_twin("typhoon-fiber-cut")
    plans = generate_plans(twin)
    for p in plans:
        p.score = score_plan(incident, p.projected, p)
    recovering = [p for p in plans if p.projected.critical_availability_pct == 100.0]
    assert recovering and all(p.score > 0 for p in recovering), (
        "恢復關鍵業務的計畫評分必須為正，成本懲罰不該壓過救命價值"
    )


def test_earthquake_forces_satellite_and_sheds_low_priority():
    """雙路中斷時只剩衛星：關鍵業務上星，低優先級業務必須被犧牲。"""
    twin, incident = _incident_twin("earthquake-dual-loss")
    plans = generate_plans(twin)
    for p in plans:
        p.score = score_plan(incident, p.projected, p)
    best = max(plans, key=lambda p: p.score)

    for sid, st in best.projected.services.items():
        if twin.services[sid].is_critical:
            assert st.admitted_mbps > 0.0, f"關鍵業務 {sid} 不得被停用"
            kinds = {l.kind.value for l in twin.path_links(st.path or [])}
            assert "satellite" in kinds, "唯一生路是衛星"
    assert best.projected.services["svc-guest"].admitted_mbps == 0.0, "訪客 Wi-Fi 應被停用"
