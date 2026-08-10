"""VLM / CV 後端（規格 §4.4、§11 P1）。

MVP 用 ``SimulatedVLM``：直接讀 Digital Twin 產生的 Camera 觀測。
真實導入時把後端換成 YOLO / VLM 推論即可 —— ``analyze()`` 介面不變，
Safety Agent 與 Policy Engine 完全不用動。

``OpenAIVLM`` 是給有真實影像時使用的骨架：接收影格（base64）並要求模型回傳
與 ``CameraObservation`` 相同欄位的結構化判讀。競賽 Demo 預設不啟用。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from ..domain import CameraObservation, FactorySnapshot

VLM_SCHEMA_PROMPT = (
    "你是工廠安全影像判讀模型。請只輸出 JSON，欄位如下："
    '{"person_count": int, "person_in_hazard_zone": bool, "ppe_compliant": bool, '
    '"fall_detected": bool, "smoke_detected": bool, "confidence": float, "caption": "繁體中文一句話"}。'
    "hazard zone 指黃線標示的運轉設備危險區；PPE 指安全帽、防護眼鏡與安全鞋。"
)


class VisionBackend(Protocol):
    """安全影像判讀後端。"""

    name: str

    def analyze(self, snapshot: FactorySnapshot) -> list[CameraObservation]:
        ...


class SimulatedVLM:
    """模擬後端：Digital Twin 已經依情境產生 Camera 觀測，這裡直接採用。

    這是誠實的做法 —— 競賽 Demo 不宣稱跑了真實影像模型，
    但整條 Safety 決策鏈（偵測 → 規則 → BLOCK → 稽核）是真的在跑。
    """

    name = "simulated-vlm"

    def analyze(self, snapshot: FactorySnapshot) -> list[CameraObservation]:
        return list(snapshot.cameras)


class OpenAIVLM:
    """真實 VLM 後端骨架（需要影像輸入時使用）。"""

    name = "openai-vlm"

    def __init__(self, llm_client: Any, frame_provider: Any) -> None:
        self.llm = llm_client
        self.frame_provider = frame_provider

    def analyze(self, snapshot: FactorySnapshot) -> list[CameraObservation]:
        results: list[CameraObservation] = []
        for cam in snapshot.cameras:
            frame_b64 = self.frame_provider(cam.camera_id, snapshot.tick)
            if frame_b64 is None:
                results.append(cam)   # 沒有影格就退回模擬觀測
                continue
            raw = self.llm.vision_json(VLM_SCHEMA_PROMPT, frame_b64)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                results.append(cam)
                continue
            results.append(
                CameraObservation(
                    camera_id=cam.camera_id,
                    zone_id=cam.zone_id,
                    machine_id=cam.machine_id,
                    person_count=int(parsed.get("person_count", 0)),
                    person_in_hazard_zone=bool(parsed.get("person_in_hazard_zone", False)),
                    ppe_compliant=bool(parsed.get("ppe_compliant", True)),
                    fall_detected=bool(parsed.get("fall_detected", False)),
                    smoke_detected=bool(parsed.get("smoke_detected", False)),
                    confidence=float(parsed.get("confidence", 0.5)),
                    caption=str(parsed.get("caption", "")),
                )
            )
        return results


__all__ = ["VisionBackend", "SimulatedVLM", "OpenAIVLM", "VLM_SCHEMA_PROMPT"]
