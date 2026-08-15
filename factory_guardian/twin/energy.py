"""能源與碳排帳（Energy & Carbon Accounting）。

這個模組**不發明新的能源模型**。設備劣化 → 摩擦／負載上升 → 電流上升，
這件事在 twin/faults.py 的故障模型裡已經是自洽的物理（軸承劣化 current +2.2A、
馬達過載 current +6.0A）。多耗的電本來就隱含在既有模擬裡，這裡只是把它**積分出來**。

三個推導步驟，每一步都能被追問：

1. **實際功率**  ``P(t) = rated_power_kw × I_clean(t) / I_nominal``

   定電壓、功率因數近似固定時電力與電流成正比，所以「電流相對額定值的比例」
   就是「功率相對額定功率的比例」。用的是 ``clean_signals``（未加量測雜訊的真實物理量）
   —— 與 ``effective_rate()`` 用 clean rpm 的理由一樣：量測雜訊不該讓機台真的多耗電。
   Agent 看到的是帶雜訊的讀值，兩者的差只有雜訊，長期積分後可互相驗證
   （見 ``tests/test_energy.py::test_energy_can_be_reconstructed_from_agent_visible_signals``）。

2. **名目功率（健康影子基線）**  同一台機器、同一個運轉狀態、但**沒有故障**時該用的電流。

   基線來自訊號規格（設備銘牌／手冊）與當下的控制狀態（RUNNING / DERATED / STOPPED），
   **不是**故障標籤 —— Ground Truth 沒有參與這條公式。
   基線也走與真實訊號完全相同的一階遲滯，所以降速／停機造成的暫態不會被誤記成浪費，
   只有「同一個狀態下電流比健康時高」才算。

3. **能源浪費**  ``E_waste = ∫ max(0, P(t) − P_nominal(t)) dt``

   只累加正值：機台因為降速而少用的電是產能的代價，不是「省下來的浪費」，
   把它拿去和劣化的多耗抵銷會讓數字失去意義。

碳排與電費是最後一步的線性換算，係數集中在本檔案最上方，全部標示為**假設參數**。

> **SYNTHETIC DEMO DATA** — 額定功率、電價與排碳係數皆為競賽用假設值，
> 真實導入時應由電表（Modbus/OPC-UA 電力量測模組）與該廠實際電價／係數取代，
> 本模組的介面（``EnergyLedger.accumulate``）不需改動。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------------------
# 假設參數（全部可調，且都標明出處與年度）
# --------------------------------------------------------------------------------------
# 台灣電力排碳係數（kgCO2e / kWh）。出處：經濟部能源署公告之「電力排碳係數」年度值。
# 這是**全國電網平均**係數，用於 Scope 2 外購電力的碳排換算。
GRID_EMISSION_FACTOR_BY_YEAR: dict[int, float] = {
    2021: 0.509,
    2022: 0.495,
    2023: 0.494,
}
# 本 Demo 採用的年度。真實導入時應更新為最新公告值（能源署每年公告一次），
# 或改用該廠實際的再生能源憑證／綠電比例後的自有係數。
GRID_EMISSION_FACTOR_YEAR: int = 2023
GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH: float = GRID_EMISSION_FACTOR_BY_YEAR[GRID_EMISSION_FACTOR_YEAR]
GRID_EMISSION_FACTOR_SOURCE: str = (
    f"經濟部能源署公告 {GRID_EMISSION_FACTOR_YEAR} 年度電力排碳係數 "
    f"{GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH:g} kgCO2e/kWh（假設參數，導入時依最新公告更新）"
)

# 工業用電電價（NTD / kWh）。假設值，量級參考台電高壓產業用電平均電價；
# 真實導入時應改用該廠實際的時間電價（尖峰／離峰）與契約容量費。
ELECTRICITY_TARIFF_NTD_PER_KWH: float = 3.5
ELECTRICITY_TARIFF_SOURCE: str = (
    f"台電高壓產業用電平均電價量級 {ELECTRICITY_TARIFF_NTD_PER_KWH:g} NTD/kWh（假設參數，未含時間電價與契約容量費）"
)


def power_kw_from_current(rated_power_kw: float, current_a: float, nominal_current_a: float) -> float:
    """由電流推導功率：``P = P_rated × I / I_nominal``（定電壓、功率因數近似固定）。

    ``I_nominal`` 是訊號規格裡的額定電流，也就是「額定功率對應的那個電流」，
    所以健康機台滿載時 P 會剛好等於 rated_power_kw。
    """
    if nominal_current_a <= 0:
        return 0.0
    return max(0.0, rated_power_kw * current_a / nominal_current_a)


@dataclass
class MachineEnergy:
    """單一機台的能源帳。所有數字都是逐 tick 累加出來的，沒有事後估算。"""

    machine_id: str
    rated_power_kw: float
    # 實際消耗
    energy_kwh: float = 0.0
    running_energy_kwh: float = 0.0      # 在線（RUNNING / DERATED / IDLE）期間的能耗
    standby_energy_kwh: float = 0.0      # 停機／維修期間的待機能耗
    # 健康影子基線（同狀態、無故障時該用的電）
    nominal_energy_kwh: float = 0.0
    energy_waste_kwh: float = 0.0
    # 時間分布
    running_min: float = 0.0
    standby_min: float = 0.0
    # 內部狀態：健康影子的電流（與真實訊號同樣走一階遲滯）
    baseline_current_a: float | None = None
    # 最近一次的瞬時值（給 Dashboard／稽核用）
    last_power_kw: float = 0.0
    last_nominal_power_kw: float = 0.0

    def accumulate(
        self,
        *,
        actual_current_a: float,
        healthy_target_current_a: float,
        nominal_current_a: float,
        minutes: float,
        online: bool,
        alpha: float,
    ) -> None:
        hours = minutes / 60.0
        if hours <= 0:
            return
        # 健康影子基線：同一個一階遲滯，只是永遠沒有故障偏移。
        if self.baseline_current_a is None:
            self.baseline_current_a = healthy_target_current_a
        else:
            self.baseline_current_a += (healthy_target_current_a - self.baseline_current_a) * alpha
        self.baseline_current_a = max(0.0, self.baseline_current_a)

        actual_kw = power_kw_from_current(self.rated_power_kw, actual_current_a, nominal_current_a)
        nominal_kw = power_kw_from_current(self.rated_power_kw, self.baseline_current_a, nominal_current_a)
        self.last_power_kw = actual_kw
        self.last_nominal_power_kw = nominal_kw

        used = actual_kw * hours
        self.energy_kwh += used
        if online:
            self.running_energy_kwh += used
            self.running_min += minutes
        else:
            # 停機／維修：感測器電流掉到待機值（控制櫃、油壓、排屑機仍在耗電），
            # 所以待機能耗照實累加，但**不計入運轉能耗**。
            self.standby_energy_kwh += used
            self.standby_min += minutes
        self.nominal_energy_kwh += nominal_kw * hours
        self.energy_waste_kwh += max(0.0, actual_kw - nominal_kw) * hours

    @property
    def co2e_kg(self) -> float:
        return self.energy_kwh * GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH

    @property
    def co2e_waste_kg(self) -> float:
        return self.energy_waste_kwh * GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH

    @property
    def energy_waste_ntd(self) -> float:
        return self.energy_waste_kwh * ELECTRICITY_TARIFF_NTD_PER_KWH

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "rated_power_kw": round(self.rated_power_kw, 2),
            "energy_kwh": round(self.energy_kwh, 3),
            "running_energy_kwh": round(self.running_energy_kwh, 3),
            "standby_energy_kwh": round(self.standby_energy_kwh, 3),
            "nominal_energy_kwh": round(self.nominal_energy_kwh, 3),
            "energy_waste_kwh": round(self.energy_waste_kwh, 3),
            "energy_waste_ntd": round(self.energy_waste_ntd, 1),
            "co2e_kg": round(self.co2e_kg, 3),
            "co2e_waste_kg": round(self.co2e_waste_kg, 3),
            "running_min": round(self.running_min, 1),
            "standby_min": round(self.standby_min, 1),
            "power_kw": round(self.last_power_kw, 2),
            "nominal_power_kw": round(self.last_nominal_power_kw, 2),
        }


@dataclass
class EnergyLedger:
    """整廠的能源帳：逐機台累加，再彙總。

    這份帳只讀「感測器電流 + 機台控制狀態 + 設備銘牌額定功率」，
    完全沒有讀取 ``_MachineRuntime.fault`` / ``fault_progress``，
    所以它可以安全地出現在 Agent 與 Dashboard 看得到的地方。
    """

    machines: dict[str, MachineEnergy] = field(default_factory=dict)

    @classmethod
    def for_machines(cls, rated_power_by_machine: dict[str, float]) -> "EnergyLedger":
        return cls(machines={mid: MachineEnergy(mid, kw) for mid, kw in rated_power_by_machine.items()})

    def accumulate(
        self,
        machine_id: str,
        *,
        actual_current_a: float,
        healthy_target_current_a: float,
        nominal_current_a: float,
        minutes: float,
        online: bool,
        alpha: float,
    ) -> None:
        entry = self.machines.get(machine_id)
        if entry is None or entry.rated_power_kw <= 0:
            return
        entry.accumulate(
            actual_current_a=actual_current_a,
            healthy_target_current_a=healthy_target_current_a,
            nominal_current_a=nominal_current_a,
            minutes=minutes,
            online=online,
            alpha=alpha,
        )

    # -- 彙總 ---------------------------------------------------------------------------
    def _sum(self, attr: str) -> float:
        return sum(getattr(m, attr) for m in self.machines.values())

    @property
    def energy_kwh(self) -> float:
        return self._sum("energy_kwh")

    @property
    def running_energy_kwh(self) -> float:
        return self._sum("running_energy_kwh")

    @property
    def standby_energy_kwh(self) -> float:
        return self._sum("standby_energy_kwh")

    @property
    def nominal_energy_kwh(self) -> float:
        return self._sum("nominal_energy_kwh")

    @property
    def energy_waste_kwh(self) -> float:
        return self._sum("energy_waste_kwh")

    @property
    def energy_ntd(self) -> float:
        return self.energy_kwh * ELECTRICITY_TARIFF_NTD_PER_KWH

    @property
    def energy_waste_ntd(self) -> float:
        return self.energy_waste_kwh * ELECTRICITY_TARIFF_NTD_PER_KWH

    @property
    def co2e_kg(self) -> float:
        return self.energy_kwh * GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH

    @property
    def co2e_waste_kg(self) -> float:
        return self.energy_waste_kwh * GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH

    def intensity_kwh_per_unit(self, produced_units: float) -> float:
        """單位產出能耗（kWh/件）—— 永續性真正該比的指標。

        只看總能耗會被「停機的機台比較省電」誤導：停下來當然耗電少，但也沒有產出。
        單位產出能耗同時吃到分子（多耗的電）與分母（因劣化而降速、重工造成的產出損失），
        所以它是唯一能同時反映「能源浪費」與「產能損失」的單一數字。
        """
        if produced_units <= 0:
            return 0.0
        return self.energy_kwh / produced_units

    def summary(self, produced_units: float = 0.0) -> dict[str, float]:
        return {
            "energy_kwh": self.energy_kwh,
            "energy_running_kwh": self.running_energy_kwh,
            "energy_standby_kwh": self.standby_energy_kwh,
            "energy_nominal_kwh": self.nominal_energy_kwh,
            "energy_waste_kwh": self.energy_waste_kwh,
            "energy_ntd": self.energy_ntd,
            "energy_waste_ntd": self.energy_waste_ntd,
            "co2e_kg": self.co2e_kg,
            "co2e_waste_kg": self.co2e_waste_kg,
            "energy_intensity_kwh_per_unit": self.intensity_kwh_per_unit(produced_units),
        }

    def to_dict(self, produced_units: float = 0.0) -> dict[str, Any]:
        return {
            **{k: round(v, 3) for k, v in self.summary(produced_units).items()},
            "machines": {mid: m.to_dict() for mid, m in self.machines.items()},
            "assumptions": {
                "grid_emission_factor_kg_co2e_per_kwh": GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH,
                "grid_emission_factor_source": GRID_EMISSION_FACTOR_SOURCE,
                "electricity_tariff_ntd_per_kwh": ELECTRICITY_TARIFF_NTD_PER_KWH,
                "electricity_tariff_source": ELECTRICITY_TARIFF_SOURCE,
                "model": "P = rated_power_kw × I / I_nominal；浪費 = ∫max(0, P − P_healthy_baseline)dt",
                "disclaimer": "SYNTHETIC DEMO DATA：額定功率、電價與排碳係數皆為競賽用假設值。",
            },
        }


__all__ = [
    "EnergyLedger",
    "MachineEnergy",
    "power_kw_from_current",
    "GRID_EMISSION_FACTOR_BY_YEAR",
    "GRID_EMISSION_FACTOR_YEAR",
    "GRID_EMISSION_FACTOR_KG_CO2E_PER_KWH",
    "GRID_EMISSION_FACTOR_SOURCE",
    "ELECTRICITY_TARIFF_NTD_PER_KWH",
    "ELECTRICITY_TARIFF_SOURCE",
]
