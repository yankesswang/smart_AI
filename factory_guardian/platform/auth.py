"""帳號、角色與權限。

正式維運平台不能讓「誰都能停機」。這一層提供：

- 使用者與密碼（PBKDF2-HMAC-SHA256，標準函式庫，無外部相依）
- 四個角色與一張權限表
- server-side session token（可立即撤銷）

角色設計對應真實工廠的職務分工：

| 角色       | 典型職務       | 能做什麼                                   |
|-----------|---------------|-------------------------------------------|
| viewer    | 主管 / 稽核    | 只讀。看得到所有畫面，按不動任何東西          |
| operator  | 線上操作員     | 確認告警、認領與完成工單、寫班別交接          |
| engineer  | 設備工程師     | operator + 核准 Agent 方案、控制站點模擬進程 |
| admin     | 系統管理員     | engineer + 使用者管理、站點註冊             |
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store, utcnow

# PBKDF2 疊代次數。OWASP 對 PBKDF2-HMAC-SHA256 的建議下限量級。
_PBKDF2_ITERATIONS = 210_000
_TOKEN_TTL_HOURS = 12


# --------------------------------------------------------------------------------------
# 角色與權限
# --------------------------------------------------------------------------------------
ROLES = ("viewer", "operator", "engineer", "admin")

# 權限字串採 "資源:動作"，讓路由端的檢查一眼看得懂意圖。
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "viewer": (
        "fleet:read",
        "site:read",
        "alarm:read",
        "workorder:read",
        "audit:read",
        "analytics:read",
    ),
    "operator": (
        "fleet:read",
        "site:read",
        "alarm:read",
        "alarm:ack",
        "alarm:resolve",
        "workorder:read",
        "workorder:create",
        "workorder:update",
        "audit:read",
        "analytics:read",
        "handover:write",
    ),
    "engineer": (
        "fleet:read",
        "site:read",
        "site:control",
        "alarm:read",
        "alarm:ack",
        "alarm:resolve",
        "workorder:read",
        "workorder:create",
        "workorder:update",
        "approval:decide",
        "audit:read",
        "analytics:read",
        "handover:write",
    ),
    "admin": (
        "fleet:read",
        "site:read",
        "site:control",
        "site:manage",
        "alarm:read",
        "alarm:ack",
        "alarm:resolve",
        "workorder:read",
        "workorder:create",
        "workorder:update",
        "approval:decide",
        "audit:read",
        "analytics:read",
        "handover:write",
        "user:manage",
    ),
}

ROLE_LABELS = {
    "viewer": "檢視者",
    "operator": "現場操作員",
    "engineer": "設備工程師",
    "admin": "系統管理員",
}


def has_permission(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, ())


@dataclass(frozen=True)
class Principal:
    """通過驗證的呼叫者。路由拿它做權限判斷與稽核記名。"""

    username: str
    display_name: str
    role: str

    def can(self, permission: str) -> bool:
        return has_permission(self.role, permission)

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "permissions": list(PERMISSIONS.get(self.role, ())),
        }


# --------------------------------------------------------------------------------------
# 密碼雜湊
# --------------------------------------------------------------------------------------
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """回傳 (hash_hex, salt_hex)。"""
    salt_hex = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS
    )
    return digest.hex(), salt_hex


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    # 定時比較，避免以回應時間反推雜湊。
    return hmac.compare_digest(candidate, password_hash)


# --------------------------------------------------------------------------------------
# AuthService
# --------------------------------------------------------------------------------------
class AuthError(Exception):
    """驗證或授權失敗。由 API 層轉成 401/403。"""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class AuthService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ 使用者
    def create_user(
        self,
        username: str,
        password: str,
        display_name: str,
        role: str,
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if role not in ROLES:
            raise AuthError(f"未知角色 {role}", status=400)
        if len(password) < 8:
            raise AuthError("密碼至少需要 8 個字元。", status=400)
        password_hash, salt = hash_password(password)
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT username FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                raise AuthError(f"帳號 {username} 已存在。", status=409)
            conn.execute(
                """
                INSERT INTO users (username, display_name, role, password_hash,
                                   password_salt, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (username, display_name, role, password_hash, salt, int(enabled), utcnow()),
            )
        return self.get_user(username) or {}

    def get_user(self, username: str) -> dict[str, Any] | None:
        row = self.store.query_one(
            """
            SELECT username, display_name, role, enabled, created_at, last_login_at
            FROM users WHERE username = ?
            """,
            (username,),
        )
        return row

    def list_users(self) -> list[dict[str, Any]]:
        return self.store.query(
            """
            SELECT username, display_name, role, enabled, created_at, last_login_at
            FROM users ORDER BY username
            """
        )

    def set_enabled(self, username: str, enabled: bool) -> None:
        with self.store.write() as conn:
            conn.execute("UPDATE users SET enabled = ? WHERE username = ?", (int(enabled), username))
            if not enabled:
                # 停用即刻生效：把該帳號的所有 token 撤掉。
                conn.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise AuthError(f"未知角色 {role}", status=400)
        with self.store.write() as conn:
            conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 8:
            raise AuthError("密碼至少需要 8 個字元。", status=400)
        password_hash, salt = hash_password(password)
        with self.store.write() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_salt = ? WHERE username = ?",
                (password_hash, salt, username),
            )
            # 換密碼同樣撤銷既有登入。
            conn.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))

    def delete_user(self, username: str) -> None:
        with self.store.write() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))
            conn.execute("DELETE FROM users WHERE username = ?", (username,))

    # ------------------------------------------------------------------ 登入
    def login(self, username: str, password: str) -> tuple[str, Principal]:
        row = self.store.query_one(
            """
            SELECT username, display_name, role, password_hash, password_salt, enabled
            FROM users WHERE username = ?
            """,
            (username,),
        )
        # 帳號不存在時也走一次雜湊，讓「帳號不存在」與「密碼錯誤」的耗時相近。
        if row is None:
            hash_password(password)
            raise AuthError("帳號或密碼錯誤。")
        if not row["enabled"]:
            raise AuthError("此帳號已停用，請聯絡系統管理員。", status=403)
        if not verify_password(password, row["password_hash"], row["password_salt"]):
            raise AuthError("帳號或密碼錯誤。")

        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=_TOKEN_TTL_HOURS)
        with self.store.write() as conn:
            conn.execute(
                "INSERT INTO auth_sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, username, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
            )
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE username = ?",
                (now.isoformat(timespec="seconds"), username),
            )
            # 順手清掉過期 token，省一個排程工作。
            conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
        principal = Principal(row["username"], row["display_name"], row["role"])
        return token, principal

    def logout(self, token: str) -> None:
        with self.store.write() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))

    def resolve(self, token: str | None) -> Principal | None:
        """把 token 換成 Principal；無效或過期回 None。"""
        if not token:
            return None
        row = self.store.query_one(
            """
            SELECT s.token, s.expires_at, u.username, u.display_name, u.role, u.enabled
            FROM auth_sessions s JOIN users u ON u.username = s.username
            WHERE s.token = ?
            """,
            (token,),
        )
        if row is None or not row["enabled"]:
            return None
        if row["expires_at"] < utcnow():
            with self.store.write() as conn:
                conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
            return None
        return Principal(row["username"], row["display_name"], row["role"])


def ensure_default_admin(auth: AuthService, username: str, password: str) -> bool:
    """若系統中還沒有任何使用者，建立第一個管理員。回傳是否真的建立。"""
    count = auth.store.scalar("SELECT COUNT(*) FROM users", default=0)
    if count:
        return False
    auth.create_user(username, password, "系統管理員", "admin")
    return True


__all__ = [
    "PERMISSIONS",
    "ROLES",
    "ROLE_LABELS",
    "AuthError",
    "AuthService",
    "Principal",
    "ensure_default_admin",
    "has_permission",
    "hash_password",
    "verify_password",
]
