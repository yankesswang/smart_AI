"""競品差異化 —— 對手是同一個評審體系選出來的得獎作品。

研究文件 §4.5 點名三件與本案最直接重疊的歷屆得獎作品。它們的特殊之處在於：
**它們是同一場競賽、同一套評審標準選出來的**。所以這份比較不是行銷素材，而是兩件事：

1. 證明這條賽道會得獎（智慧製造 × 設備異常，2022–2025 連續四年都有作品進前三）。
2. 證明我們沒有重做它們 —— 研究文件 §3.3 的最後一條失分警訊就是
   「重複 2022–2025 得獎作品的核心解法，未清楚說明新問題、新方法與新價值」。

論點軸線直接採用 §4.5 給的避讓策略：
**它們是單點偵測；我們是多模態根因 → 人工核准 → 工單 → 修復驗證的閉環。**

---
一條紀律
---
公開資料沒揭露的，我們不推測。研究文件自己就多次註明「細部技術規格未完整公開」，
所以下表的 ``public_scope`` 只寫官方新聞與公開專案資料寫得出來的範圍，
``unknown`` 欄位明確列出我們不知道的事。把對手講得比公開資料更弱，
在決賽問答時會被反咬 —— 而且那也不誠實。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Competitor:
    key: str
    name: str
    year: str
    award: str
    team: str
    public_scope: str
    overlap: str
    avoidance: str      # 研究文件 §4.5 給的避讓策略
    unknown: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "year": self.year,
            "award": self.award,
            "team": self.team,
            "public_scope": self.public_scope,
            "overlap": self.overlap,
            "avoidance": self.avoidance,
            "unknown": self.unknown,
            "source": self.source,
        }


COMPETITORS: tuple[Competitor, ...] = (
    Competitor(
        key="smart-tool-holder",
        name="AI 智慧刀把系統",
        year="2024",
        award="社會組季軍",
        team="加工智慧隊／沃龍工業、中正大學機械",
        public_scope="分析振動與受力訊號，預測刀具磨耗與剩餘壽命，降低停機與材料浪費。",
        overlap="預測磨耗／剩餘壽命 —— 與我們的振動訊號診斷共用同一類物理量。",
        avoidance=(
            "不要只做單一設備預測。我們的振動只是四個訊號之一，"
            "而且輸出不是壽命曲線而是**帶證據的根因排名**："
            "指紋餘弦 0.75 ＋ 歷史先驗 0.15 ＋ 文件支持 0.10，"
            "落選候選的餘弦值也一併呈現，讓判斷可以被反駁。"
            "更關鍵的是預測之後：影響分析 → 方案比較 → Safety 裁決 → 人工核准 → 工單 → 修復驗證。"
        ),
        unknown="公開資料未揭露其模型架構、資料量與是否具備工單／派工整合。",
        source="研究文件 §2（2024 得獎作品）與 §4.5〔S3、S5〕",
    ),
    Competitor(
        key="digital-twin-inspection",
        name="數位孿生 AI 檢測系統",
        year="2022",
        award="不分組亞軍",
        team="寒武紀灰姑娘蟲／和碩聯合科技",
        public_scope="虛實同步自動化與遠端虛擬機台操作，結合 5G MEC，朝無人工廠邁進。",
        overlap="數位孿生、虛實同步、遠端機台操作 —— 與我們的 Factory Digital Twin 表面上重疊。",
        avoidance=(
            "數位孿生只當選配，不要成為主敘事。我們的孿生體不是展示品，"
            "它有兩個功能性角色：(a) 讓方案在**診斷信念模型**上乾跑，"
            "所以診斷錯了投影就會錯（fork_as_belief 會拿掉真實故障標籤）；"
            "(b) 讓 Verification Agent 在**真實**孿生體上量執行後 KPI，不通過就重新規劃。"
            "孿生體在這裡是驗證機制，不是視覺化。"
        ),
        unknown="公開資料未揭露其孿生體是否參與決策，或僅用於同步與遠端操作。",
        source="研究文件 §2（2022 得獎作品）與 §4.5〔S7、S10〕",
    ),
    Competitor(
        key="partial-discharge",
        name="無所遁形",
        year="2025",
        award="社會組季軍",
        team="無所遁形／台電、震江、益大",
        public_scope="不中斷運轉地偵測電氣放電異常，目標是降低停電與火災風險。",
        overlap="不中斷運轉的異常偵測 —— 與我們「早期介入、不必先停機」的主張同向。",
        avoidance=(
            "避免把題目縮成單一電氣偵測，明確切換到設備根因與處置閉環。"
            "我們與它最大的差別是**偵測之後**：它回答「這裡有異常」，"
            "我們回答「是什麼原因、影響哪張訂單、有哪四個方案、哪個方案安全、誰簽名、"
            "工單派給誰、修完有沒有恢復」。"
            "Safety Agent 的預測型規則（SR-02P/SR-03P/SR-13）就是這個差別的具體形式："
            "它擋的不是當下的數值，是方案會把設備帶到的地方。"
        ),
        unknown="公開資料未揭露其感測原理細節、誤報率與是否連動工單或處置流程。",
        source="研究文件 §2（2025 得獎作品）與 §4.5〔S3〕",
    ),
)


@dataclass(frozen=True)
class ComparisonAxis:
    """比較軸：``ours`` 一律附上 repo 內可驗證的位置。"""

    axis: str
    them: str
    ours: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {"axis": self.axis, "them": self.them, "ours": self.ours, "evidence": self.evidence}


COMPARISON_AXES: tuple[ComparisonAxis, ...] = (
    ComparisonAxis(
        "感測模態",
        "以單一物理量為主（振動／受力、放電訊號、影像同步）。",
        "溫度、振動、電流、轉速 ＋ 現場影像（CameraObservation）＋ 手冊／SOP／歷史工單。",
        "twin/topology.py 的 SignalSpec、agents/vision.py、knowledge/",
    ),
    ComparisonAxis(
        "輸出形態",
        "異常分數、剩餘壽命或告警。",
        "根因排名 ＋ 信心度 ＋ 可引用 Evidence ＋ 替代假設（落選候選的餘弦值一併列出）。",
        "agents/diagnosis.py；Dashboard 步驟卡可展開推理過程",
    ),
    ComparisonAxis(
        "有沒有處置方案",
        "公開資料未見方案生成與比較。",
        "四個候選方案在信念模型上各乾跑 30 分鐘，六準則固定尺規加權排名（非 LLM）。",
        "agents/production.py、optimizer.py",
    ),
    ComparisonAxis(
        "安全是硬限制還是加權項",
        "公開資料未見安全否決機制。",
        "Safety Agent 具否決權，含**預測型**規則：擋的是方案會把設備帶到的狀態，不只當下數值。",
        "agents/safety.py 的 SR-02P / SR-03P / SR-13；policy/engine.py",
    ),
    ComparisonAxis(
        "人工在環",
        "公開資料未見權限與核准流程。",
        "停機／修改控制狀態等高風險動作必須人工核准；Safety Override 為系統禁止動作。",
        "policy/engine.py、CLI 與 Dashboard 的核准對話框",
    ),
    ComparisonAxis(
        "工單與派工",
        "公開資料未見工單整合。",
        "自動產生含零件清單、SOP 連結、所需技能與工時的工單，並量測完整度。",
        "agents/maintenance.py；KPI work_order_completeness_pct",
    ),
    ComparisonAxis(
        "修復後驗證",
        "公開資料未見閉環驗證。",
        "執行真的改變孿生體狀態，Verification Agent 在真實孿生體上量 KPI，不通過就重新規劃。",
        "agents/verification.py、orchestrator.py",
    ),
    ComparisonAxis(
        "可稽核性",
        "公開資料未見稽核軌跡設計。",
        "每個判斷與動作寫入 JSONL 稽核軌跡（一次閉環 27 筆、21,575 bytes，實測）。",
        "audit.py；deployment/budget.py::MEASURED_AUDIT_BYTES_PER_INCIDENT",
    ),
    ComparisonAxis(
        "有沒有對照組",
        "公開資料多以自身準確率呈現。",
        "三組對照組跑在相同 seed、相同情境、相同總時長上，KPI 直接可比。",
        "benchmark.py；/benchmark 頁面每次載入都真的重跑",
    ),
    ComparisonAxis(
        "商業論據",
        "公開資料未見量化 ROI 與定價。",
        "實測 KPI → 年化模型 → 三情境 ROI、回收期與破口分析，假設全部具名且標示來源。",
        "business/（本模組）、docs/factory_guardian/business_case.md",
    ),
)


POSITIONING = (
    "三件作品（就公開資料所示）都停在『偵測／預測』這一步，而且都是單一模態為主。"
    "Factory Guardian 的差異軸不是模型更準，而是把偵測之後的六個步驟做完並量測："
    "根因 → 影響 → 方案 → 安全裁決 → 人工核准 → 工單 → 修復驗證。"
    "這也是研究文件 §3.1 歸納的得獎共同特質裡最難的一項『從偵測走到行動』。"
)

RISK_NOTE = (
    "反向風險：這三件都得獎，代表評審熟悉這條賽道，也代表『再做一次單點偵測』會被直接看穿"
    "（§3.3 失分警訊最後一條）。我們的答法是把比較表放進提案書第 12 頁，主動指出重疊，"
    "再用可驗證的 repo 位置說明避讓 —— 而不是等評審自己聯想到。"
)


def competitor_report() -> dict[str, Any]:
    return {
        "note": "對手皆為同一評審體系選出之歷屆得獎作品（研究文件 §2、§4.5）。",
        "discipline": "公開資料未揭露之處一律標示 unknown，不做推測。",
        "competitors": [c.to_dict() for c in COMPETITORS],
        "axes": [a.to_dict() for a in COMPARISON_AXES],
        "positioning": POSITIONING,
        "risk": RISK_NOTE,
    }


__all__ = [
    "Competitor",
    "COMPETITORS",
    "ComparisonAxis",
    "COMPARISON_AXES",
    "POSITIONING",
    "RISK_NOTE",
    "competitor_report",
]
