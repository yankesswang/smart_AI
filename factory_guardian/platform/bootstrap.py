"""平台啟動流程：建庫、建預設帳號、註冊站點。

把「第一次啟動要做什麼」集中在這裡，讓 API 層只負責路由。
所有動作都具備冪等性：重複啟動不會產生重複資料。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, Settings, get_settings
from .auth import AuthService, ensure_default_admin
from .fleet import FleetManager
from .services import AlarmService, AnalyticsService, AuditService, HandoverService, WorkOrderService
from .store import Store

DEFAULT_DB_PATH = REPO_ROOT / "runs" / "platform.db"

# 預設站點：讓平台一啟動就是「多站台」形態，而不是空殼。
# 每個站點不同 seed 與情境，才會呈現各自獨立的營運處境。
DEFAULT_SITES: tuple[dict[str, Any], ...] = (
    {
        "site_id": "TC-01",
        "name": "台中一廠",
        "region": "中區",
        "adapter_config": {"seed": 20260809, "scenario_id": "bearing-degradation"},
    },
    {
        "site_id": "TC-02",
        "name": "台中二廠",
        "region": "中區",
        "adapter_config": {"seed": 771102, "scenario_id": "cooling-failure"},
    },
    {
        "site_id": "LK-01",
        "name": "林口一廠",
        "region": "北區",
        "adapter_config": {"seed": 480915, "scenario_id": None},
    },
    {
        "site_id": "KH-01",
        "name": "高雄一廠",
        "region": "南區",
        "adapter_config": {"seed": 330621, "scenario_id": "bearing-with-intrusion"},
    },
)

# 預設帳號：對應四個角色，讓權限差異在 Demo 與驗收時看得出來。
DEFAULT_USERS: tuple[tuple[str, str, str, str], ...] = (
    ("admin", "admin12345", "系統管理員", "admin"),
    ("engineer", "engineer123", "王工程師", "engineer"),
    ("operator", "operator123", "陳操作員", "operator"),
    ("viewer", "viewer12345", "李廠長", "viewer"),
)


@dataclass
class Platform:
    """平台的所有元件。API 層透過它取用各項服務。"""

    store: Store
    fleet: FleetManager
    auth: AuthService
    alarms: AlarmService
    work_orders: WorkOrderService
    handovers: HandoverService
    analytics: AnalyticsService
    audit: AuditService
    settings: Settings

    def shutdown(self) -> None:
        self.fleet.stop_all()
        self.store.close()


def build_platform(
    db_path: Path | str | None = None,
    settings: Settings | None = None,
    *,
    seed_sites: bool = True,
    seed_users: bool = True,
    autostart: bool = True,
) -> Platform:
    settings = settings or get_settings()
    path = Path(db_path or os.getenv("FG_PLATFORM_DB", str(DEFAULT_DB_PATH)))
    store = Store(path)

    auth = AuthService(store)
    fleet = FleetManager(store, settings=settings)

    if seed_users:
        _seed_users(auth)
    if seed_sites:
        _seed_sites(fleet)
    if autostart:
        fleet.start_all()

    return Platform(
        store=store,
        fleet=fleet,
        auth=auth,
        alarms=AlarmService(store),
        work_orders=WorkOrderService(store),
        handovers=HandoverService(store),
        analytics=AnalyticsService(store),
        audit=AuditService(store),
        settings=settings,
    )


def _seed_users(auth: AuthService) -> None:
    """建立預設帳號。已存在的帳號不會被覆寫（不會重設現場改過的密碼）。"""
    created_admin = ensure_default_admin(
        auth,
        os.getenv("FG_ADMIN_USER", "admin"),
        os.getenv("FG_ADMIN_PASSWORD", "admin12345"),
    )
    # 只有在系統剛初始化（連 admin 都是這次建的）時才補其他示範帳號，
    # 避免在已上線的系統裡莫名其妙冒出帳號。
    if not created_admin:
        return
    for username, password, display_name, role in DEFAULT_USERS:
        if auth.get_user(username) is None:
            auth.create_user(username, password, display_name, role)


def _seed_sites(fleet: FleetManager) -> None:
    if fleet.site_records():
        return
    for site in DEFAULT_SITES:
        fleet.register_site(
            site["site_id"],
            site["name"],
            region=site["region"],
            adapter_config=site["adapter_config"],
        )


__all__ = ["DEFAULT_SITES", "DEFAULT_USERS", "Platform", "build_platform"]
