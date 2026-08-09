"""數位孿生引擎的行為測試 —— 這些不變式是整套系統可信度的地基。"""

from __future__ import annotations

import pytest

from aegismesh.domain import Fault, LinkState
from aegismesh.twin import DigitalTwin, SCENARIOS


def test_baseline_all_services_healthy():
    snap = DigitalTwin().evaluate("baseline")
    assert snap.critical_availability_pct == 100.0
    assert snap.slo_compliance_pct == 100.0
    assert all(s.reachable and s.slo_met for s in snap.services.values())


def test_baseline_prefers_fiber_the_cheapest_path():
    twin = DigitalTwin()
    snap = twin.evaluate("baseline")
    for st in snap.services.values():
        kinds = {l.kind.value for l in twin.path_links(st.path or [])}
        assert "fiber" in kinds, "正常狀態下應走成本最低、延遲最低的固網"


def test_fiber_cut_makes_every_service_unreachable():
    twin = DigitalTwin()
    twin.apply_faults([Fault("w-fiber", LinkState.DOWN)])
    snap = twin.evaluate("incident")
    # 路由表尚未更新，所有業務仍指向斷掉的固網路徑
    assert all(not s.reachable for s in snap.services.values())
    assert snap.critical_availability_pct == 0.0


def test_congestion_raises_latency_nonlinearly():
    """M/M/1 近似：使用率越高，延遲成長越快。這是允入控制存在的理由。"""
    twin = DigitalTwin()
    link = twin.links["w-fiber"]
    low, _, _ = DigitalTwin._congested_metrics(link, link.capacity_mbps * 0.30)
    mid, _, _ = DigitalTwin._congested_metrics(link, link.capacity_mbps * 0.60)
    high, _, _ = DigitalTwin._congested_metrics(link, link.capacity_mbps * 0.90)
    assert low < mid < high
    assert (high - mid) > (mid - low), "延遲成長必須是非線性的"


def test_oversubscription_causes_packet_loss():
    twin = DigitalTwin()
    link = twin.links["w-sat"]
    _, loss, util = DigitalTwin._congested_metrics(link, link.capacity_mbps * 1.5)
    assert util > 100.0
    assert loss > 30.0, "超載 50% 應造成顯著丟包"


def test_down_link_yields_zero_capacity():
    twin = DigitalTwin()
    twin.apply_faults([Fault("w-5g", LinkState.DOWN)])
    assert twin.links["w-5g"].effective_capacity_mbps == 0.0
    assert not twin.links["w-5g"].is_usable


def test_clone_is_isolated_from_live_twin():
    """影子孿生必須完全隔離，否則『先推演後執行』就是假的。"""
    twin = DigitalTwin()
    shadow = twin.clone()
    shadow.apply_faults([Fault("w-fiber", LinkState.DOWN)])
    shadow.routing.admitted["svc-guest"] = 0.0

    assert twin.links["w-fiber"].state is LinkState.UP
    assert twin.routing.admitted["svc-guest"] > 0.0
    assert twin.evaluate("live").critical_availability_pct == 100.0


def test_candidate_paths_exclude_broken_links():
    twin = DigitalTwin()
    twin.apply_faults([Fault("w-fiber", LinkState.DOWN)])
    for path in twin.candidate_paths("svc-ed-vitals"):
        assert all(l.is_usable for l in twin.path_links(path))


def test_candidate_paths_still_find_satellite_after_both_terrestrial_links_fail():
    twin = DigitalTwin()
    twin.apply_faults([
        Fault("w-fiber", LinkState.DOWN),
        Fault("w-5g", LinkState.DOWN),
    ])
    paths = twin.candidate_paths("svc-ed-vitals")
    assert paths, "較長的衛星路徑仍可用時，不應被較短的中斷路徑擠出候選清單"
    assert all("w-sat" in {link.id for link in twin.path_links(path)} for path in paths)


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_scenario_degrades_something(scenario_id):
    twin = DigitalTwin()
    baseline = twin.evaluate("baseline")
    twin.apply_faults(SCENARIOS[scenario_id].faults)
    incident = twin.evaluate("incident")
    assert incident.slo_compliance_pct < baseline.slo_compliance_pct, "情境必須真的造成劣化"


def test_faults_translate_to_runnable_netem_commands():
    """本機模擬要能一對一搬到 containerlab，否則落地路徑就斷了。

    語法必須是真的可執行的 —— netem 沒有「打幾折」的寫法，速率一定是絕對值。
    """
    down = Fault("w-fiber", LinkState.DOWN)
    assert down.netem_command("eth1").startswith("ip link set dev eth1 down")

    degraded = Fault("w-5g", LinkState.DEGRADED, extra_latency_ms=25,
                     extra_loss_pct=0.25, capacity_factor=0.40)
    cmd = degraded.netem_command("eth2", capacity_mbps=600)
    assert "tc qdisc replace dev eth2 root netem" in cmd
    assert "delay 25ms" in cmd and "loss 0.25%" in cmd
    assert "rate 240mbit" in cmd, "0.40 × 600 Mbps 應換算成絕對速率"
    assert "x-of-baseline" not in cmd, "不得輸出不合法的 tc 語法"


def test_netem_command_flags_missing_capacity_instead_of_faking_syntax():
    """沒有容量資訊時寧可明講缺什麼，也不要吐出一段跑不動的假指令。"""
    cmd = Fault("w-5g", LinkState.DEGRADED, capacity_factor=0.4).netem_command()
    assert "基準容量" in cmd
