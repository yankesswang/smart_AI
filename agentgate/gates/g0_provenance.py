"""G0 來源信任(Provenance)— 差異化核心之一(規格 §4.2)。

Agent 收到的「指令」來自多個通道,多數系統把它們拉平成同一個 prompt,
攻擊正是從這個縫隙進來。G0 的規則是結構性的,不依賴偵測「這段文字看起來像攻擊」:
攻擊者可以改寫措辭,但改不掉指令是從一份上傳文件進來的這個事實。

兩條規則:

**G0-R1 權限升級阻斷** — 動作的風險等級,不得高於其指令來源鏈中
最低信任通道所能授權的等級。

**G0-R2 確認提升(confirmation lifting)** — 不可信來源(tool_output /
user_unverified)提出的指令,若鏈中存在一個**獨立的**已驗證(user_verified)或
系統(system)節點,而該節點帶著 ``confirms = <被確認來源的 source_ref>``
與 ``confirmed_action_hash = <這次動作的指紋>``,則該不可信來源的授權上限
提升到確認者的上限。沒有確認的不可信來源仍然是 ``low``。

為什麼需要 G0-R2:純粹的「最弱環節硬上限」會把真實客服最常見的流程整組擋掉 ——
用戶上傳帳單截圖、Agent 讀了截圖、用戶說「就退這 880 元」。截圖是 tool_output,
硬上限把整鏈壓到 low,退費(medium)必被攔。真正該擋的不是「來源鏈裡有附件」,
而是「不可信來源提出了沒有人確認過的動作」。

為什麼「用戶轉述 PDF 內容」不算確認:

* 轉述只是**多一個指令來源節點**。它沒有 ``confirms``、也沒有指紋 ——
  因為 runtime 從來沒有向用戶展示過一個具體動作、也沒有收到針對它的同意事件。
  最弱環節規則照常適用,tool_output 仍是 ``low``。
* 確認則是一個**獨立發生的事件**:harness 把「issue_refund / account_id=X /
  amount=880」這組具體參數呈現給已驗證用戶,用戶同意,harness 當場算指紋。
  指紋涵蓋**全部參數** —— 用戶對「退到本人帳戶」說好,Agent 卻送出
  ``refund_to=ACC-9999``,指紋立刻對不上,提升不成立。
* 因此 Demo 的 a-inj 情境(PDF 夾帶「匯出全部客戶」)仍在 G0 被攔:
  用戶確實上傳了 PDF,但**從來沒有人確認過「匯出 500 筆客戶個資」這個動作**,
  鏈中沒有指紋對得上的確認節點。

獨立性怎麼定義(這是提升會不會變成後門的關鍵):

1. 確認者本身必須是 ``user_verified`` / ``system`` 通道 —— 一份文件不能確認另一份文件。
2. 確認者的 ``source_ref`` 必須不同於被確認者 —— 不得自我確認。
3. 確認者不會被提升 —— 提升不遞移,不可信節點串不出一條信任鏈。
4. ``memory`` 不可被提升 —— 記憶是 Agent 自己寫的,讓它被確認等於自己給自己背書。
"""

from __future__ import annotations

from ..ontology import (
    CHANNEL_MAX_RISK,
    CONFIRMER_CHANNELS,
    LIFTABLE_CHANNELS,
    Evidence,
    Provenance,
    RISK_ORDER,
    TrustVerdict,
)


def _match_confirmation(
    node: Provenance, chain: list[Provenance], fingerprint: str
) -> tuple[Provenance | None, list[Provenance]]:
    """找出可以提升 ``node`` 的確認節點。

    回傳 (有效確認者 | None, 指紋對不上的確認者列表)。
    後者是攻擊訊號:確認了 A 動作,卻拿去授權 B 動作。
    """
    stale: list[Provenance] = []
    for cand in chain:
        if cand.channel not in CONFIRMER_CHANNELS:
            continue
        if not cand.confirmed_action_hash:
            continue
        if cand.confirms != node.source_ref:
            continue
        if cand.source_ref == node.source_ref:
            continue  # 不得自我確認
        if fingerprint and cand.confirmed_action_hash == fingerprint:
            return cand, stale
        stale.append(cand)
    return None, stale


def evaluate_trust(
    chain: list[Provenance], action_fingerprint: str = ""
) -> TrustVerdict:
    """評估指令來源鏈,找出最弱環節與其風險授權上限。

    ``action_fingerprint`` 由 runtime 算出(``ActionRequest.fingerprint()``)。
    留空 = 沒有指紋可比對 → 一律不提升(fail-closed):寧可誤攔也不放行。

    空鏈視同完全不可信(cap = low):沒有來源紀錄的指令不能被授權做任何高風險動作。
    """
    if not chain:
        return TrustVerdict(min_trust="untrusted", risk_cap="low", weakest_link=None,
                            chain=[], effective_caps=[], lifts=[], stale_confirmations=[])

    effective_caps: list[str] = []
    lifts: list[dict[str, object]] = []
    stale: list[dict[str, object]] = []
    for node in chain:
        cap = CHANNEL_MAX_RISK[node.channel]
        if node.channel in LIFTABLE_CHANNELS:
            confirmer, stale_hits = _match_confirmation(node, chain, action_fingerprint)
            for bad in stale_hits:
                stale.append({
                    "confirmer_ref": bad.source_ref,
                    "confirms": bad.confirms,
                    "confirmed_action_hash": bad.confirmed_action_hash,
                    "this_action_hash": action_fingerprint,
                })
            if confirmer is not None:
                lifted_cap = CHANNEL_MAX_RISK[confirmer.channel]
                if RISK_ORDER[lifted_cap] > RISK_ORDER[cap]:
                    lifts.append({
                        "source_ref": node.source_ref,
                        "channel": node.channel.value,
                        "from_cap": cap,
                        "to_cap": lifted_cap,
                        "confirmer_ref": confirmer.source_ref,
                        "confirmer_channel": confirmer.channel.value,
                        "action_hash": action_fingerprint,
                    })
                    cap = lifted_cap
        effective_caps.append(cap)

    weakest_index = min(range(len(chain)), key=lambda i: RISK_ORDER[effective_caps[i]])
    weakest = chain[weakest_index]
    return TrustVerdict(
        min_trust=weakest.trust,
        risk_cap=effective_caps[weakest_index],
        weakest_link=weakest,
        chain=list(chain),
        effective_caps=effective_caps,
        lifts=lifts,
        stale_confirmations=stale,
    )


def trust_evidence(verdict: TrustVerdict) -> list[Evidence]:
    """把信任裁決轉成證據,供核准介面與稽核鏈引用。"""
    items: list[Evidence] = []
    caps = verdict.effective_caps or [CHANNEL_MAX_RISK[p.channel] for p in verdict.chain]
    for prov, cap in zip(verdict.chain, caps):
        base = CHANNEL_MAX_RISK[prov.channel]
        suffix = f"(經確認提升為 {cap})" if cap != base else ""
        items.append(
            Evidence(
                source="provenance",
                reference=prov.source_ref,
                statement=(
                    f"指令來源 {prov.channel.value}(信任等級 {prov.trust},"
                    f"可授權上限 {base}){suffix}:{prov.source_ref}"
                ),
            )
        )
    for lift in verdict.lifts:
        items.append(
            Evidence(
                source="provenance",
                reference="G0-R2",
                statement=(
                    f"確認提升:{lift['source_ref']} 的授權上限由 {lift['from_cap']} "
                    f"提升為 {lift['to_cap']} —— 已驗證節點 {lift['confirmer_ref']} "
                    f"確認了本次動作(指紋 {str(lift['action_hash'])[:12]}…)。"
                ),
            )
        )
    for bad in verdict.stale_confirmations:
        items.append(
            Evidence(
                source="provenance",
                reference="G0-R2",
                statement=(
                    f"確認不成立:{bad['confirmer_ref']} 確認的動作指紋 "
                    f"{str(bad['confirmed_action_hash'])[:12]}… 與本次動作 "
                    f"{str(bad['this_action_hash'])[:12]}… 不符,不予提升。"
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
