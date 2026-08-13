"""Factory Guardian 中央管理平台層。

這一層把「單一 Demo Session」升級成正式產品形態的營運平台：

- ``store``      —— SQLite 持久化（站點、告警、工單、使用者、稽核）
- ``adapters``   —— 資料來源抽象；模擬器只是其中一種實作
- ``fleet``      —— 多站點註冊表與背景執行緒管理
- ``services``   —— 告警生命週期、工單、班別交接等營運邏輯
- ``auth``       —— 帳號、角色與權限控管

原本的 ``api.session.DemoSession`` 保持不動，繼續服務展示用頁面；
平台層在它之上建立多站點、可持久化、可稽核的正式維運介面。
"""

from __future__ import annotations

__all__ = [
    "adapters",
    "auth",
    "fleet",
    "services",
    "store",
]
