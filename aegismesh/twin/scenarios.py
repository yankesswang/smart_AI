"""三個災害情境（MVP 範圍上限）。

每個 Fault 都能翻譯成真實的 tc/netem 或 ip link 指令 —— 這是把本機模擬
搬到 containerlab / Mininet 時的落地介面，不是為了 Demo 而編造的數字。
"""

from __future__ import annotations

from ..domain import Fault, LinkState, Scenario

TYPHOON_FIBER_CUT = Scenario(
    id="typhoon-fiber-cut",
    name="颱風致光纖中斷 ＋ 5G 基地台壅塞",
    narrative=(
        "颱風外圍環流吹垮人孔內的光纖，醫院對外的主線直接斷掉；"
        "同時附近設了避難收容所，湧入的人潮把 5G 基地台擠爆，"
        "備援的 5G 只剩約四成速度，反應時間也變慢。"
    ),
    duration_hours=24.0,
    # 避難收容湧入 → 訪客 Wi-Fi 需求暴增；傷患湧入 → 急診生命徵象串流變多
    demand={"svc-guest": 2.5, "svc-ed-vitals": 1.6, "svc-teleconsult": 1.3},
    faults=[
        Fault("w-fiber", LinkState.DOWN, description="對外主線光纖被扯斷，完全不通"),
        Fault(
            "w-5g", LinkState.DEGRADED, extra_latency_ms=25.0, extra_loss_pct=0.25,
            capacity_factor=0.40,
            description="5G 基地台塞車：速度只剩四成、反應時間多 25 毫秒、開始掉資料",
        ),
    ],
)

EARTHQUAKE_DUAL_LOSS = Scenario(
    id="earthquake-dual-loss",
    name="地震致固網與 5G 雙路中斷（衛星為唯一生路）",
    narrative=(
        "強震震斷固網管道，附近的 5G 基地台也因為停電停止服務。"
        "醫院只剩衛星這條路，而衛星只有 120 Mbps、反應時間 48 毫秒 —— "
        "頻寬必須嚴格留給救命服務，其他的只能先停。"
    ),
    duration_hours=36.0,
    demand={"svc-ed-vitals": 2.0, "svc-icu-iot": 1.4, "svc-guest": 1.8},
    faults=[
        Fault("w-fiber", LinkState.DOWN, description="固網管道被震斷"),
        Fault("w-5g", LinkState.DOWN, description="5G 基地台停電，完全停止服務"),
        Fault(
            "w-sat", LinkState.DEGRADED, extra_latency_ms=6.0, extra_loss_pct=0.15,
            capacity_factor=0.9, description="下雨使衛星訊號稍微變差",
        ),
    ],
)

BACKBONE_BROWNOUT = Scenario(
    id="backbone-brownout",
    name="電信骨幹塞車：沒有斷線，但品質悄悄跌破標準",
    narrative=(
        "電信機房到醫療雲之間的骨幹線路異常，反應時間拉長、開始掉一點資料。"
        "沒有任何一條線斷掉，傳統的告警系統不會響 —— "
        "但遠距診療的品質其實已經跌破當初承諾的標準。"
    ),
    duration_hours=6.0,
    demand={"svc-teleconsult": 1.2},
    faults=[
        Fault(
            "b-fiber", LinkState.DEGRADED, extra_latency_ms=95.0, extra_loss_pct=1.4,
            capacity_factor=0.75,
            description="骨幹塞車：反應時間多 95 毫秒、資料遺失 1.4%",
        ),
    ],
)

SCENARIOS: dict[str, Scenario] = {
    s.id: s for s in (TYPHOON_FIBER_CUT, EARTHQUAKE_DUAL_LOSS, BACKBONE_BROWNOUT)
}


def get_scenario(scenario_id: str) -> Scenario:
    if scenario_id not in SCENARIOS:
        raise KeyError(f"未知情境 {scenario_id}；可用：{', '.join(SCENARIOS)}")
    return SCENARIOS[scenario_id]
