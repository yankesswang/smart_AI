"""四分鐘閉環 Demo 劇本（研究文件 §5.1）—— 寫成資料，不是 print 序列。

文件 §5.1 給了八個時間段，每段都有「畫面」與「要證明的事情」兩欄。
這個模組把那張表原封不動搬進程式碼，再補上兩件現場真的需要的東西：

* ``cue``：台上那句話怎麼講（runbook 逐段口白的來源）。
* ``metrics`` / ``details``：這一段結束時要念出來的**當下真實數字**是哪幾個欄位。

刻意做成資料的理由有三個：

1. 節奏可調 —— 場地說「你們只有 3 分鐘」時，改的是 ``end_s``，不是八段 print。
2. 可被測試 —— ``tests/test_stage.py`` 直接拿這張表對照文件 §5.1 的八段。
3. 數字不會變成預錄字串 —— 劇本只寫「要念哪個欄位」，值一律由 Director
   從當次執行的結果取出。劇本裡沒有任何一個具體數值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 劇本總長（秒）。文件 §5.1 的四分鐘。
TOTAL_SECONDS = 240.0


def mmss(seconds: float) -> str:
    """把秒數印成劇本上的 ``M:SS``。"""
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


@dataclass(frozen=True)
class Metric:
    """一段劇本結束時要念出來的一個數字。

    ``key`` 指向 Director 蒐集到的事實字典；劇本只認欄位名，不認值。
    """

    key: str
    label: str
    unit: str = ""
    digits: int | None = None

    def render(self, value: Any) -> str:
        """把值格式化成台上讀得出來的樣子（缺值一律顯示 ``—``，不編數字）。"""
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, (int, float)) and self.digits is not None:
            text = f"{float(value):,.{self.digits}f}"
        elif isinstance(value, float):
            text = f"{value:,.2f}".rstrip("0").rstrip(".")
        else:
            text = str(value)
        return f"{text}{self.unit}"


@dataclass(frozen=True)
class Act:
    """劇本的一段。"""

    act_id: str
    start_s: float
    end_s: float
    #: 文件 §5.1「畫面」欄，原文。
    screen: str
    #: 文件 §5.1「要證明的事情」欄，原文。
    doc_claim: str
    #: 這一段在這個實作裡真正證明了什麼（比文件更強的那句話）。
    proves: str
    #: 台上口白要點。
    cue: str
    #: 由 Orchestrator 的哪個 stage 觸發（``end`` 代表整場跑完之後）。
    trigger: str
    #: 要念出來的純量數字。
    metrics: tuple[Metric, ...] = ()
    #: 值為 ``list[str]`` 的事實鍵，逐行列出（證據、方案、驗證項…）。
    details: tuple[str, ...] = ()
    #: 缺任何一個就算「資料遺失」，對應文件 §5.3 的資料遺失率。
    required: tuple[str, ...] = ()

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def window(self) -> str:
        return f"{mmss(self.start_s)}–{mmss(self.end_s)}"

    def missing(self, facts: dict[str, Any]) -> list[str]:
        """這一段有哪些必要資料沒拿到。空值（None / 空清單）也算沒拿到。"""
        out = []
        for key in self.required:
            value = facts.get(key)
            if value is None or (isinstance(value, (list, tuple, dict, str)) and len(value) == 0):
                out.append(key)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "act_id": self.act_id,
            "window": self.window,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "duration_s": self.duration_s,
            "screen": self.screen,
            "doc_claim": self.doc_claim,
            "proves": self.proves,
            "cue": self.cue,
            "trigger": self.trigger,
            "metrics": [m.key for m in self.metrics],
            "details": list(self.details),
            "required": list(self.required),
        }


# --------------------------------------------------------------------------------------
# 劇本本體：文件 §5.1 的八段，順序與時間窗完全照抄
# --------------------------------------------------------------------------------------
SCRIPT: tuple[Act, ...] = (
    Act(
        act_id="A1",
        start_s=0.0,
        end_s=25.0,
        screen="正常狀態",
        doc_claim="顯示泵浦健康、感測數據與當班人員。",
        proves="畫面上的每個數字都是模擬器此刻的狀態，不是一張截圖。",
        cue="先讓評審看見『沒事的樣子』——三台機台、健康度、產線達成率、"
            "以及 CAM-01 看到的當班人員。這一段不要講技術，只要建立基準線。",
        trigger="tick",
        metrics=(
            Metric("normal.tick", "模擬時間", " min", 0),
            Metric("normal.factory_health", "工廠健康度", "", 1),
            Metric("normal.production_pct", "產線達成率", "%", 1),
            Metric("normal.person_count", "當班人員（CAM-01）", " 人", 0),
            Metric("normal.hazard_zone_clear", "危險區淨空", ""),
        ),
        details=("normal.machines", "normal.camera"),
        required=("normal.factory_health", "normal.production_pct", "normal.machines", "normal.camera"),
    ),
    Act(
        act_id="A2",
        start_s=25.0,
        end_s=55.0,
        screen="異常出現",
        doc_claim="聲音／振動改變，溫度或電流上升；攝影機看到洩漏或人員接近風險。",
        proves="異常是由感測訊號自己浮出來的：注入時點固定，觸發條件寫在事件裡，"
               "偵測延遲是量出來的。",
        cue="指著振動與溫度那兩條線講：故障在第幾分鐘注入、系統在第幾分鐘叫出來、"
            "中間差幾分鐘。觸發條件（門檻／趨勢／健康度）逐條念出來。",
        trigger="detect",
        metrics=(
            Metric("detect.event_id", "事件編號"),
            Metric("detect.injected_at_min", "故障注入於", " min", 0),
            Metric("detect.at_min", "系統偵測於", " min", 0),
            Metric("detect.latency_min", "偵測延遲", " min", 0),
            Metric("detect.severity", "嚴重度"),
            Metric("detect.detector", "偵測器"),
            Metric("detect.health", "機台健康度", "", 1),
        ),
        details=("detect.readings", "detect.triggers", "detect.camera"),
        required=("detect.event_id", "detect.readings", "detect.triggers", "detect.latency_min"),
    ),
    Act(
        act_id="A3",
        start_s=55.0,
        end_s=90.0,
        screen="多模態診斷",
        doc_claim="Agent 串接感測、影像、手冊與歷史工單，提出可能根因。",
        proves="根因由多個模態的證據合成，權重是固定的、可攤開的；LLM 只寫敘述，不決定排名。",
        cue="重點是「多模態」三個字要有東西撐：念出這次診斷用到了哪幾種證據來源，"
            "以及排名權重各佔多少。",
        trigger="diagnose",
        metrics=(
            Metric("diagnose.label", "研判根因"),
            Metric("diagnose.confidence", "信心度", "", 3),
            Metric("diagnose.candidate_count", "候選根因", " 個", 0),
            Metric("diagnose.modality_count", "證據模態", " 種", 0),
            Metric("diagnose.signal_strength", "訊號強度", "", 3),
            Metric("diagnose.llm_mode", "敘述來源"),
        ),
        details=("diagnose.modalities", "diagnose.weights", "diagnose.narrative"),
        required=("diagnose.label", "diagnose.confidence", "diagnose.modalities"),
    ),
    Act(
        act_id="A4",
        start_s=90.0,
        end_s=125.0,
        screen="證據與替代假設",
        doc_claim="顯示資料時間點、SOP 條款、相似案例、信心分數與其他可能原因。",
        proves="每條結論都指得回來源，落選的假設連同它們的分數一起攤開；"
               "信心不足時系統會選擇繼續觀察，而不是硬做。",
        cue="這一段是拉開信任的關鍵：先念證據（手冊條號、歷史案例編號、資料時間點），"
            "再念落選候選各自的分數，最後說明信心門檻與『續觀察』那幾分鐘。",
        trigger="diagnose",
        metrics=(
            Metric("evidence.count", "可引用證據", " 條", 0),
            Metric("evidence.confidence_threshold", "動設備前的信心門檻", "", 2),
            Metric("evidence.confirm_waited_min", "續觀察", " min", 0),
            Metric("evidence.data_at_min", "資料時間點", " min", 0),
        ),
        details=("evidence.items", "evidence.alternatives", "evidence.sop_refs"),
        required=("evidence.items", "evidence.alternatives"),
    ),
    Act(
        act_id="A5",
        start_s=125.0,
        end_s=155.0,
        screen="處置建議",
        doc_claim="建議降載、隔離、檢查軸承或密封件；高風險動作等待主管確認。",
        proves="方案的數字是模擬器乾跑出來的，不是 LLM 猜的；"
               "Safety 是硬限制，會把不安全的方案直接刪掉；排名由規則引擎算。",
        cue="先講受影響的訂單與交期風險，再講被 Safety 擋掉的方案"
            "（念出規則編號與理由），最後才講推薦方案與它的預測值。",
        trigger="rank",
        metrics=(
            Metric("plan.affected_order_count", "受影響訂單", " 張", 0),
            Metric("plan.total_delay_min", "不處置的總延遲", " min", 0),
            Metric("plan.count", "候選方案", " 個", 0),
            Metric("plan.blocked_count", "被 Safety 阻擋", " 個", 0),
            Metric("plan.recommended", "推薦方案"),
            Metric("plan.score", "加權分數", "", 3),
            Metric("plan.projected_production_pct", "預測產線達成率", "%", 1),
            Metric("plan.projected_delay_min", "預測最大延遲", " min", 0),
        ),
        details=("plan.impact_orders", "plan.blocked", "plan.candidates"),
        required=("plan.count", "plan.recommended", "plan.candidates", "plan.impact_orders"),
    ),
    Act(
        act_id="A6",
        start_s=155.0,
        end_s=185.0,
        screen="核准與工單",
        doc_claim="主管一鍵核准，自動建立工單並指派最近、具資格的維修人員。",
        proves="高風險動作停在人這裡，核准者與理由寫進稽核軌跡；"
               "核准之後工單自動帶著零件、SOP 與工時開出來。",
        cue="停一下，讓評審看見系統在等人。念出 Policy 判定的風險等級，"
            "按下核准，再念工單的完整度與它帶了哪些 SOP。",
        trigger="approve",
        metrics=(
            Metric("approve.plan_id", "待核准方案"),
            Metric("approve.risk", "風險等級"),
            Metric("approve.approved", "核准"),
            Metric("approve.approver", "核准者"),
            Metric("work_order.id", "工單"),
            Metric("work_order.priority", "優先度"),
            Metric("work_order.skill", "指派技能"),
            Metric("work_order.repair_min", "預估工時", " min", 0),
            Metric("work_order.completeness_pct", "工單完整度", "%", 0),
        ),
        details=("approve.policy_reasons", "work_order.parts", "work_order.sop_refs"),
        required=("approve.plan_id", "approve.approver", "work_order.id", "work_order.completeness_pct"),
    ),
    Act(
        act_id="A7",
        start_s=185.0,
        end_s=215.0,
        screen="維修與驗證",
        doc_claim="完成替換／排除後，系統確認訊號回復正常。",
        proves="執行真的改變了孿生體狀態，驗證是在真實孿生體上重新量的，不是把預測值抄一遍。",
        cue="逐條念執行結果，然後念驗證：每一項的預測值 vs 實測值。"
            "重點是「實測」兩個字——這些數字是執行後重新量出來的。",
        trigger="verify",
        metrics=(
            Metric("verify.plan_id", "執行方案"),
            Metric("verify.passed", "驗證通過"),
            Metric("verify.check_count", "驗證項目", " 項", 0),
            Metric("verify.health_after", "機台健康度", "", 1),
            Metric("verify.production_pct_after", "產線達成率", "%", 1),
        ),
        details=("verify.effects", "verify.checks"),
        required=("verify.plan_id", "verify.checks", "verify.effects"),
    ),
    Act(
        act_id="A8",
        start_s=215.0,
        end_s=240.0,
        screen="成果總結",
        doc_claim="顯示反應時間、處置時間、避免停機損失與完整稽核紀錄。",
        proves="這四分鐘裡的每個數字都來自這一次執行；稽核軌跡逐筆落地，"
               "整場決策可以用指紋比對重現。",
        cue="收在三個數字上：反應時間、處置時間、相對『什麼都不做』避免的損失。"
            "最後把稽核筆數與決策指紋念出來——那是「可重現」的證據。",
        trigger="end",
        metrics=(
            Metric("summary.detection_latency_min", "反應時間（偵測延遲）", " min", 0),
            Metric("summary.time_to_diagnose_min", "處置時間（至可行動診斷）", " min", 0),
            Metric("summary.diagnosis_correct", "根因正確"),
            Metric("summary.production_attainment_pct", "產能達成率", "%", 1),
            Metric("summary.max_order_delay_min", "最大交期延遲", " min", 0),
            Metric("summary.machine_health_final", "設備最終健康度", "", 1),
            Metric("summary.secondary_damage", "二次損壞"),
            Metric("summary.avoided_loss_ntd", "避免的停機損失（對照 Baseline A）", " NTD", 0),
            Metric("summary.audit_records", "稽核紀錄", " 筆", 0),
            Metric("summary.link_mode", "鏈路模式"),
            Metric("summary.compute_seconds", "閉環實際運算耗時", " s", 2),
            Metric("summary.fingerprint", "決策指紋"),
        ),
        details=("summary.counterfactual", "summary.audit_stages"),
        required=("summary.production_attainment_pct", "summary.audit_records", "summary.fingerprint"),
    ),
)

ACT_IDS: tuple[str, ...] = tuple(a.act_id for a in SCRIPT)


def act(act_id: str) -> Act:
    for item in SCRIPT:
        if item.act_id == act_id:
            return item
    raise KeyError(f"未知段落 {act_id}；可用：{', '.join(ACT_IDS)}")


def acts_for(trigger: str) -> tuple[Act, ...]:
    """某個 Orchestrator stage 會觸發哪幾段（``diagnose`` 會觸發 A3 與 A4）。"""
    return tuple(a for a in SCRIPT if a.trigger == trigger)


def script_dict() -> dict[str, Any]:
    return {
        "source": "研究文件 §5.1 四分鐘閉環 Demo 劇本",
        "total_seconds": TOTAL_SECONDS,
        "acts": [a.to_dict() for a in SCRIPT],
    }


__all__ = [
    "ACT_IDS",
    "SCRIPT",
    "TOTAL_SECONDS",
    "Act",
    "Metric",
    "act",
    "acts_for",
    "mmss",
    "script_dict",
]
