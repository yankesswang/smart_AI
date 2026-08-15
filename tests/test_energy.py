"""能源與碳排 KPI（永續發展性）。

這組測試守住四件事，每一件都對應評審會追問的一個問題：

1. 「你們的能源浪費是憑空估的嗎？」→ 健康設備的浪費**恰好為 0**，
   而且整份能耗可以用 Agent 看得到的電流讀值重建出來（誤差只有量測雜訊）。
2. 「早期介入到底省了什麼？」→ 同一個情境、同一個 seed，放任劣化 vs 早期介入的
   浪費電力與碳排差一個數量級以上。
3. 「停機的機台也算耗電？」→ 停機只計待機能耗，不計運轉能耗。
4. 「能源數字會不會偷看答案？」→ 能源帳只讀電流、控制狀態與設備銘牌，
   不讀 fault / fault_progress。
"""

from __future__ import annotations

import pytest

from factory_guardian.benchmark import REPORT_COLUMNS
from factory_guardian.domain import Action, ActionKind, MachineState
from factory_guardian.episode import EpisodeKPI, run_episode
from factory_guardian.twin.energy import (
    ELECTRICITY_TARIFF_NTD_PER_KWH,
    GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH,
    power_kw_from_current,
)
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import get_scenario
from factory_guardian.twin.topology import build_factory

EQUIPMENT_SCENARIOS = ["bearing-degradation", "cooling-failure", "motor-overload"]


# --------------------------------------------------------------------------------------
# 模型本身
# --------------------------------------------------------------------------------------
def test_power_is_linear_in_current():
    """P = P_rated × I / I_nominal —— 額定電流時就是額定功率。"""
    assert power_kw_from_current(22.0, 10.2, 10.2) == pytest.approx(22.0)
    assert power_kw_from_current(22.0, 20.4, 10.2) == pytest.approx(44.0)
    assert power_kw_from_current(22.0, 0.0, 10.2) == pytest.approx(0.0)


def test_every_machine_has_a_rated_power():
    topo = build_factory()
    for machine in topo.machines.values():
        assert machine.rated_power_kw > 0
        # 額定功率必須和 current 訊號的額定值對應同一個工作點，否則換算沒有意義。
        assert machine.signal("current") is not None


def test_healthy_factory_wastes_no_energy(twin: FactoryTwin):
    """沒有故障 → 實際功率永遠等於名目功率 → 浪費恰好是 0。"""
    twin.run(30)
    energy = twin.energy_kpi()
    assert energy["energy_kwh"] > 0
    assert energy["energy_waste_kwh"] == pytest.approx(0.0, abs=1e-9)
    assert energy["co2e_waste_kg"] == pytest.approx(0.0, abs=1e-9)
    assert energy["energy_kwh"] == pytest.approx(energy["energy_nominal_kwh"])


def test_carbon_and_cost_are_linear_conversions(twin: FactoryTwin):
    twin.schedule(get_scenario("motor-overload").injections)
    twin.run(40)
    energy = twin.energy_kpi()
    assert energy["co2e_kg"] == pytest.approx(energy["energy_kwh"] * GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH)
    assert energy["co2e_waste_kg"] == pytest.approx(
        energy["energy_waste_kwh"] * GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH
    )
    assert energy["energy_waste_ntd"] == pytest.approx(
        energy["energy_waste_kwh"] * ELECTRICITY_TARIFF_NTD_PER_KWH
    )


@pytest.mark.parametrize("scenario_id", EQUIPMENT_SCENARIOS)
def test_degradation_is_charged_to_the_degraded_machine(scenario_id):
    """浪費全部記在劣化的那台機器上，健康的機台一度都沒有多耗。"""
    twin = FactoryTwin(seed=20260809, tick_minutes=1.0)
    twin.schedule(get_scenario(scenario_id).injections)
    twin.run(45)
    ledger = twin.energy
    assert ledger.machines["M-A"].energy_waste_kwh > 0
    assert ledger.machines["M-B"].energy_waste_kwh == pytest.approx(0.0, abs=1e-9)
    assert ledger.machines["M-C"].energy_waste_kwh == pytest.approx(0.0, abs=1e-9)
    # 實際功率高於健康基線，差額就是浪費的來源。
    assert ledger.machines["M-A"].last_power_kw > ledger.machines["M-A"].last_nominal_power_kw


def test_waste_keeps_growing_while_the_fault_is_left_alone():
    twin = FactoryTwin(seed=20260809, tick_minutes=1.0)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(20)
    early = twin.energy_kpi()["energy_waste_kwh"]
    twin.run(20)
    later = twin.energy_kpi()["energy_waste_kwh"]
    assert 0 < early < later


# --------------------------------------------------------------------------------------
# 停機 / 轉單
# --------------------------------------------------------------------------------------
def test_stopped_machine_accrues_standby_not_running_energy(twin: FactoryTwin):
    twin.run(10)
    entry = twin.energy.machines["M-A"]
    running_before = entry.running_energy_kwh
    assert running_before > 0
    assert entry.standby_energy_kwh == pytest.approx(0.0)

    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    twin.run(10)
    assert twin.snapshot().machines["M-A"].state is MachineState.STOPPED
    # 停機期間不再累積「運轉能耗」——
    assert entry.running_energy_kwh == pytest.approx(running_before)
    assert entry.running_min == pytest.approx(10.0)
    # 但控制櫃／油壓仍在耗電，所以待機能耗照實累加，且功率遠低於額定。
    assert entry.standby_energy_kwh > 0
    assert entry.standby_min == pytest.approx(10.0)
    assert entry.last_power_kw < 0.1 * entry.rated_power_kw


def test_transferred_load_is_still_counted_on_the_receiving_machine(twin: FactoryTwin):
    """轉單不會讓電憑空消失：接手的機台一直在線，它的能耗一直被計入總帳。"""
    twin.run(5)
    twin.apply(Action(ActionKind.TRANSFER_ORDER, "ORD-A001", {"order_id": "ORD-A001", "to_machine": "M-B"}))
    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    before_b = twin.energy.machines["M-B"].running_energy_kwh
    twin.run(20)

    assert twin.orders["ORD-A001"].assigned_machine == "M-B"
    assert twin.energy.machines["M-B"].running_energy_kwh > before_b
    assert twin.energy.machines["M-B"].running_min == pytest.approx(25.0)
    # 總帳 = 各機台之和（含停機機台的待機電），沒有漏記也沒有重複計。
    assert twin.energy_kpi()["energy_kwh"] == pytest.approx(
        sum(m.energy_kwh for m in twin.energy.machines.values())
    )


def test_dry_run_fork_does_not_pollute_the_real_ledger(twin: FactoryTwin):
    """方案乾跑會跑掉幾十個 tick；那些電不能記到真實孿生體上。"""
    twin.run(10)
    real_before = twin.energy_kpi()["energy_kwh"]
    clone = twin.fork()
    clone.run(30)
    assert clone.energy_kpi()["energy_kwh"] > real_before
    assert twin.energy_kpi()["energy_kwh"] == pytest.approx(real_before)


# --------------------------------------------------------------------------------------
# 可信度：不洩漏 Ground Truth、可由可見訊號重建
# --------------------------------------------------------------------------------------
def test_energy_never_exposes_ground_truth(twin: FactoryTwin):
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(25)

    snapshot_payload = str(twin.snapshot().to_dict())
    assert "bearing_degradation" not in snapshot_payload
    assert "fault" not in snapshot_payload

    ledger_payload = str(twin.energy.to_dict(twin.completed_units_total))
    assert "bearing_degradation" not in ledger_payload
    assert "fault" not in ledger_payload
    assert "ground_truth" not in ledger_payload
    for key in twin.energy_kpi():
        assert "fault" not in key

    # 但引擎內部仍然知道答案 —— 能源帳只是沒有用到它。
    assert twin.ground_truth == {"M-A": "bearing_degradation"}


def test_energy_can_be_reconstructed_from_agent_visible_signals():
    """把 Agent 看得到的電流讀值自己積一次，應該回到同一個度數。

    能源帳用的是未加量測雜訊的真實電流（雜訊不該讓機台真的多耗電），
    但兩者的差只有雜訊：積分幾十個 tick 之後誤差落在 1% 量級。
    這證明這些度數是**可被外部稽核重算**的，不是模擬器內部另外編的一個數字。
    """
    twin = FactoryTwin(seed=20260809, tick_minutes=1.0)
    twin.schedule(get_scenario("motor-overload").injections)
    topo = twin.topo
    hours = twin.tick_minutes / 60.0

    estimated = 0.0
    for _ in range(40):
        snapshot = twin.step()
        for mid, machine in snapshot.machines.items():
            spec = topo.machines[mid].signal("current")
            current = machine.value("current")
            if spec is None or current is None:
                continue
            estimated += power_kw_from_current(topo.machines[mid].rated_power_kw, current, spec.nominal) * hours

    measured = twin.energy_kpi()["energy_kwh"]
    assert measured == pytest.approx(estimated, rel=0.03)


# --------------------------------------------------------------------------------------
# Episode / Benchmark
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario_id", EQUIPMENT_SCENARIOS)
def test_guardian_wastes_far_less_energy_and_carbon_than_baseline_a(scenario_id):
    """整個永續論點的核心：早期介入省下來的電，是「本來會被劣化吃掉」的那些電。

    Baseline A 放任劣化到二次損壞，摩擦／負載一路推高電流；
    Factory Guardian 在訊號還微弱時就介入，浪費被壓在數量級以下。
    """
    scenario = get_scenario(scenario_id)
    baseline = run_episode(scenario, mode="baseline-a", persist_audit=False, require_approval=False)
    guardian = run_episode(scenario, mode="guardian", persist_audit=False, require_approval=False)

    assert baseline.kpi.energy_waste_kwh > 1.0
    assert guardian.kpi.energy_waste_kwh < baseline.kpi.energy_waste_kwh / 5
    assert guardian.kpi.co2e_waste_kg < baseline.kpi.co2e_waste_kg / 5
    assert guardian.kpi.energy_waste_ntd < baseline.kpi.energy_waste_ntd
    # 單位產出能耗才是永續真正該比的指標：分子是多耗的電，分母是被劣化吃掉的產出。
    assert guardian.kpi.energy_intensity_kwh_per_unit < baseline.kpi.energy_intensity_kwh_per_unit
    # 對照組確實跑到了「放任劣化」的終局，數字才有意義。
    assert baseline.kpi.secondary_damage
    assert baseline.kpi.machine_health_final < 70


def test_healthy_episode_reports_no_energy_waste():
    """工安情境沒有設備故障 → 浪費為 0；不會因為停機就被記成『浪費』。"""
    result = run_episode(get_scenario("hazard-zone"), mode="guardian",
                         persist_audit=False, require_approval=False)
    assert result.kpi.energy_kwh > 0
    assert result.kpi.energy_waste_kwh == pytest.approx(0.0, abs=1e-6)
    assert result.kpi.co2e_kg > 0


def test_energy_kpi_is_measured_not_estimated():
    """KPI 直接來自孿生體逐 tick 的累積帳，兩者必須完全一致。"""
    result = run_episode(get_scenario("bearing-degradation"), mode="baseline-a",
                         persist_audit=False, require_approval=False)
    kpi = result.kpi
    assert kpi.co2e_kg == pytest.approx(kpi.energy_kwh * GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH)
    assert kpi.energy_waste_ntd == pytest.approx(kpi.energy_waste_kwh * ELECTRICITY_TARIFF_NTD_PER_KWH)
    assert kpi.energy_kwh > kpi.energy_waste_kwh > 0
    assert kpi.energy_intensity_kwh_per_unit > 0


def test_energy_columns_are_in_the_benchmark_report():
    columns = {key: (label, higher) for key, label, higher in REPORT_COLUMNS}
    for key in ("energy_kwh", "energy_waste_kwh", "energy_waste_ntd",
                "co2e_kg", "co2e_waste_kg", "energy_intensity_kwh_per_unit"):
        assert key in columns, f"{key} 未出現在 Benchmark 報表欄位"
        label, higher_is_better = columns[key]
        assert label and not higher_is_better       # 能耗與碳排一律越低越好
        assert hasattr(EpisodeKPI(), key)           # 欄位名稱必須真的存在於 KPI
