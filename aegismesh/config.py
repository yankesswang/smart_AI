"""執行期設定：從 .env 讀取，全平台單一入口。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# override=False：真實環境變數優先於 .env，方便 CI 覆寫
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_model: str
    openai_base_url: str | None
    allow_offline_llm: bool
    audit_dir: Path
    require_approval: bool

    @property
    def llm_available(self) -> bool:
        return bool(self.openai_api_key)


def load_settings() -> Settings:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    base_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
    audit_dir = Path(os.getenv("AEGIS_AUDIT_DIR") or "runs")
    if not audit_dir.is_absolute():
        audit_dir = PROJECT_ROOT / audit_dir
    return Settings(
        openai_api_key=key or None,
        openai_model=(os.getenv("OPENAI_MODEL") or "gpt-4o").strip(),
        openai_base_url=base_url or None,
        allow_offline_llm=_flag("AEGIS_ALLOW_OFFLINE_LLM", True),
        audit_dir=audit_dir,
        require_approval=_flag("AEGIS_REQUIRE_APPROVAL", True),
    )


SETTINGS = load_settings()
