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
        "颱風外圍環流造成院區對外主幹光纖於人孔段受損中斷；同時周邊避難收容"
        "導致 gNB-01 用戶暴增，5G 回程可用頻寬僅剩約四成且延遲上升。"
    ),
    faults=[
        Fault("w-fiber", LinkState.DOWN, description="主幹光纖實體中斷"),
        Fault(
            "w-5g", LinkState.DEGRADED, extra_latency_ms=25.0, extra_loss_pct=0.25,
            capacity_factor=0.40, description="5G 基地台壅塞：容量剩 40%、延遲 +25ms、丟包 +0.25%",
        ),
    ],
)

EARTHQUAKE_DUAL_LOSS = Scenario(
    id="earthquake-dual-loss",
    name="地震致固網與 5G 雙路中斷（衛星為唯一生路）",
    narrative=(
        "強震造成固網管道斷裂，鄰近 5G 基地台停電退出服務。院區僅餘海地星空"
        "衛星鏈路，頻寬 120 Mbps、延遲 48ms，必須嚴格分配給生命關鍵業務。"
    ),
    faults=[
        Fault("w-fiber", LinkState.DOWN, description="固網管道斷裂"),
        Fault("w-5g", LinkState.DOWN, description="基地台停電退服"),
        Fault(
            "w-sat", LinkState.DEGRADED, extra_latency_ms=6.0, extra_loss_pct=0.15,
            capacity_factor=0.9, description="降雨衰減使衛星鏈路輕微劣化",
        ),
    ],
)

BACKBONE_BROWNOUT = Scenario(
    id="backbone-brownout",
    name="骨幹壅塞致 SLA 邊緣劣化（無斷線的隱性事故）",
    narrative=(
        "區域骨幹異常導致固網 POP 至醫療雲段延遲上升、輕微丟包。"
        "沒有任何鏈路 down，傳統告警不會觸發，但遠距診療品質已跌破 SLO。"
    ),
    faults=[
        Fault(
            "b-fiber", LinkState.DEGRADED, extra_latency_ms=95.0, extra_loss_pct=1.4,
            capacity_factor=0.75, description="骨幹擁塞：延遲 +95ms、丟包 1.4%",
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
