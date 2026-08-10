"""執行期設定。所有設定都可以用環境變數覆寫，讓 Demo 與測試都能重現。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 載入 .env（python-dotenv 是選用的；沒有也不影響離線 Demo）。
# FG_NO_DOTENV=1 可以完全停用，測試靠它確保不會意外撿到本機的 API 金鑰而去打網路。
if os.getenv("FG_NO_DOTENV", "").strip().lower() not in {"1", "true", "yes", "on"}:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # pragma: no cover - 只在缺套件時發生
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Factory Guardian 的全域設定。"""

    # --- LLM ---
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    openai_base_url: str | None = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL") or None)
    # 沒有金鑰時自動退回離線敘述器；設 0 可強制要求真實 LLM。
    allow_offline_llm: bool = field(default_factory=lambda: _env_flag("FG_ALLOW_OFFLINE_LLM", True))
    llm_timeout_s: float = field(default_factory=lambda: _env_float("FG_LLM_TIMEOUT_S", 30.0))

    # --- 稽核 ---
    audit_dir: Path = field(
        default_factory=lambda: Path(os.getenv("FG_AUDIT_DIR", str(REPO_ROOT / "runs"))).expanduser()
    )

    # --- 治理 ---
    # 高風險動作（停機 / 改變控制狀態）預設需要人工核准。
    require_approval: bool = field(default_factory=lambda: _env_flag("FG_REQUIRE_APPROVAL", True))

    # --- 模擬 ---
    seed: int = field(default_factory=lambda: int(os.getenv("FG_SEED", "20260809")))
    tick_seconds: float = field(default_factory=lambda: _env_float("FG_TICK_SECONDS", 60.0))

    @property
    def llm_enabled(self) -> bool:
        """是否要真的打 LLM API。"""
        return bool(self.openai_api_key)

    def describe(self) -> dict[str, object]:
        return {
            "llm_mode": "openai:" + self.openai_model if self.llm_enabled else "offline-deterministic",
            "allow_offline_llm": self.allow_offline_llm,
            "require_approval": self.require_approval,
            "audit_dir": str(self.audit_dir),
            "seed": self.seed,
            "tick_seconds": self.tick_seconds,
        }


_SETTINGS: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """取得全域設定（預設會快取；測試可用 refresh=True 重新讀環境變數）。"""
    global _SETTINGS
    if _SETTINGS is None or refresh:
        _SETTINGS = Settings()
    return _SETTINGS
