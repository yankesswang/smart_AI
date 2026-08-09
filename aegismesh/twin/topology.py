"""智慧醫院園區拓樸（MVP：12 節點 / 13 鏈路 / 6 業務 / 3 條 WAN 路徑）。

刻意壓在 12 節點以內，符合提案的 MVP 範圍限制；但每條鏈路的頻寬、延遲、
損失率與成本都取自可辯護的實務量級，讓孿生推演結果具備可信度。
"""

from __future__ import annotations

from ..domain import Link, LinkKind, Node, NodeKind, Service, ServiceSLO


def build_nodes() -> list[Node]:
    return [
        Node("ward-ed", "急診暨檢傷區", NodeKind.WARD),
        Node("ward-icu", "加護病房（生理監測 IoT）", NodeKind.WARD),
        Node("blk-img", "影像醫學暨遠距診療大樓", NodeKind.WARD),
        Node("blk-adm", "行政大樓暨訪客區", NodeKind.WARD),
        Node("core-sw", "院內核心交換機", NodeKind.CORE),
        Node("cpe-fiber", "固網 CPE（主要）", NodeKind.CPE),
        Node("cpe-5g", "5G CPE（備援）", NodeKind.CPE),
        Node("sat-vsat", "衛星終端 VSAT（緊急）", NodeKind.VSAT),
        Node("pop-fiber", "中華電信固網 POP", NodeKind.POP, site="cht"),
        Node("gnb-5g", "5G 基地台 gNB-01", NodeKind.GNB, site="cht"),
        Node("gw-sat", "海地星空衛星閘道", NodeKind.SATGW, site="cht"),
        Node("dc-hicloud", "HiCloud 醫療雲資料中心", NodeKind.DC, site="cht"),
    ]


def build_links() -> list[Link]:
    """cost_per_gb 單位為新台幣元/GB，反映固網 << 5G << 衛星的實際價差。"""
    return [
        # 院內 LAN
        Link("l-ed", "ward-ed", "core-sw", LinkKind.LAN, 10_000, 0.20, 0.001, 0.0),
        Link("l-icu", "ward-icu", "core-sw", LinkKind.LAN, 10_000, 0.20, 0.001, 0.0),
        Link("l-img", "blk-img", "core-sw", LinkKind.LAN, 10_000, 0.25, 0.001, 0.0),
        Link("l-adm", "blk-adm", "core-sw", LinkKind.LAN, 10_000, 0.25, 0.001, 0.0),
        # 核心 → 三種接取設備
        Link("l-core-fiber", "core-sw", "cpe-fiber", LinkKind.LAN, 10_000, 0.20, 0.001, 0.0),
        Link("l-core-5g", "core-sw", "cpe-5g", LinkKind.LAN, 2_000, 0.30, 0.002, 0.0),
        Link("l-core-sat", "core-sw", "sat-vsat", LinkKind.LAN, 500, 0.50, 0.002, 0.0),
        # WAN 三路：固網專線 / 5G / 衛星
        Link("w-fiber", "cpe-fiber", "pop-fiber", LinkKind.FIBER, 1_000, 3.0, 0.005, 0.08),
        Link("w-5g", "cpe-5g", "gnb-5g", LinkKind.MOBILE_5G, 600, 14.0, 0.05, 0.55),
        Link("w-sat", "sat-vsat", "gw-sat", LinkKind.SATELLITE, 120, 48.0, 0.30, 4.20),
        # 電信骨幹 → 醫療雲
        Link("b-fiber", "pop-fiber", "dc-hicloud", LinkKind.BACKBONE, 10_000, 4.0, 0.002, 0.01),
        Link("b-5g", "gnb-5g", "dc-hicloud", LinkKind.BACKBONE, 5_000, 6.0, 0.005, 0.01),
        Link("b-sat", "gw-sat", "dc-hicloud", LinkKind.BACKBONE, 1_000, 12.0, 0.010, 0.01),
    ]


def build_services() -> list[Service]:
    """5 個關鍵應用 + 1 個非關鍵（訪客 Wi-Fi），用來展示優先級搶占。"""
    return [
        Service(
            "svc-ed-vitals", "急診生命徵象即時串流", "ward-ed", "dc-hicloud", priority=0,
            slo=ServiceSLO(max_latency_ms=120, max_loss_pct=1.0, min_bandwidth_mbps=8, required_bandwidth_mbps=25),
            clinical_note="檢傷分級與急救決策依據，中斷即影響病人安全",
        ),
        Service(
            "svc-icu-iot", "ICU 生理監測 IoT 遙測", "ward-icu", "dc-hicloud", priority=0,
            slo=ServiceSLO(max_latency_ms=150, max_loss_pct=1.0, min_bandwidth_mbps=5, required_bandwidth_mbps=15),
            clinical_note="連續生命徵象告警，斷線期間無法觸發中央監視警報",
        ),
        Service(
            "svc-teleconsult", "遠距診療視訊會診", "blk-img", "dc-hicloud", priority=1,
            slo=ServiceSLO(max_latency_ms=200, max_loss_pct=1.5, min_bandwidth_mbps=12, required_bandwidth_mbps=60),
            clinical_note="可降級為關鍵影格＋語音，仍可維持會診",
        ),
        Service(
            "svc-pacs", "醫療影像 PACS 同步", "blk-img", "dc-hicloud", priority=2,
            slo=ServiceSLO(max_latency_ms=800, max_loss_pct=2.0, min_bandwidth_mbps=20, required_bandwidth_mbps=300),
            clinical_note="可延後傳輸並於網路恢復後續傳",
        ),
        Service(
            "svc-his", "HIS 行政與批價系統", "blk-adm", "dc-hicloud", priority=3,
            slo=ServiceSLO(max_latency_ms=400, max_loss_pct=2.0, min_bandwidth_mbps=10, required_bandwidth_mbps=80),
            clinical_note="非即時，可容忍短暫降速",
        ),
        Service(
            "svc-guest", "訪客 Wi-Fi", "blk-adm", "dc-hicloud", priority=4,
            slo=ServiceSLO(max_latency_ms=800, max_loss_pct=5.0, min_bandwidth_mbps=5, required_bandwidth_mbps=200),
            clinical_note="災害期間可完全停用以釋出頻寬",
        ),
    ]
