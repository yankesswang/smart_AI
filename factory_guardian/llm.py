"""LLM 介接層。

規格 §9 的紅線：**LLM 不決定控制行動**。

它在這個系統裡只做三件事：
1. 把引擎算出來的證據與數字寫成人看得懂的敘述（Diagnosis narrative、Impact narrative）。
2. 協助整理跨 Agent 的說明。
3. 完全不參與 Plan 排名、Safety 裁決與動作執行。

沒有 API 金鑰時自動退回**確定性離線敘述器**：Demo 不會因為沒網路而開天窗，
而且每次輸出一樣，Benchmark 才可重現。所有 LLM 呼叫都會進稽核軌跡。

部署上這一層就是 **Edge / Cloud 的分界線**：整個閉環只有敘述生成放在 hicloud，
其餘元件全部跑在廠內 MEC。因此廠區對外鏈路一斷，受影響的也只有這裡 ——
敘述改由邊緣確定性敘述器產生（數字與證據完全相同），
偵測、診斷、安全阻擋、核准、執行與驗證一律不受影響。
鏈路狀態由 :mod:`factory_guardian.deployment.link` 管理，降級會寫進稽核軌跡。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable

from .audit import AuditLog
from .config import Settings, get_settings
from .deployment.link import DEGRADE_STAGE, is_cloud_up

#: 雲端鏈路中斷時的敘述模式名稱。刻意與 "offline-deterministic"（沒金鑰）區分，
#: 因為兩者的原因不同：一個是沒設定，一個是網路斷了。
MODE_EDGE_AUTONOMOUS = "edge-autonomous"


@dataclass
class LLMResponse:
    text: str
    mode: str            # "openai:<model>" 或 "offline-deterministic"
    latency_ms: float
    prompt_hash: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "latency_ms": round(self.latency_ms, 1),
            "prompt_hash": self.prompt_hash,
            "error": self.error,
            "chars": len(self.text),
        }


class LLMClient:
    """薄封裝：有金鑰就打 OpenAI，沒有就用離線敘述器。"""

    def __init__(self, settings: Settings | None = None, audit: AuditLog | None = None) -> None:
        self.settings = settings or get_settings()
        self.audit = audit
        self._client: Any = None

    @property
    def mode(self) -> str:
        if not is_cloud_up():
            return MODE_EDGE_AUTONOMOUS
        return f"openai:{self.settings.openai_model}" if self.settings.llm_enabled else "offline-deterministic"

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # 延遲載入：離線模式不需要它

            kwargs: dict[str, Any] = {"api_key": self.settings.openai_api_key, "timeout": self.settings.llm_timeout_s}
            if self.settings.openai_base_url:
                kwargs["base_url"] = self.settings.openai_base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def narrate(
        self,
        system: str,
        user: str,
        fallback: Callable[[], str],
        actor: str = "llm",
        max_tokens: int = 600,
    ) -> LLMResponse:
        """產生敘述文字。``fallback`` 是離線／失敗時使用的確定性敘述器。"""
        prompt_hash = hashlib.blake2b((system + "\x00" + user).encode("utf-8"), digest_size=8).hexdigest()
        start = time.perf_counter()

        # --- 雲端鏈路中斷：邊緣自主模式 -------------------------------------------------
        # 這一段刻意排在 llm_enabled 判斷之前。鏈路斷掉是網路事實，不是設定問題，
        # 所以它也蓋過 FG_ALLOW_OFFLINE_LLM=0 —— 那個旗標的用途是「別讓我不小心在
        # 沒金鑰的情況下以為自己在打真 LLM」，不是「寧可讓工廠的閉環停下來」。
        if not is_cloud_up():
            response = LLMResponse(fallback(), MODE_EDGE_AUTONOMOUS, (time.perf_counter() - start) * 1000, prompt_hash)
            self._audit_degradation(actor)
            self._audit(actor, response, user)
            return response

        if not self.settings.llm_enabled:
            if not self.settings.allow_offline_llm:
                raise RuntimeError("未設定 OPENAI_API_KEY 且 FG_ALLOW_OFFLINE_LLM=0，無法產生敘述。")
            response = LLMResponse(fallback(), "offline-deterministic", (time.perf_counter() - start) * 1000, prompt_hash)
            self._audit(actor, response, user)
            return response

        try:
            client = self._ensure_client()
            completion = client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            text = (completion.choices[0].message.content or "").strip() or fallback()
            response = LLMResponse(text, self.mode, (time.perf_counter() - start) * 1000, prompt_hash)
        except Exception as exc:  # 網路/額度/逾時：Demo 必須還是走得下去
            if not self.settings.allow_offline_llm:
                raise
            response = LLMResponse(
                fallback(),
                "offline-fallback",
                (time.perf_counter() - start) * 1000,
                prompt_hash,
                error=f"{type(exc).__name__}: {exc}",
            )
        self._audit(actor, response, user)
        return response

    def _audit(self, actor: str, response: LLMResponse, prompt: str) -> None:
        if self.audit is None:
            return
        self.audit.log(
            "llm_call",
            actor,
            **response.to_dict(),
            cloud_link="up" if is_cloud_up() else "down",
            prompt_preview=prompt[:400],
            note="LLM 僅產生敘述，不參與方案排名、安全裁決或動作執行。",
        )

    def _audit_degradation(self, actor: str) -> None:
        """把「雲端能力降級」寫成獨立的稽核事件。

        降級不能是靜悄悄發生的：事後看稽核軌跡的人必須能分辨這段敘述
        是雲端 LLM 寫的還是邊緣敘述器寫的，否則證據鏈就有一個說不清楚的洞。
        """
        if self.audit is None:
            return
        self.audit.log(
            DEGRADE_STAGE,
            "cloud-link",
            capability="llm_narrative",
            tier="cloud",
            requested_by=actor,
            fallback="edge-deterministic-narrator",
            control_loop_impact="none",
            note=(
                "廠區對外鏈路中斷，敘述改由 MEC 邊緣確定性敘述器產生（數字與證據不變）。"
                "偵測、診斷、安全裁決、核准、執行與驗證全部在邊緣完成，未受影響。"
            ),
        )


SYSTEM_PROMPT = (
    "你是智慧工廠營運平台 Factory Guardian AI 的敘述助理。"
    "你會收到規則引擎與模擬器已經算好的數字與證據。"
    "你的工作只有一件事：用精確、專業、不誇大的繁體中文，把它們寫成工程師看得懂的說明。"
    "嚴格規定：不得杜撰任何未出現在輸入中的數字或結論；"
    "不得做出控制決策或建議覆寫安全規則；不得改變已給定的方案排名。"
)


__all__ = ["LLMClient", "LLMResponse", "MODE_EDGE_AUTONOMOUS", "SYSTEM_PROMPT"]
