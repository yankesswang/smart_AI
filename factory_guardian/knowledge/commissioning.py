"""設備安裝驗收記錄（As-Commissioned Baseline）。

⚠ 全部為競賽用合成資料（Synthetic），不代表任何真實設備商規格或真實驗收紀錄。

這份文件回答一個現實問題：**「正常」到底是誰的正常？**

手冊寫的是同型號設備的**通用**規格（例如「振動正常低於 4 mm/s」），
但每一台機器交機試車時量到的基準值都不一樣 —— 同型號、同批號的兩台 CNC，
主軸振動基準可能差 20%（組裝公差、地基剛性、環境溫度、安裝水平度都會影響）。

真實工廠的預測性維護因此不是拿讀值去比手冊，而是比**這台機器自己的交機基準**：
M-A 振動 3.4 mm/s 是「比自己基準高 79%」（顯著劣化）；
同樣 3.4 mm/s 在 M-B 上只是「比自己基準高 26%」（還在正常散布內）。
手冊門檻（4 mm/s）對兩台機器一視同仁，會漏掉前者、誤報後者。

這份基準也是**刻意跟模擬器的 nominal 不同**的：
交機驗收是在特定條件下量的（無負載、室溫 26°C、熱平衡 30 分鐘），
跟產線實際運轉時的穩態值本來就有落差。Agent 必須自己處理這個落差，
而不是拿到一份跟模擬器內部參數完全對齊的答案。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalBaseline:
    """單一訊號的交機基準值。"""

    signal: str
    unit: str
    # 試車時量到的基準值
    baseline: float
    # 驗收允收帶（同一台機器重複量測的正常散布範圍）
    tolerance: float
    condition: str = ""

    def deviation_ratio(self, value: float) -> float:
        """相對本機基準的偏離倍數（以允收帶為單位）。

        回傳 0 代表落在允收帶內；1.0 代表超出允收帶一個帶寬。
        用允收帶而非固定 scale 正規化，是因為每台機器的量測散布本來就不同：
        振動基準低的機器，同樣的絕對變化代表更嚴重的劣化。
        """
        if self.tolerance <= 1e-9:
            return 0.0
        excess = abs(value - self.baseline) - self.tolerance
        if excess <= 0:
            return 0.0
        return excess / self.tolerance


@dataclass(frozen=True)
class CommissioningRecord:
    """一台機器的安裝驗收記錄。"""

    doc_id: str
    machine_id: str
    model: str
    serial_no: str
    installed_on: str
    commissioned_by: str
    baselines: tuple[SignalBaseline, ...]
    note: str = ""
    synthetic: bool = True

    def baseline_of(self, signal: str) -> SignalBaseline | None:
        return next((b for b in self.baselines if b.signal == signal), None)

    def as_text(self) -> str:
        """給 RAG 檢索用的純文字表示。"""
        rows = "；".join(
            f"{b.signal} 基準 {b.baseline:g}{b.unit}（允收 ±{b.tolerance:g}）"
            for b in self.baselines
        )
        return (
            f"[{self.doc_id}] {self.machine_id} {self.model} SN {self.serial_no}｜"
            f"{self.installed_on} 由 {self.commissioned_by} 驗收｜試車基準：{rows}。{self.note}"
        )


# --------------------------------------------------------------------------------------
# 三台機器的交機驗收記錄
#
# 關鍵設計：M-A 與 M-B 是同型號，但基準值刻意不同。
#   M-A 振動基準 1.9（組裝品質好），M-B 2.6（安裝水平度較差，地基共振較明顯）
#   → 同樣讀到 3.6 mm/s，對 M-A 是明顯異常，對 M-B 還在正常散布內。
# 這個差異讓 Agent 沒辦法只靠一組通用門檻做判斷。
# --------------------------------------------------------------------------------------
COMMISSIONING: tuple[CommissioningRecord, ...] = (
    CommissioningRecord(
        doc_id="COMM-M-A-2023",
        machine_id="M-A",
        model="CNC-VMC850",
        serial_no="850-22417",
        installed_on="2023-11-08",
        commissioned_by="陳志明（L2 機械技師）",
        baselines=(
            SignalBaseline("vibration", "mm/s", baseline=1.9, tolerance=0.55,
                           condition="8000 rpm 無負載，熱平衡後量測"),
            SignalBaseline("temperature", "°C", baseline=58.4, tolerance=4.5,
                           condition="連續運轉 30 分鐘熱平衡，室溫 26°C"),
            SignalBaseline("current", "A", baseline=9.6, tolerance=1.1,
                           condition="額定負載試切"),
            SignalBaseline("rpm_pct", "%", baseline=99.2, tolerance=1.2,
                           condition="目標轉速達成率"),
        ),
        note=(
            "本機主軸振動基準優於同型號平均（2.3 mm/s），組裝與動平衡品質良好。"
            "更換主軸軸承後應回到 2.1 mm/s 以內，若無法回到基準需檢查軸頸。"
            "地基水平度 0.02 mm/m，符合原廠要求。"
        ),
    ),
    CommissioningRecord(
        doc_id="COMM-M-B-2024",
        machine_id="M-B",
        model="CNC-VMC850",
        serial_no="850-24106",
        installed_on="2024-03-19",
        commissioned_by="張美惠（L2 機械技師）",
        baselines=(
            SignalBaseline("vibration", "mm/s", baseline=2.6, tolerance=0.70,
                           condition="8000 rpm 無負載，熱平衡後量測"),
            SignalBaseline("temperature", "°C", baseline=61.0, tolerance=5.0,
                           condition="連續運轉 30 分鐘熱平衡，室溫 27°C"),
            SignalBaseline("current", "A", baseline=10.4, tolerance=1.3,
                           condition="額定負載試切"),
            SignalBaseline("rpm_pct", "%", baseline=98.6, tolerance=1.5,
                           condition="目標轉速達成率"),
        ),
        note=(
            "本機振動基準高於 M-A，驗收時已確認為地基共振所致（安裝位置靠近廠房樑柱），"
            "非主軸本體問題。判讀本機振動時應以 2.6 mm/s 為基準，不可直接套用 M-A 的判讀經驗。"
            "地基水平度 0.05 mm/m，在允收範圍但接近上限。"
        ),
    ),
    CommissioningRecord(
        doc_id="COMM-M-C-2023",
        machine_id="M-C",
        model="PKG-AL220",
        serial_no="AL220-9932",
        installed_on="2023-11-15",
        commissioned_by="王建良（L3 電氣技師）",
        baselines=(
            SignalBaseline("temperature", "°C", baseline=54.0, tolerance=5.5,
                           condition="連續包裝 30 分鐘，室溫 26°C"),
            SignalBaseline("current", "A", baseline=9.1, tolerance=1.2,
                           condition="額定包裝速率"),
            SignalBaseline("rpm_pct", "%", baseline=99.5, tolerance=1.0,
                           condition="輸送帶速率達成率"),
        ),
        note="包裝機無主軸振動監測點。溫升主要來自封口加熱器，與加工機的熱源性質不同。",
    ),
)

_COMM_INDEX = {rec.machine_id: rec for rec in COMMISSIONING}
_COMM_BY_DOC = {rec.doc_id: rec for rec in COMMISSIONING}


def commissioning_for(machine_id: str) -> CommissioningRecord | None:
    return _COMM_INDEX.get(machine_id)


def commissioning_by_doc(doc_id: str) -> CommissioningRecord | None:
    return _COMM_BY_DOC.get(doc_id)


__all__ = [
    "SignalBaseline",
    "CommissioningRecord",
    "COMMISSIONING",
    "commissioning_for",
    "commissioning_by_doc",
]
