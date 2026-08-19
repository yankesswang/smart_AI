"""G0 來源信任(Provenance)— 差異化核心之一(規格 §4.2)。

Agent 收到的「指令」來自多個通道,多數系統把它們拉平成同一個 prompt,
攻擊正是從這個縫隙進來。G0 的規則是結構性的,不依賴偵測「這段文字看起來像攻擊」:
攻擊者可以改寫措辭,但改不掉指令是從一份上傳文件進來的這個事實。

G0-R1 權限升級阻斷:動作的風險等級,不得高於其指令來源鏈中
最低信任通道所能授權的等級。
"""

from __future__ import annotations

from ..ontology import (
    CHANNEL_MAX_RISK,
    Evidence,
    Provenance,
    RISK_ORDER,
    TrustVerdict,
)


def evaluate_trust(chain: list[Provenance]) -> TrustVerdict:
    """評估指令來源鏈,找出最弱環節與其風險授權上限。

    空鏈視同完全不可信(cap = low):沒有來源紀錄的指令不能被授權做任何高風險動作。
    """
    if not chain:
        return TrustVerdict(min_trust="untrusted", risk_cap="low", weakest_link=None, chain=[])
    weakest = min(chain, key=lambda p: RISK_ORDER[CHANNEL_MAX_RISK[p.channel]])
    return TrustVerdict(
        min_trust=weakest.trust,
        risk_cap=CHANNEL_MAX_RISK[weakest.channel],
        weakest_link=weakest,
        chain=list(chain),
    )


def trust_evidence(verdict: TrustVerdict) -> list[Evidence]:
    """把信任裁決轉成證據,供核准介面與稽核鏈引用。"""
    items: list[Evidence] = []
    for prov in verdict.chain:
        items.append(
            Evidence(
                source="provenance",
                reference=prov.source_ref,
                statement=(
                    f"指令來源 {prov.channel.value}(信任等級 {prov.trust},"
                    f"可授權上限 {CHANNEL_MAX_RISK[prov.channel]}):{prov.source_ref}"
                ),
            )
        )
    if verdict.weakest_link is not None:
        items.append(
            Evidence(
                source="provenance",
                reference="G0-R1",
                statement=(
                    f"來源鏈最弱環節為 {verdict.weakest_link.channel.value}"
                    f"({verdict.weakest_link.source_ref}),整鏈風險授權上限 = {verdict.risk_cap}。"
                ),
            )
        )
    return items


__all__ = ["evaluate_trust", "trust_evidence"]
