"""最佳化器不變式測試。

這些測試守住的是「醫院可以接受的分配結果」，不是「程式沒有崩潰」。
"""

from __future__ import annotations

import pytest

from aegismesh.optimizer import _quota_cap_mbps, generate_plans, score_plan
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


# --------------------------------------------------- 難度模型：配額 / 需求 / 保底


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_equal_priority_critical_services_all_get_a_floor(scenario_id):
    """同優先級的關鍵業務不得有人吃飽、有人歸零。

    早期的單階段貪婪會讓排序第一的 P0 把配額上限吃光，同為 P0 的第二個
    服務拿到 0 —— 急診活著、加護病房斷線。兩階段允入（保底＋加碼）修掉它。
    """
    twin = DigitalTwin()
    twin.apply_scenario(SCENARIOS[scenario_id])
    for plan in generate_plans(twin):
        crit = {
            sid: st.admitted_mbps
            for sid, st in plan.projected.services.items()
            if twin.services[sid].is_critical and st.reachable
        }
        served = [v for v in crit.values() if v > 0]
        starved = [sid for sid, v in crit.items() if v <= 0]
        if served and starved:
            # 有人被餓死時，其他人不得超過自己的最低需求（表示資源真的不夠，
            # 而不是被先到先得吃光）
            for sid, v in crit.items():
                if v > 0:
                    assert v <= twin.min_mbps(sid) + 1e-6, (
                        f"{scenario_id}/{plan.strategy}：{sid} 拿到 {v:.1f} Mbps 超過保底，"
                        f"但 {starved} 卻歸零"
                    )


def test_quota_cap_binds_before_link_capacity():
    """嚴格配速的策略下，衛星流量不得超過「撐完整場事件」的速率。"""
    twin = DigitalTwin()
    twin.apply_scenario(SCENARIOS["earthquake-dual-loss"])
    sustainable = _quota_cap_mbps(twin, "w-sat", 1.0)
    link = twin.links["w-sat"]
    assert sustainable < link.effective_capacity_mbps, "配額必須比天線容量更早成為瓶頸"

    plan = next(p for p in generate_plans(twin) if p.strategy == "lowest_cost")
    load = plan.projected.links["w-sat"].load_mbps
    assert load <= sustainable + 1e-6, f"費用優先策略用了 {load:.1f} Mbps，超過可持續的 {sustainable:.1f}"
    assert plan.projected.quota_hours_left["w-sat"] >= twin.event_hours - 1e-6


def test_demand_surge_scales_requirements():
    """災害不只打斷線路，也改變需求。"""
    twin = DigitalTwin()
    base = twin.required_mbps("svc-guest")
    twin.apply_scenario(SCENARIOS["typhoon-fiber-cut"])
    assert twin.required_mbps("svc-guest") == pytest.approx(base * 2.5)
    assert twin.min_mbps("svc-guest") == pytest.approx(
        twin.services["svc-guest"].slo.min_bandwidth_mbps * 2.5
    )
    twin.clear_faults()
    assert twin.required_mbps("svc-guest") == pytest.approx(base), "清除故障後需求要回到平常水準"


def test_shared_duct_is_visible_in_the_model():
    """院區對外光纖與 5G 回程共用市政管道 —— 帳面上的備援其實會一起斷。"""
    twin = DigitalTwin()
    fiber_path = ["cpe-fiber", "pop-fiber"]
    backhaul_path = ["gnb-5g", "dc-hicloud"]
    assert twin.shared_duct_risk(fiber_path, backhaul_path) == {"duct-civic-north"}
    assert twin.shared_duct_risk(fiber_path, ["sat-vsat", "gw-sat"]) == set()
