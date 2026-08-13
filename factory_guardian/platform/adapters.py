"""資料來源介面（Adapter 層）。

正式導入時，平台上層不應該知道資料是從哪裡來的 —— OPC-UA、MQTT、
Modbus TCP 或 REST，對「告警清單」與「工單」而言沒有差別。這一層把
差異收斂到一個介面：

    class DataSourceAdapter:
        def start() / stop()
        def poll() -> FactorySnapshot     # 取得當前現場狀態
        def apply(action) -> ActionEffect # 下行控制
        def descriptor() -> dict          # 站點能力與連線資訊

``SimulatedAdapter`` 是目前唯一的完整實作，它包住既有的 ``FactoryTwin``。
真實站點只要再寫一個實作，上層（fleet / services / API / 前端）完全不用改。

站點會標記 ``simulated: true``，讓正式介面上永遠看得出哪些數字是模擬的 ——
這是原本 Demo 就守住的可信度原則，平台層繼續沿用。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any

from ..config import Settings, get_settings
from ..domain import Action, FactorySnapshot
from ..twin.engine import ActionEffect, FactoryTwin
from ..twin.scenarios import get_scenario


class AdapterError(Exception):
    """資料源連線或控制失敗。"""


class DataSourceAdapter(ABC):
    """一個站點的資料來源。

    實作必須是執行緒安全的：平台會從輪詢執行緒呼叫 ``poll()``，
    同時可能有 API 執行緒呼叫 ``apply()``。
    """

    kind: str = "abstract"
    #: 這個資料源的數值是否為模擬產生。正式介面會據此顯示標記。
    simulated: bool = True

    def __init__(self, site_id: str, config: dict[str, Any] | None = None) -> None:
        self.site_id = site_id
        self.config = dict(config or {})
        self._connected = False

    # ------------------------------------------------------------------ 生命週期
    @abstractmethod
    def start(self) -> None:
        """建立連線 / 初始化。必須具備冪等性。"""

    @abstractmethod
    def stop(self) -> None:
        """關閉連線並釋放資源。"""

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 上行
    @abstractmethod
    def poll(self) -> FactorySnapshot:
        """取得當前現場狀態。真實 adapter 會在這裡讀點位並組成 snapshot。"""

    # ------------------------------------------------------------------ 下行
    @abstractmethod
    def apply(self, action: Action) -> ActionEffect:
        """執行一個控制動作（停機、降速、轉單…）。"""

    # ------------------------------------------------------------------ 描述
    def descriptor(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "simulated": self.simulated,
            "connected": self.connected,
            "config": self.public_config(),
        }

    def public_config(self) -> dict[str, Any]:
        """可安全外露的設定。實作要在這裡濾掉帳密等機敏欄位。"""
        return dict(self.config)


# --------------------------------------------------------------------------------------
# 模擬資料源
# --------------------------------------------------------------------------------------
class SimulatedAdapter(DataSourceAdapter):
    """以 ``FactoryTwin`` 為後端的資料源。

    正式平台把它當成「一個會回報數字的站點」，跟真實 PLC 站點同一個介面。
    模擬器本身還是唯一知道 Ground Truth 的地方，而 ``poll()`` 只回傳
    ``snapshot()`` —— 不包含故障標籤，這點與原本的設計一致。
    """

    kind = "simulated"
    simulated = True

    def __init__(
        self,
        site_id: str,
        config: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(site_id, config)
        self.settings = settings or get_settings()
        self._lock = threading.RLock()
        # 每個站點用不同 seed，否則多站台會演出一模一樣的劇本，
        # 一眼就看得出是同一個模擬器複製出來的。
        self.seed = int(self.config.get("seed", self.settings.seed))
        self.twin = FactoryTwin(
            seed=self.seed,
            tick_minutes=self.settings.tick_seconds / 60.0,
        )

    # ------------------------------------------------------------------ 生命週期
    def start(self) -> None:
        with self._lock:
            if self._connected:
                return
            self._connected = True
            # 站點可以帶預設情境，讓多站台各自有不同的營運處境。
            scenario_id = self.config.get("scenario_id")
            if scenario_id:
                self.twin.schedule(get_scenario(scenario_id).injections)

    def stop(self) -> None:
        with self._lock:
            self._connected = False

    # ------------------------------------------------------------------ 上行
    def poll(self) -> FactorySnapshot:
        with self._lock:
            return self.twin.snapshot()

    def step(self) -> FactorySnapshot:
        """推進模擬時間一格。只有模擬資料源有這個概念。"""
        with self._lock:
            return self.twin.step()

    # ------------------------------------------------------------------ 下行
    def apply(self, action: Action) -> ActionEffect:
        with self._lock:
            return self.twin.apply(action)

    # ------------------------------------------------------------------ 描述
    def public_config(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "scenario_id": self.config.get("scenario_id"),
            "tick_seconds": self.settings.tick_seconds,
        }


# --------------------------------------------------------------------------------------
# 真實協定 adapter 的骨架
# --------------------------------------------------------------------------------------
class OpcUaAdapter(DataSourceAdapter):
    """OPC-UA 站點的接點。

    這裡刻意保留為未實作的骨架：真正的實作需要現場的 endpoint、
    點位表（NodeId ↔ 訊號名稱）與憑證，那些資訊只有導入時才拿得到。
    介面已經固定，補上實作後平台其餘部分不用改動。

    需要的設定：
        endpoint      opc.tcp://host:4840
        security      None / Basic256Sha256
        node_map      {machine_id: {signal_name: node_id}}
        credentials   使用者名稱與憑證路徑
    """

    kind = "opcua"
    simulated = False

    def start(self) -> None:
        raise AdapterError(
            "OPC-UA adapter 尚未實作。需要現場 endpoint、點位表與憑證才能完成連線。"
        )

    def stop(self) -> None:
        self._connected = False

    def poll(self) -> FactorySnapshot:
        raise AdapterError("OPC-UA adapter 尚未實作。")

    def apply(self, action: Action) -> ActionEffect:
        raise AdapterError("OPC-UA adapter 尚未實作。")


class MqttAdapter(DataSourceAdapter):
    """MQTT / Sparkplug B 站點的接點（骨架，同 OpcUaAdapter）。

    需要的設定：
        broker_url    mqtt://host:1883
        topic_prefix  spBv1.0/<group>/DDATA/<node>
        qos / tls     連線品質與加密設定
        credentials   帳密或憑證
    """

    kind = "mqtt"
    simulated = False

    def start(self) -> None:
        raise AdapterError(
            "MQTT adapter 尚未實作。需要 broker 位址、topic 結構與認證資訊才能完成連線。"
        )

    def stop(self) -> None:
        self._connected = False

    def poll(self) -> FactorySnapshot:
        raise AdapterError("MQTT adapter 尚未實作。")

    def apply(self, action: Action) -> ActionEffect:
        raise AdapterError("MQTT adapter 尚未實作。")


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------
ADAPTER_KINDS: dict[str, type[DataSourceAdapter]] = {
    "simulated": SimulatedAdapter,
    "opcua": OpcUaAdapter,
    "mqtt": MqttAdapter,
}

ADAPTER_LABELS = {
    "simulated": "模擬資料源（Digital Twin）",
    "opcua": "OPC-UA",
    "mqtt": "MQTT / Sparkplug B",
}


def build_adapter(
    kind: str,
    site_id: str,
    config: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> DataSourceAdapter:
    cls = ADAPTER_KINDS.get(kind)
    if cls is None:
        raise AdapterError(f"未知的資料源型別 {kind}；可用：{', '.join(ADAPTER_KINDS)}")
    if cls is SimulatedAdapter:
        return SimulatedAdapter(site_id, config, settings=settings)
    return cls(site_id, config)


def describe_adapter_kinds() -> list[dict[str, Any]]:
    return [
        {
            "kind": kind,
            "label": ADAPTER_LABELS.get(kind, kind),
            "simulated": cls.simulated,
            "implemented": kind == "simulated",
        }
        for kind, cls in ADAPTER_KINDS.items()
    ]


__all__ = [
    "ADAPTER_KINDS",
    "ADAPTER_LABELS",
    "AdapterError",
    "DataSourceAdapter",
    "MqttAdapter",
    "OpcUaAdapter",
    "SimulatedAdapter",
    "build_adapter",
    "describe_adapter_kinds",
]
