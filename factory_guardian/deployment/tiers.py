"""Edge / Cloud 分層對照表、OT 網段隔離與中華電信能力對應。

這份表不是「把既有元件重新畫一次」的裝飾圖，它宣告了一條可以被測試驗證的不變式：

    **控制關鍵路徑（Detect → Diagnose → Safety → Approve → Execute → Verify）
    上的每一個元件都必須是 EDGE。**

由 ``critical_path_is_edge_only()`` 檢查，並由
``tests/test_deployment.py::test_no_cloud_component_sits_on_the_control_critical_path`` 守住。
只要有人把 LLM 或雲端知識庫放進閉環的決策路徑，測試就會紅。

分層依據（不是按「聽起來像邊緣」分，而是按三個可判定的條件）：

1. **確定性**：輸出只由輸入決定，沒有外部服務、沒有取樣隨機性 → 可以放邊緣。
2. **延遲敏感**：決策晚了就沒有意義（工安阻擋、執行前閘門）→ 必須放邊緣。
3. **資料主權**：輸入含可辨識人員影像或製程 know-how → 不得出廠，必須放邊緣。

三個條件任一成立就是 EDGE。CLOUD 只留給「算得起、等得起、送得出去」的東西。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Tier(str, Enum):
    """部署層。"""

    EDGE = "edge"      # 廠內 MEC 節點（中華電信 5G 專網 + MEC）
    CLOUD = "cloud"    # hicloud / AI 平台


class OfflineImpact(str, Enum):
    """雲端鏈路中斷時，這個元件會發生什麼事。"""

    NONE = "none"            # 完全不受影響
    DEGRADED = "degraded"    # 功能仍在，但品質下降（換用邊緣替代實作）
    DEFERRED = "deferred"    # 暫停並在邊緣排隊，鏈路恢復後補送


@dataclass(frozen=True)
class DeploymentComponent:
    """一個既有元件在部署拓撲上的位置。"""

    component_id: str
    name: str
    tier: Tier
    module: str                     # 對應的程式碼位置（可被追查，不是抽象方塊）
    role: str
    critical_path: bool             # 是否位於控制關鍵路徑上
    latency_budget_ms: float | None  # 該元件自身的延遲預算；None 代表非延遲敏感
    offline_impact: OfflineImpact
    offline_behavior: str
    placement_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "name": self.name,
            "tier": self.tier.value,
            "module": self.module,
            "role": self.role,
            "critical_path": self.critical_path,
            "latency_budget_ms": self.latency_budget_ms,
            "offline_impact": self.offline_impact.value,
            "offline_behavior": self.offline_behavior,
            "placement_reason": self.placement_reason,
        }


# --------------------------------------------------------------------------------------
# EDGE —— 廠內 MEC 節點
# 全部是確定性運算：沒有一個需要打外部 API，斷網照跑。
# --------------------------------------------------------------------------------------
_EDGE: tuple[DeploymentComponent, ...] = (
    DeploymentComponent(
        "monitoring-agent", "Monitoring Agent", Tier.EDGE,
        "factory_guardian/agents/monitoring.py",
        "門檻 + 趨勢 + 健康度三重偵測，產生 AnomalyEvent",
        critical_path=True, latency_budget_ms=50.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：純數值判斷，輸入來自廠內 OPC-UA / MQTT。",
        placement_reason="延遲敏感 + 確定性。偵測延遲是本系統對照 Baseline 的第一個 KPI，不能押在 WAN 上。",
    ),
    DeploymentComponent(
        "diagnosis-fingerprint", "Diagnosis Agent（感測器指紋比對）", Tier.EDGE,
        "factory_guardian/agents/diagnosis.py",
        "指紋餘弦相似度 0.75 + 歷史先驗 0.15 + 文件支持 0.10 的加權排名",
        critical_path=True, latency_budget_ms=100.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：排名由數值指紋主導，RAG 走邊緣本地 TF-IDF 索引。",
        placement_reason="確定性 + 資料主權。原始感測波形是製程 know-how，不出廠。",
    ),
    DeploymentComponent(
        "knowledge-local", "本地知識庫（TF-IDF 檢索）", Tier.EDGE,
        "factory_guardian/knowledge/retriever.py",
        "手冊 / SOP / 維修紀錄的 CJK-aware TF-IDF 檢索，產生可引用 Evidence",
        critical_path=True, latency_budget_ms=30.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：語料與索引都在邊緣節點上。",
        placement_reason="資料主權。設備手冊與維修紀錄屬工廠資產，且離線可用是硬需求。",
    ),
    DeploymentComponent(
        "vision-inference", "Vision Backend（CV / VLM 推論）", Tier.EDGE,
        "factory_guardian/agents/vision.py",
        "影像判讀出 CameraObservation：人數、危險區、PPE、跌倒、煙霧",
        critical_path=True, latency_budget_ms=30.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：模型權重常駐邊緣 GPU，只在鏈路恢復時拉取新版本。",
        placement_reason="三個條件全中。影格含可辨識人員影像（個資），且工安判讀不能等 WAN。",
    ),
    DeploymentComponent(
        "safety-agent", "Safety Agent（硬規則 + 預測型規則）", Tier.EDGE,
        "factory_guardian/agents/safety.py",
        "SR-xx 規則裁決、方案審查、執行前最後閘門 gate_execution()",
        critical_path=True, latency_budget_ms=20.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響。這是整份表最不可退讓的一列：安全裁決永遠不得依賴可能斷掉的鏈路。",
        placement_reason="延遲敏感 + 確定性。安全功能的可用度不可以是 WAN 可用度的下游。",
    ),
    DeploymentComponent(
        "policy-engine", "Policy Engine（動作權限 / 人工核准）", Tier.EDGE,
        "factory_guardian/policy/engine.py",
        "動作風險分級、禁止 safety_override、判定是否需人工核准",
        critical_path=True, latency_budget_ms=5.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：政策表在邊緣，核准由廠內 HMI 完成。",
        placement_reason="延遲敏感 + 確定性。治理決策若因斷網而無法作出，等於治理失效。",
    ),
    DeploymentComponent(
        "production-agent", "Production Agent（影響分析 + 方案投影）", Tier.EDGE,
        "factory_guardian/agents/production.py",
        "Knowledge Graph 追訂單影響、在信念模型上乾跑 30 分鐘投影",
        critical_path=True, latency_budget_ms=500.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：投影跑在邊緣的孿生體上。",
        placement_reason="資料主權。訂單與產能資料屬工廠營運機密。",
    ),
    DeploymentComponent(
        "optimizer", "Plan Optimizer（加權多準則排名）", Tier.EDGE,
        "factory_guardian/optimizer.py",
        "固定尺規的六準則加權排名；Safety BLOCK 為硬限制",
        critical_path=True, latency_budget_ms=50.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：純計算，本來就不是 LLM。",
        placement_reason="確定性。排名必須跨情境、跨執行可重現，不能依賴外部服務的版本。",
    ),
    DeploymentComponent(
        "maintenance-agent", "Maintenance Agent（工單生成）", Tier.EDGE,
        "factory_guardian/agents/maintenance.py",
        "產生含 SOP 引用、料件與安全注意事項的工單",
        critical_path=False, latency_budget_ms=100.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：工單在邊緣生成並派發至廠內 CMMS；雲端只做副本。",
        placement_reason="確定性。派工是現場動作，不該因為對外斷線而停擺。",
    ),
    DeploymentComponent(
        "verification-agent", "Verification Agent（執行後驗證）", Tier.EDGE,
        "factory_guardian/agents/verification.py",
        "在真實孿生體上量 KPI、比對預測與反事實，不通過就觸發重新規劃",
        critical_path=True, latency_budget_ms=100.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：量測對象是廠內設備狀態。",
        placement_reason="確定性 + 延遲敏感。閉環的「閉」字在這裡，斷了就不是閉環。",
    ),
    DeploymentComponent(
        "orchestrator", "Orchestrator（八階段閉環 + 重試）", Tier.EDGE,
        "factory_guardian/orchestrator.py",
        "串接偵測到驗證的完整流程，驗證失敗時重新規劃",
        critical_path=True, latency_budget_ms=None,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：協調邏輯本身沒有任何雲端呼叫。",
        placement_reason="延遲敏感。流程控制與被控設備必須在同一個故障域內。",
    ),
    DeploymentComponent(
        "digital-twin", "Digital Twin / 設備介接層", Tier.EDGE,
        "factory_guardian/twin/engine.py",
        "設備狀態、感測讀值與動作執行；正式導入時由 OPC-UA / MQTT / MES Adapter 取代",
        critical_path=True, latency_budget_ms=100.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：直接掛在 OT 網段的設備上。",
        placement_reason="三個條件全中。控制指令的來源必須在 OT 區內，不接受來自區外的寫入。",
    ),
    DeploymentComponent(
        "audit-log", "Audit Trail 寫入（JSONL）", Tier.EDGE,
        "factory_guardian/audit.py",
        "每個判斷、裁決、核准與狀態變更的不可否認紀錄",
        critical_path=True, latency_budget_ms=5.0,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：先落地邊緣磁碟；鏈路恢復後再同步到 OT SOC。",
        placement_reason="延遲敏感 + 資料主權。稽核不能因為斷網而出現空窗，否則整條證據鏈就有缺口。",
    ),
    DeploymentComponent(
        "hmi-dashboard", "廠內 HMI / 戰情 Dashboard", Tier.EDGE,
        "factory_guardian/api/",
        "現場操作、人工核准對話框、稽核串流",
        critical_path=True, latency_budget_ms=None,
        offline_impact=OfflineImpact.NONE,
        offline_behavior="不受影響：由邊緣節點自行提供服務，前端不下載任何外部字型或 CDN 資源。",
        placement_reason="延遲敏感。人工核准是閉環的一站，操作介面斷線等於閉環斷點。",
    ),
)


# --------------------------------------------------------------------------------------
# CLOUD —— hicloud / AI 平台
# 共同特徵：都不在控制關鍵路徑上，斷網時降級但不阻斷閉環。
# --------------------------------------------------------------------------------------
_CLOUD: tuple[DeploymentComponent, ...] = (
    DeploymentComponent(
        "llm-narrative", "LLM 敘述生成", Tier.CLOUD,
        "factory_guardian/llm.py",
        "把引擎已經算好的數字與證據寫成工程師看得懂的說明文字",
        critical_path=False, latency_budget_ms=3000.0,
        offline_impact=OfflineImpact.DEGRADED,
        offline_behavior="降級：改用邊緣確定性敘述器。數字、證據與結論完全相同，只有文字較制式。",
        placement_reason="非延遲敏感且算力昂貴。LLM 本來就被規格 §9 排除在控制決策之外，放雲端不影響安全性。",
    ),
    DeploymentComponent(
        "knowledge-vector", "向量知識庫 / 跨廠語料", Tier.CLOUD,
        "factory_guardian/knowledge/retriever.py（未來替換點）",
        "跨廠手冊、設備商文件與案例的向量檢索",
        critical_path=False, latency_budget_ms=500.0,
        offline_impact=OfflineImpact.DEGRADED,
        offline_behavior="降級：退回邊緣 TF-IDF 索引，Evidence 仍可引用，只是召回範圍縮小到本廠語料。",
        placement_reason="跨廠語料的體積與更新頻率不適合每個邊緣節點各存一份；且它只影響證據豐富度，不影響裁決。",
    ),
    DeploymentComponent(
        "event-report", "事件報告與長期 KPI 儀表板", Tier.CLOUD,
        "factory_guardian/benchmark.py（報表匯出）",
        "跨班別、跨月的 KPI 匯總與管理層報表",
        critical_path=False, latency_budget_ms=None,
        offline_impact=OfflineImpact.DEFERRED,
        offline_behavior="延後：事件在邊緣排隊（JSONL 落地），鏈路恢復後補送，不會遺失。",
        placement_reason="時間尺度是天與月，不是毫秒。放雲端才有跨廠比較的價值。",
    ),
    DeploymentComponent(
        "model-update", "模型與門檻更新", Tier.CLOUD,
        "factory_guardian/prediction/service.py、agents/vision.py",
        "TabFM / VLM checkpoint 與偵測門檻的版本發佈",
        critical_path=False, latency_budget_ms=None,
        offline_impact=OfflineImpact.DEFERRED,
        offline_behavior="延後：邊緣沿用既有版本繼續運作。更新一律為簽章驗證後的單向下拉，雲端不得推送。",
        placement_reason="訓練需要跨廠資料與 GPU 叢集；但發佈是低頻動作，不需要即時性。",
    ),
    DeploymentComponent(
        "cross-site-analytics", "跨廠分析與 Benchmark 匯總", Tier.CLOUD,
        "factory_guardian/benchmark.py",
        "多廠區對照組比較、故障型態分布與門檻校準建議",
        critical_path=False, latency_budget_ms=None,
        offline_impact=OfflineImpact.DEFERRED,
        offline_behavior="延後：單廠閉環與 KPI 量測完全不依賴它。",
        placement_reason="定義上需要多個廠的資料，邊緣做不到。",
    ),
    DeploymentComponent(
        "soc-siem", "OT SOC / SIEM 日誌匯入", Tier.CLOUD,
        "factory_guardian/audit.py（JSONL 輸出即為 SOC 的證據來源）",
        "稽核軌跡與資安日誌的集中監控、威脅偵測與事件追蹤",
        critical_path=False, latency_budget_ms=None,
        offline_impact=OfflineImpact.DEFERRED,
        offline_behavior="延後：邊緣先落地，鏈路恢復後補送。補送時保留原始 ts 與 run_id，時序不會被改寫。",
        placement_reason="集中監控需要跨場域視野；但稽核的**產生**在邊緣，所以斷網不會產生證據空窗。",
    ),
)

COMPONENTS: tuple[DeploymentComponent, ...] = _EDGE + _CLOUD


# --------------------------------------------------------------------------------------
# OT 網段隔離（IEC 62443 分區 / Purdue 模型）
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NetworkZone:
    zone_id: str
    level: str
    name: str
    contains: tuple[str, ...]
    transport: str
    egress: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id, "level": self.level, "name": self.name,
            "contains": list(self.contains), "transport": self.transport, "egress": self.egress,
        }


NETWORK_ZONES: tuple[NetworkZone, ...] = (
    NetworkZone(
        "Z-FIELD", "L0–L1", "現場層（感測器 / PLC / 致動器）",
        ("振動與溫度感測器", "PLC", "安全電驛", "IP Camera"),
        "有線 fieldbus / 5G 專網 CPE，不接任何 IT 網段",
        "禁止出廠。只允許往 Z-CELL 的單一方向資料流。",
    ),
    NetworkZone(
        "Z-CELL", "L2", "單元層（MEC 邊緣節點）",
        ("Agent 閉環", "Digital Twin / 設備 Adapter", "影像推論", "本地知識庫", "稽核落地"),
        "中華電信 5G 專網獨立 slice + MEC local breakout，無預設對外路由",
        "僅能經 Z-DMZ 出廠，且只能送出經過政策過濾的資料。",
    ),
    NetworkZone(
        "Z-SITE", "L3", "廠務層（MES / CMMS / HMI）",
        ("工單派發", "排程", "廠內戰情 Dashboard", "人工核准介面"),
        "廠內有線網路",
        "僅能經 Z-DMZ 出廠。",
    ),
    NetworkZone(
        "Z-DMZ", "L3.5", "工業 DMZ（唯一出廠點）",
        ("資料分類過濾", "去識別化", "外送佇列", "簽章驗證的模型下拉"),
        "單向轉送 + 應用層代理，不做 L3 轉發",
        "全廠唯一的對外出口。所有出廠流量在此逐筆比對資料分類政策。",
    ),
    NetworkZone(
        "Z-CLOUD", "L4–L5", "企業 / 雲端層（hicloud）",
        ("LLM 敘述", "向量知識庫", "事件報告", "模型訓練與發佈", "OT SOC / SIEM"),
        "hicloud 專線 / VPN",
        "不得對 Z-CELL 發起連線；所有互動皆由邊緣主動發起。",
    ),
)


@dataclass(frozen=True)
class EgressRule:
    data_class: str
    allowed: bool
    reason: str
    control: str

    def to_dict(self) -> dict[str, Any]:
        return {"data_class": self.data_class, "allowed": self.allowed,
                "reason": self.reason, "control": self.control}


#: 資料分類政策 —— 「哪些流量必須留在廠內、哪些可以出廠」。
DATA_EGRESS_POLICY: tuple[EgressRule, ...] = (
    EgressRule(
        "原始攝影機影格", False,
        "含可辨識人員影像（個資與生物特徵）；工安場景更敏感，一旦外流無法回收。",
        "影像只在 Z-CELL 內解碼與推論；出廠的只有 CameraObservation 的欄位值（人數、是否在危險區、PPE 布林值）。",
    ),
    EgressRule(
        "原始感測波形（加速度計 / 電流）", False,
        "取樣率足以還原加工參數與刀具路徑，等同製程 know-how。",
        "邊緣做 RMS / FFT 特徵萃取，只送出純量與特徵向量。",
    ),
    EgressRule(
        "控制指令（stop_machine / derate / start_maintenance）", False,
        "控制平面若接受來自 OT 區外的寫入，等於把停機權交給 WAN 的可用度與安全性。",
        "Digital Twin / OPC-UA Adapter 只接受來自 Z-CELL 的動作；Z-DMZ 不轉發任何寫入類請求。",
    ),
    EgressRule(
        "設備憑證 / PLC 位址表 / 網路拓撲", False,
        "屬於攻擊者最想要的偵察資料。",
        "僅存於 Z-CELL；SOC 取得的是事件與雜湊，不是位址表本身。",
    ),
    EgressRule(
        "稽核軌跡 JSONL（事件、裁決、核准）", True,
        "SOC 與稽核需要不可否認紀錄；內容是決策而非原始感測或影像。",
        "經 Z-DMZ 去識別化（人員以角色代碼表示）後外送；斷網時在邊緣排隊。",
    ),
    EgressRule(
        "LLM 敘述用 prompt（數值 + 代號）", True,
        "只含引擎已算好的數字、機台代號與故障代碼，不含人員身分或影像。",
        "prompt 由 Agent 組裝，欄位固定可審查；斷網時整條敘述改在邊緣產生，不出廠。",
    ),
    EgressRule(
        "KPI 匯總與事件報告", True,
        "管理與跨廠比較所需，時間尺度是小時與天。",
        "經 Z-DMZ 外送；延遲或中斷不影響閉環。",
    ),
    EgressRule(
        "模型與門檻更新（下行）", True,
        "邊緣需要新版模型與校準門檻。",
        "單向 pull-only，簽章驗證後才載入；雲端無法推送，也無法觸發邊緣執行任何動作。",
    ),
)


# --------------------------------------------------------------------------------------
# 中華電信能力對應
# 每一列都標 status，避免把「規劃中」講成「已完成」——虛報是比缺項更嚴重的失分。
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ChtCapability:
    capability: str
    role: str
    requirement: str
    status: str          # demonstrated / interface-ready / requirement-derived / roadmap
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "role": self.role, "requirement": self.requirement,
                "status": self.status, "evidence": self.evidence}


CHT_CAPABILITIES: tuple[ChtCapability, ...] = (
    ChtCapability(
        "5G 企業專網",
        "Camera → MEC 上行、MEC → PLC 下行、以 slice 對 OT 流量做網段隔離與 QoS 保證。",
        "見 bandwidth_budget()：pilot line 需 16.2 Mbps 持續上行；安全鏈路單向延遲 ≤ 10 ms；"
        "安全功能可用度目標 99.9%，反推每段需 ≥ 99.95%。",
        "requirement-derived",
        "頻寬與延遲數字由本模組計算，計算假設全部標示在 assumptions 欄位；實際頻譜與涵蓋需 CHT RF 規劃確認。",
    ),
    ChtCapability(
        "MEC 邊緣運算",
        "承載 EDGE 層全部元件；斷網時整條閉環仍在廠內完成。",
        "邊緣決策鏈 p95 ≤ 250 ms；MEC 節點需具備可跑 CV 推論的 GPU。",
        "demonstrated",
        "FG_CLOUD_LINK=0 下完整閉環仍通過驗證，由 "
        "tests/test_deployment.py::test_closed_loop_completes_and_verifies_with_cloud_link_down 證明。",
    ),
    ChtCapability(
        "hicloud / AI 平台",
        "LLM 敘述、向量知識庫、事件報告、模型訓練與發佈。",
        "非關鍵路徑：斷線時全部降級或延後，閉環不中斷。",
        "interface-ready",
        "整合點是 llm.py 的 LLMClient 與 knowledge/retriever.py 的 KnowledgeBase，兩者都已是可替換介面。",
    ),
    ChtCapability(
        "OT 資安 / SOC",
        "設備身分（SIM 綁定）、存取控制、日誌集中、威脅監控與事件追蹤。",
        "稽核完整率 100%；控制指令零外部來源；斷網期間不得產生稽核空窗。",
        "interface-ready",
        "audit.py 產生的 JSONL 就是 SOC 的證據來源（一行一事件、含 run_id 與 UTC 時戳）；"
        "DATA_EGRESS_POLICY 定義了 DMZ 的過濾規則。",
    ),
    ChtCapability(
        "AI 定位",
        "派工時找出最近且具備所需技能的維修人員。",
        "僅在場域確有定位需求時導入。",
        "roadmap",
        "工單已有 required_skill 與 estimated_repair_min 欄位可承接，但**尚未接定位服務**，不宣稱已實作。",
    ),
)


# --------------------------------------------------------------------------------------
# 查詢
# --------------------------------------------------------------------------------------
def components_by_tier(tier: Tier) -> list[DeploymentComponent]:
    return [c for c in COMPONENTS if c.tier is tier]


def critical_path_components() -> list[DeploymentComponent]:
    return [c for c in COMPONENTS if c.critical_path]


def critical_path_is_edge_only() -> bool:
    """關鍵路徑上不得出現 CLOUD 元件。這是整個部署設計的不變式。"""
    return all(c.tier is Tier.EDGE for c in critical_path_components())


def cloud_components_degrade_gracefully() -> bool:
    """所有 CLOUD 元件在斷網時都必須有明確的降級或延後行為，不得直接失效。"""
    return all(
        c.offline_impact in (OfflineImpact.DEGRADED, OfflineImpact.DEFERRED)
        for c in components_by_tier(Tier.CLOUD)
    )


def describe_tiers() -> dict[str, Any]:
    """給 API 與 Dashboard 的分層對照表。"""
    edge = components_by_tier(Tier.EDGE)
    cloud = components_by_tier(Tier.CLOUD)
    return {
        "components": [c.to_dict() for c in COMPONENTS],
        "summary": {
            "edge_count": len(edge),
            "cloud_count": len(cloud),
            "critical_path_count": len(critical_path_components()),
            "critical_path_is_edge_only": critical_path_is_edge_only(),
            "cloud_components_degrade_gracefully": cloud_components_degrade_gracefully(),
        },
        "invariant": (
            "控制關鍵路徑（Detect → Diagnose → Safety → Approve → Execute → Verify）"
            "上的每一個元件都是 EDGE。雲端只提供敘述、知識與報表，斷線時降級而不阻斷閉環。"
        ),
        "placement_criteria": [
            "確定性：輸出只由輸入決定，沒有外部服務相依 → 可放邊緣。",
            "延遲敏感：決策晚了就失去意義（工安阻擋、執行前閘門）→ 必須放邊緣。",
            "資料主權：輸入含可辨識人員影像或製程 know-how → 不得出廠。",
        ],
        "network_zones": [z.to_dict() for z in NETWORK_ZONES],
        "egress_policy": [r.to_dict() for r in DATA_EGRESS_POLICY],
        "cht_capabilities": [c.to_dict() for c in CHT_CAPABILITIES],
    }


__all__ = [
    "CHT_CAPABILITIES",
    "COMPONENTS",
    "DATA_EGRESS_POLICY",
    "NETWORK_ZONES",
    "ChtCapability",
    "DeploymentComponent",
    "EgressRule",
    "NetworkZone",
    "OfflineImpact",
    "Tier",
    "cloud_components_degrade_gracefully",
    "components_by_tier",
    "critical_path_components",
    "critical_path_is_edge_only",
    "describe_tiers",
]
