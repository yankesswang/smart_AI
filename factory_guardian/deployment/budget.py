"""頻寬與延遲預算 —— 論證為什麼需要 5G 專網 + MEC，而不是 Wi-Fi + 公有雲。

寫這個模組的規則只有一條：**每個數字都要能追到它的來源**。
所以每一筆都帶 ``basis`` 欄位，只有四種值：

* ``measured``：在這個 repo 上實測得到的數字（下方 MEASURED_* 常數，附量測條件）。
* ``live-measured``：API 被呼叫時當場重跑量測（見 :func:`measure_edge_decision_latency`）。
* ``vendor-typical``：硬體規格書的典型值範圍，取保守中值。
* ``assumption``：部署階段的工程假設，計算式與參數全部列在 ``assumptions`` 裡。

沒有第五種。任何無法歸入這四類的數字都不該出現在提案書裡。

---
本 MVP 的實測條件
---
* Python 3，單執行緒，一般筆電 CPU，無 GPU，離線模式（不打 LLM API）。
* 情境 ``bearing-degradation``、``seed=20260809``、``FG_REQUIRE_APPROVAL=0``。
* 完整閉環（Detect → … → Verify）牆鐘時間 56.5 ms。
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Any

# ======================================================================================
# 實測常數（來源：本 repo 的 bearing-degradation 閉環）
# ======================================================================================
#: 一次完整閉環寫出的稽核軌跡位元組數（27 筆 JSONL 紀錄）。
MEASURED_AUDIT_BYTES_PER_INCIDENT = 21_575
MEASURED_AUDIT_RECORDS_PER_INCIDENT = 27
#: 一次閉環 5 次敘述呼叫的 prompt 與回應總位元組數（有金鑰時才會真的送出）。
MEASURED_LLM_PROMPT_BYTES_PER_INCIDENT = 9_495
MEASURED_LLM_COMPLETION_BYTES_PER_INCIDENT = 1_959
MEASURED_LLM_CALLS_PER_INCIDENT = 5
#: 一筆遙測匯總（3 台機台 × 健康度與感測讀值 + 產線 KPI）序列化後的位元組數。
MEASURED_KPI_ROLLUP_BYTES = 377
#: 完整閉環的邊緣運算牆鐘時間。
MEASURED_LOOP_WALL_MS = 56.5
#: 各 Agent 的平均決策延遲（``Agent.metrics()`` 的 avg_decision_latency_ms）。
MEASURED_AGENT_DECISION_MS: dict[str, float] = {
    "monitoring-agent": 0.05,
    "diagnosis-agent": 1.41,
    "production-agent": 19.59,
    "safety-agent": 0.02,
    "maintenance-agent": 0.06,
    "verification-agent": 5.56,
}

# ======================================================================================
# 部署假設（每一個都要能被質疑、能被替換）
# ======================================================================================
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 15
#: H.264 High profile 在「固定機位、背景靜止、中低動態」的工廠場景下的壓縮效率。
VIDEO_BITS_PER_PIXEL = 0.10
#: CBR 餘裕 + I-frame 尖峰 + RTP/UDP/IP 封包表頭。
VIDEO_OVERHEAD_FACTOR = 1.30

#: 感測器輪詢頻率。MVP 的一個 tick 代表 60 秒模擬時間；1 Hz 是導入階段的假設值，
#: 依據是趨勢型偵測（Monitoring Agent 的 trend 規則）需要分鐘級以內的取樣密度。
SENSOR_POLL_HZ = 1.0
#: 單筆讀值的 OPC-UA / MQTT 承載（machine_id、signal、value、unit、ts、quality）。
SENSOR_PAYLOAD_BYTES = 120
#: MQTT + TCP/IP 表頭。
SENSOR_TRANSPORT_HEADER_BYTES = 60

#: 若**不做**邊緣前處理，軸承診斷需要的原始加速度計波形規格。
#: 10 kHz 才足以覆蓋 BPFO/BPFI 特徵頻率及其諧波。
ACCEL_AXES = 3
ACCEL_SAMPLE_HZ = 10_000
ACCEL_BITS_PER_SAMPLE = 16
#: 具備旋轉件、需要振動診斷的機台數（M-A、M-B 兩台 CNC）。
VIBRATION_MACHINES = 2
#: 這兩台機台經邊緣特徵萃取後上傳的純量訊號數（每台 4 個）。
VIBRATION_MACHINE_SIGNALS = 8

#: 每月事故數的保守假設（pilot line，約每日 7 次告警等級事件）。
INCIDENTS_PER_MONTH = 200
#: 常態遙測匯總頻率：每分鐘一筆。
ROLLUPS_PER_MONTH = 60 * 24 * 30
SECONDS_PER_MONTH = 60 * 60 * 24 * 30

#: 串聯可用度計算用的單段可用度假設。
SEGMENT_AVAILABILITY = 0.999
#: 安全功能的可用度目標。
SAFETY_AVAILABILITY_TARGET = 0.999

#: 安全鏈路端到端延遲預算上限。
SAFETY_LATENCY_TARGET_MS = 250.0


# ======================================================================================
# 攝影機規模
# ======================================================================================
@dataclass(frozen=True)
class CameraScale:
    scale_id: str
    label: str
    cameras: int
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {"scale_id": self.scale_id, "label": self.label,
                "cameras": self.cameras, "basis": self.basis}


CAMERA_SCALES: tuple[CameraScale, ...] = (
    CameraScale("mvp", "競賽 MVP：CAM-01 一路（Machine A 危險區）", 1, "measured"),
    CameraScale("pilot-line", "Pilot Line：3 台機台各 1 路危險區 + 1 路產線總覽", 4, "assumption"),
    CameraScale("plant", "單廠：6 條同規格產線", 24, "assumption"),
)
DEFAULT_SCALE_ID = "pilot-line"


def _video_mbps_per_stream() -> float:
    bps = VIDEO_WIDTH * VIDEO_HEIGHT * VIDEO_FPS * VIDEO_BITS_PER_PIXEL * VIDEO_OVERHEAD_FACTOR
    return bps / 1e6


def _signal_count() -> int:
    """從實際拓撲數出感測訊號總數（M-A 4 + M-B 4 + M-C 3 = 11）。

    延遲匯入 twin：部署模組不該在載入時就把模擬器拉進來。
    """
    try:
        from ..twin.topology import build_factory

        return sum(len(m.signals) for m in build_factory().machines.values())
    except Exception:  # pragma: no cover - 只在拓撲不可用時退回已知值
        return 11


# ======================================================================================
# 頻寬預算
# ======================================================================================
def bandwidth_budget(scale_id: str = DEFAULT_SCALE_ID) -> dict[str, Any]:
    """算出上行頻寬需求，並論證「為什麼是 5G 專網而不是 Wi-Fi」。"""
    scale = next((s for s in CAMERA_SCALES if s.scale_id == scale_id), None)
    if scale is None:
        raise ValueError(f"未知的規模 {scale_id}")

    per_stream = _video_mbps_per_stream()
    video_mbps = per_stream * scale.cameras

    signals = _signal_count()
    sample_bits = (SENSOR_PAYLOAD_BYTES + SENSOR_TRANSPORT_HEADER_BYTES) * 8
    sensor_mbps = signals * SENSOR_POLL_HZ * sample_bits / 1e6

    raw_wave_mbps = ACCEL_AXES * ACCEL_SAMPLE_HZ * ACCEL_BITS_PER_SAMPLE * VIBRATION_MACHINES / 1e6
    processed_wave_mbps = VIBRATION_MACHINE_SIGNALS * SENSOR_POLL_HZ * sample_bits / 1e6

    total_mbps = video_mbps + sensor_mbps

    # 「影像送雲端判讀」vs「邊緣判讀只送事件」的月流量對比。
    video_to_cloud_bytes = video_mbps * 1e6 * SECONDS_PER_MONTH / 8
    incident_bytes = (
        MEASURED_AUDIT_BYTES_PER_INCIDENT
        + MEASURED_LLM_PROMPT_BYTES_PER_INCIDENT
        + MEASURED_LLM_COMPLETION_BYTES_PER_INCIDENT
    )
    edge_first_bytes = INCIDENTS_PER_MONTH * incident_bytes + ROLLUPS_PER_MONTH * MEASURED_KPI_ROLLUP_BYTES

    return {
        "scale": scale.to_dict(),
        "scales": [s.to_dict() for s in CAMERA_SCALES],
        "streams": [
            {
                "stream": "工安影像上行",
                "detail": f"{VIDEO_WIDTH}x{VIDEO_HEIGHT} / {VIDEO_FPS} fps / H.264 High",
                "per_unit_mbps": round(per_stream, 3),
                "units": scale.cameras,
                "mbps": round(video_mbps, 3),
                "direction": "uplink",
                "basis": "assumption",
                "formula": (
                    f"{VIDEO_WIDTH}×{VIDEO_HEIGHT}×{VIDEO_FPS} px/s × {VIDEO_BITS_PER_PIXEL} bit/px"
                    f" × {VIDEO_OVERHEAD_FACTOR} 表頭餘裕 = {per_stream:.2f} Mbps/路"
                ),
            },
            {
                "stream": "感測遙測上行（邊緣前處理後）",
                "detail": f"{signals} 個訊號 × {SENSOR_POLL_HZ:g} Hz",
                "per_unit_mbps": round(sample_bits * SENSOR_POLL_HZ / 1e6, 6),
                "units": signals,
                "mbps": round(sensor_mbps, 4),
                "direction": "uplink",
                "basis": "assumption",
                "formula": (
                    f"{signals} × {SENSOR_POLL_HZ:g} Hz ×"
                    f" ({SENSOR_PAYLOAD_BYTES}+{SENSOR_TRANSPORT_HEADER_BYTES}) B × 8"
                    f" = {sensor_mbps * 1000:.1f} kbps"
                ),
            },
        ],
        "total_uplink_mbps": round(total_mbps, 3),
        "edge_preprocessing_gain": {
            "raw_waveform_mbps": round(raw_wave_mbps, 3),
            "processed_mbps": round(processed_wave_mbps, 4),
            "reduction_factor": round(raw_wave_mbps / processed_wave_mbps, 1),
            "note": (
                f"若不在邊緣做 RMS/FFT，{VIBRATION_MACHINES} 台 CNC 的三軸 {ACCEL_SAMPLE_HZ // 1000} kHz/"
                f"{ACCEL_BITS_PER_SAMPLE}-bit 原始波形就要 {raw_wave_mbps:.2f} Mbps 持續上行；"
                f"邊緣萃取特徵後只剩 {processed_wave_mbps * 1000:.1f} kbps。"
                "這是 MEC 最直接的經濟理由，也是原始波形不出廠的資安理由。"
            ),
            "basis": "assumption",
        },
        "monthly_egress": {
            "video_to_cloud_tb": round(video_to_cloud_bytes / 1e12, 2),
            "edge_first_mb": round(edge_first_bytes / 1e6, 2),
            "reduction_factor": int(video_to_cloud_bytes / edge_first_bytes),
            "incident_bytes": incident_bytes,
            "note": (
                f"影像送雲端判讀：{video_mbps:.1f} Mbps × 30 天 = {video_to_cloud_bytes / 1e12:.2f} TB/月。"
                f"邊緣判讀後只送事件與匯總：{INCIDENTS_PER_MONTH} 次事故 × {incident_bytes / 1000:.1f} KB"
                f" + 每分鐘 {MEASURED_KPI_ROLLUP_BYTES} B 匯總 = {edge_first_bytes / 1e6:.1f} MB/月，"
                f"相差約 {int(video_to_cloud_bytes / edge_first_bytes):,} 倍。"
            ),
            "basis": "measured + assumption",
        },
        "why_not_wifi": [
            {
                "issue": "上行沒有排程保證",
                "detail": "Wi-Fi 是 CSMA/CA 競爭式存取，上行沒有 grant 機制。"
                          f"本案需要 {total_mbps:.1f} Mbps **持續**上行，一次重傳風暴就會讓工安影像掉格。"
                          "5G 專網的上行由基地台排程（SR/BSR grant），容量可規劃、可保證。",
            },
            {
                "issue": "無法對單一流做 QoS",
                "detail": "工安影像與控制遙測必須優先於廠內一般 IT 流量。"
                          "5G 可用 5QI/GBR flow 與 network slice 分離；Wi-Fi 的 WMM 只有四個粗略優先權類別，"
                          "且在共享未授權頻段裡對外部干擾無能為力。",
            },
            {
                "issue": "金屬環境與移動性",
                "detail": "工廠多路徑衰減與遮蔽嚴重；AP 間換手常見 100 ms 以上並伴隨掉包，"
                          "AGV 與行動終端會在換手瞬間失去連線。5G 的 handover 對使用者面是無縫的。",
            },
            {
                "issue": "隔離只能靠上層",
                "detail": "Wi-Fi 的 OT/IT 隔離只能做到 VLAN/SSID；5G 專網可用獨立 slice + SIM 綁定的設備身分，"
                          "把隔離下推到承載層，這正是 OT 資安要的東西。",
            },
        ],
        "assumptions": [
            f"影像規格：{VIDEO_WIDTH}×{VIDEO_HEIGHT} @ {VIDEO_FPS} fps。"
            "選 1080p 而非 720p 的理由是 PPE（安全帽、護目鏡）在遠距畫面上需要足夠像素才判讀得準。",
            f"壓縮效率取 {VIDEO_BITS_PER_PIXEL} bit/px（H.264 High、固定機位、中低動態）；"
            f"再乘 {VIDEO_OVERHEAD_FACTOR} 涵蓋 CBR 餘裕、I-frame 尖峰與封包表頭。",
            f"感測輪詢 {SENSOR_POLL_HZ:g} Hz、每筆 "
            f"{SENSOR_PAYLOAD_BYTES}+{SENSOR_TRANSPORT_HEADER_BYTES} B。"
            "MVP 的 tick 是 60 秒模擬時間，1 Hz 是導入階段的假設值。",
            f"事故頻率假設 {INCIDENTS_PER_MONTH} 次/月；此數字只影響雲端 egress 對比，不影響上行頻寬需求。",
            "實際頻譜、涵蓋與細胞容量需由中華電信做 RF 規劃確認；此處只提出需求側的數字。",
        ],
    }


# ======================================================================================
# 延遲預算
# ======================================================================================
@dataclass(frozen=True)
class LatencySegment:
    segment_id: str
    label: str
    where: str
    ms: float
    basis: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"segment_id": self.segment_id, "label": self.label, "where": self.where,
                "ms": round(self.ms, 3), "basis": self.basis, "note": self.note}


def _static_safety_chain(safety_ms: float, policy_ms: float, live: bool) -> list[LatencySegment]:
    measured_basis = "live-measured" if live else "measured"
    return [
        LatencySegment(
            "camera-sampling", "影像取樣量化", "現場 / Z-FIELD",
            1000.0 / VIDEO_FPS, "assumption",
            f"{VIDEO_FPS} fps → 最壞情況等一個影格間隔 {1000.0 / VIDEO_FPS:.1f} ms。"
            "這一段主導整條鏈路，提高幀率是縮短安全延遲最直接的手段。",
        ),
        LatencySegment(
            "encode", "H.264 低延遲編碼", "現場 / Z-FIELD",
            25.0, "vendor-typical",
            "zero-latency 設定、無 B-frame。工業攝影機規格書典型值 20–40 ms，取中值。",
        ),
        LatencySegment(
            "uplink-5g", "Camera → MEC 上行", "5G 專網",
            10.0, "vendor-typical",
            "5G SA eMBB 使用者面單向典型 10–20 ms，取下緣；URLLC 設定可再降到 1–5 ms。"
            "此值需由中華電信實際專網量測確認。",
        ),
        LatencySegment(
            "vision-inference", "CV / VLM 影像判讀", "MEC / Z-CELL",
            30.0, "assumption",
            "YOLO-class 物件偵測 + PPE 分類，邊緣 GPU 單張推論。"
            "MVP 用 SimulatedVLM，不宣稱已量測真實模型。",
        ),
        LatencySegment(
            "safety-rules", "Safety Agent 硬規則裁決", "MEC / Z-CELL",
            safety_ms, measured_basis,
            "gate_execution() 的 p95。這是本 repo 實際跑出來的數字，不是估的。",
        ),
        LatencySegment(
            "policy-engine", "Policy Engine 動作權限裁決", "MEC / Z-CELL",
            policy_ms, measured_basis,
            "evaluate_actions() 的 p95。",
        ),
        LatencySegment(
            "downlink-5g", "MEC → PLC 閘道下行", "5G 專網",
            10.0, "vendor-typical",
            "與上行同一段承載，取相同保守值。",
        ),
        LatencySegment(
            "plc-actuation", "PLC 掃描週期 + 輸出更新", "現場 / Z-FIELD",
            20.0, "vendor-typical",
            "一般工業 PLC 掃描週期 10–20 ms，取上緣。",
        ),
    ]


def measure_edge_decision_latency(
    snapshot: Any = None,
    machine_id: str = "M-A",
    iterations: int = 200,
) -> dict[str, Any]:
    """**當場實測**邊緣的兩個決策元件延遲（Safety 硬規則 + Policy 裁決）。

    這是刻意的設計：Demo 現場打開 ``/api/deployment`` 看到的延遲數字，
    是那台機器當下跑出來的，不是寫死在簡報裡的。取 p95 而非平均，
    因為安全預算要看尾端而不是平均。

    ``snapshot`` 是 Agent 可見的 :class:`FactorySnapshot`（不含 Ground Truth）。
    傳 None 時會自建一個乾淨的孿生體來量。
    """
    from ..agents.base import AgentContext
    from ..agents.safety import SafetyAgent
    from ..domain import Action, ActionKind
    from ..policy.engine import PolicyEngine

    if snapshot is None:
        from ..twin.engine import FactoryTwin

        snapshot = FactoryTwin(seed=20260809).snapshot()

    policy = PolicyEngine(require_approval=False)
    agent = SafetyAgent(AgentContext(audit=None, policy=policy))
    actions = [
        Action(ActionKind.STOP_MACHINE, machine_id, {}, "延遲量測"),
        Action(ActionKind.START_MAINTENANCE, machine_id, {}, "延遲量測"),
    ]
    kinds = [a.kind for a in actions]

    safety_samples: list[float] = []
    policy_samples: list[float] = []
    for _ in range(max(20, iterations)):
        start = time.perf_counter()
        agent.gate_execution(actions, snapshot, machine_id)
        safety_samples.append((time.perf_counter() - start) * 1000.0)
        start = time.perf_counter()
        policy.evaluate_actions(kinds)
        policy_samples.append((time.perf_counter() - start) * 1000.0)

    def p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    return {
        "iterations": len(safety_samples),
        "safety_gate_median_ms": round(statistics.median(safety_samples), 4),
        "safety_gate_p95_ms": round(p95(safety_samples), 4),
        "policy_eval_median_ms": round(statistics.median(policy_samples), 4),
        "policy_eval_p95_ms": round(p95(policy_samples), 4),
        "note": "在本次請求中即時量測，非預錄值。",
    }


def latency_budget(measured: dict[str, Any] | None = None) -> dict[str, Any]:
    """工安鏈路的端到端延遲預算。

    ``measured`` 是 :func:`measure_edge_decision_latency` 的結果；沒有就用實測常數。
    """
    live = measured is not None
    safety_ms = float(measured["safety_gate_p95_ms"]) if live else 0.0204
    policy_ms = float(measured["policy_eval_p95_ms"]) if live else 0.0047

    chain = _static_safety_chain(safety_ms, policy_ms, live)
    total = sum(seg.ms for seg in chain)
    edge_compute = sum(MEASURED_AGENT_DECISION_MS.values())

    return {
        "chain_name": "工安事件偵測 → Safety 裁決 → 阻擋不安全方案並觸發告警",
        "scope_note": (
            "本預算涵蓋的是「系統多快能知道現場有危險，並拒絕不安全的方案」。"
            "真正要停機的動作屬高風險動作，依 Policy 必須經人工核准，"
            "那一段由人決定快慢，不在也不該在延遲預算內。"
        ),
        "segments": [seg.to_dict() for seg in chain],
        "total_ms": round(total, 1),
        "target_ms": SAFETY_LATENCY_TARGET_MS,
        "within_target": total <= SAFETY_LATENCY_TARGET_MS,
        "headroom_ms": round(SAFETY_LATENCY_TARGET_MS - total, 1),
        "edge_compute": {
            "agent_decision_ms": dict(MEASURED_AGENT_DECISION_MS),
            "sum_ms": round(edge_compute, 2),
            "full_loop_wall_ms": MEASURED_LOOP_WALL_MS,
            "basis": "measured",
            "note": (
                f"六個 Agent 的決策運算合計 {edge_compute:.1f} ms，完整八階段閉環牆鐘時間 "
                f"{MEASURED_LOOP_WALL_MS} ms —— 而且是在一般筆電 CPU、單執行緒、無 GPU 的條件下。"
                "也就是說 MEC 節點的算力門檻由影像推論決定，不是由 Agent 決策決定。"
            ),
        },
        "why_not_cloud": {
            "headline": "把 Safety 放雲端，輸的不是平均延遲，是可用度與尾端延遲。",
            "points": [
                "平均延遲：台灣境內 WAN 來回約 10–20 ms，對 250 ms 的預算其實塞得下 —— "
                "所以「雲端太慢」不是誠實的論點，不要這樣講。",
                "尾端延遲：WAN 的 p99 沒有上界（壅塞、重路由、TLS 重協商）。"
                "安全功能要的是有界的最壞情況，不是漂亮的平均值。",
                "可用度：鏈路一斷，雲端側的安全判斷就是 0，不是變慢。"
                "把安全功能建在可能歸零的相依上，本身就是設計缺陷。",
                "資料主權：雲端判讀就必須把含人員影像的影格送出廠，"
                "而那正是 DATA_EGRESS_POLICY 明文禁止的第一項。",
            ],
        },
        "availability": _availability_math(),
    }


def _availability_math() -> dict[str, Any]:
    """串聯可用度：段數越多越差。這是把 Safety 留在邊緣的量化理由。"""
    edge_segments = 2      # 5G 專網 + MEC 節點
    cloud_segments = 4     # 5G 專網 + MEC + WAN + 雲端服務
    minutes_per_month = 60 * 24 * 30

    edge_av = SEGMENT_AVAILABILITY ** edge_segments
    cloud_av = SEGMENT_AVAILABILITY ** cloud_segments
    required_edge = SAFETY_AVAILABILITY_TARGET ** (1.0 / edge_segments)
    required_cloud = SAFETY_AVAILABILITY_TARGET ** (1.0 / cloud_segments)

    return {
        "segment_availability": SEGMENT_AVAILABILITY,
        "target": SAFETY_AVAILABILITY_TARGET,
        "target_downtime_min_per_month": round((1 - SAFETY_AVAILABILITY_TARGET) * minutes_per_month, 1),
        "paths": [
            {
                "path": "邊緣路徑（5G 專網 → MEC）",
                "segments": edge_segments,
                "availability_pct": round(edge_av * 100, 4),
                "downtime_min_per_month": round((1 - edge_av) * minutes_per_month, 1),
                "required_per_segment_pct": round(required_edge * 100, 4),
            },
            {
                "path": "雲端路徑（5G 專網 → MEC → WAN → 雲端服務）",
                "segments": cloud_segments,
                "availability_pct": round(cloud_av * 100, 4),
                "downtime_min_per_month": round((1 - cloud_av) * minutes_per_month, 1),
                "required_per_segment_pct": round(required_cloud * 100, 4),
            },
        ],
        "basis": "assumption",
        "note": (
            "串聯可用度是連乘，段數只會讓結果變差。要讓安全功能達到 99.9%，"
            f"兩段的邊緣路徑要求每段 ≥ {required_edge * 100:.3f}%；"
            f"四段的雲端路徑要求每段 ≥ {required_cloud * 100:.3f}%，"
            "而 WAN 那一段在實務上做不到。這就是為什麼安全裁決必須留在邊緣 —— "
            "不是因為雲端不好，而是因為串聯的段數本身就是風險。"
        ),
    }


__all__ = [
    "CAMERA_SCALES",
    "DEFAULT_SCALE_ID",
    "CameraScale",
    "LatencySegment",
    "MEASURED_AGENT_DECISION_MS",
    "MEASURED_AUDIT_BYTES_PER_INCIDENT",
    "MEASURED_KPI_ROLLUP_BYTES",
    "MEASURED_LOOP_WALL_MS",
    "SAFETY_LATENCY_TARGET_MS",
    "bandwidth_budget",
    "latency_budget",
    "measure_edge_decision_latency",
]
