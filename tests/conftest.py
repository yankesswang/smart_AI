"""測試共用設定。

所有測試都跑在**離線模式**（不打 LLM API）與臨時稽核目錄，
確保 CI 不需要金鑰、不會寫進專案的 runs/。
"""

from __future__ import annotations

import os

import pytest

# 必須在匯入 factory_guardian 之前設定，config 在載入時就會讀環境變數。
# FG_NO_DOTENV 是關鍵：少了它，config 的 load_dotenv() 會把專案 .env 裡的
# OPENAI_API_KEY 塞回環境，測試就會真的去打 API —— 又慢又要花錢，而且結果不可重現。
os.environ["FG_NO_DOTENV"] = "1"
os.environ["OPENAI_API_KEY"] = ""
os.environ["FG_ALLOW_OFFLINE_LLM"] = "1"
os.environ["FG_REQUIRE_APPROVAL"] = "0"

from factory_guardian.agents.base import AgentContext  # noqa: E402
from factory_guardian.audit import NullAuditLog  # noqa: E402
from factory_guardian.config import Settings, get_settings  # noqa: E402
from factory_guardian.policy.engine import PolicyEngine  # noqa: E402
from factory_guardian.twin.engine import FactoryTwin  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("FG_AUDIT_DIR", str(tmp_path / "runs"))
    get_settings(refresh=True)
    yield
    get_settings(refresh=True)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(audit_dir=tmp_path / "runs", require_approval=False, openai_api_key=None)


@pytest.fixture
def ctx(settings) -> AgentContext:
    return AgentContext(settings=settings, audit=NullAuditLog(), policy=PolicyEngine(require_approval=False))


@pytest.fixture
def twin() -> FactoryTwin:
    return FactoryTwin(seed=20260809, tick_minutes=1.0)
