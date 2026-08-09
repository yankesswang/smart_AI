"""OpenAI 串接層。

設計上有兩個硬性約束：
1. LLM 只做「理解、解釋、排序」，不產生任何會被直接執行的數值 ——
   路徑、頻寬、SLA 全部來自孿生引擎與最佳化器。
2. 離線可退化。評審現場沒網路、金鑰失效、或 API 逾時時，整套閉環仍要跑得完，
   只是少了自然語言敘述。Demo 不能因為外部相依而開天窗。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .config import SETTINGS, Settings

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 40.0
_MAX_RETRIES = 2


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    """OpenAI Chat Completions 的薄封裝，強制 JSON 輸出。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or SETTINGS
        self._client = None
        self.last_error: str | None = None
        self.calls = 0

        if self.settings.llm_available:
            try:
                from openai import OpenAI

                kwargs: dict[str, Any] = {
                    "api_key": self.settings.openai_api_key,
                    "timeout": _TIMEOUT_SEC,
                    "max_retries": _MAX_RETRIES,
                }
                if self.settings.openai_base_url:
                    kwargs["base_url"] = self.settings.openai_base_url
                self._client = OpenAI(**kwargs)
            except Exception as exc:  # pragma: no cover - 建構失敗屬環境問題
                self.last_error = f"OpenAI 用戶端建立失敗：{exc}"
                logger.warning(self.last_error)

    @property
    def online(self) -> bool:
        return self._client is not None

    @property
    def mode(self) -> str:
        if self.online:
            return f"OpenAI · {self.settings.openai_model}"
        return "離線決定性推理（未設定 OPENAI_API_KEY）"

    def complete_json(
        self,
        system: str,
        user: str,
        fallback: Callable[[], dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 900,
    ) -> dict[str, Any]:
        """要求 LLM 回傳 JSON 物件；任何失敗都退回 fallback()，絕不讓閉環中斷。"""
        if not self.online:
            return self._fallback(fallback, reason="offline")

        try:
            self.calls += 1
            resp = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = (resp.choices[0].message.content or "").strip()
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("模型未回傳 JSON 物件")
            data["_llm"] = self.settings.openai_model
            return data
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("LLM 呼叫失敗，改用決定性推理：%s", self.last_error)
            if not self.settings.allow_offline_llm:
                raise LLMUnavailable(self.last_error) from exc
            return self._fallback(fallback, reason=self.last_error)

    @staticmethod
    def _fallback(fallback: Callable[[], dict[str, Any]], reason: str) -> dict[str, Any]:
        data = fallback()
        data["_llm"] = "offline"
        data["_llm_reason"] = reason
        return data


def compact_json(obj: Any, limit: int = 6000) -> str:
    """把孿生快照壓成給 LLM 讀的緊湊 JSON，避免無意義的 token 花費。"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "…(truncated)"
