"""使用者名單與權限申請（存在 SQLite）。

設定檔的 [[users]] 只在資料庫還沒有任何使用者時匯入一次，之後一律在 Portal 的
使用者管理頁維護。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .config import ROLES, Config, User
from .db import DB
from .errors import PortalError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserStore:
    def __init__(self, cfg: Config, db: DB, now: Callable[[], str],
                 audit: Callable[..., None]):
        self.cfg = cfg
        self.db = db
        self.now = now
        self.audit = audit

    # ------------------------------------------------------------ 查詢
    def seed_from_config(self) -> int:
        if self.db.one("SELECT 1 AS x FROM users LIMIT 1"):
            return 0
        for u in self.cfg.users.values():
            self.db.execute(
                "INSERT INTO users(email, role, banks, enabled, created_at, updated_at, updated_by) "
                "VALUES(?, ?, ?, 1, ?, ?, 'config')",
                (u.email, u.role, json.dumps(list(u.banks)), self.now(), self.now()))
        if self.cfg.users:
            self.audit(None, "users_seeded", count=len(self.cfg.users))
        return len(self.cfg.users)

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        r = dict(r)
        r["banks"] = json.loads(r["banks"])
        r["enabled"] = bool(r["enabled"])
        return r

    def row(self, email: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
        return self._row(r) if r else None

    def get(self, email: str) -> User | None:
        """登入用：只回啟用中的使用者。"""
        r = self.row(email)
        if r is None or not r["enabled"]:
            return None
        return User(email=r["email"], role=r["role"], banks=tuple(r["banks"]))

    def list(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.query("SELECT * FROM users ORDER BY role, email")]

    def enabled_users(self) -> list[dict[str, Any]]:
        return [u for u in self.list() if u["enabled"]]

    # ------------------------------------------------------------ 驗證
    def _clean(self, email: str, role: str, banks: list[str]) -> tuple[str, str, list[str]]:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise PortalError(400, "email 格式不正確")
        role = (role or "").strip().upper()
        if role not in ROLES:
            raise PortalError(400, f"角色必須是 {' / '.join(ROLES)}")
        banks = sorted({b.strip() for b in banks or [] if b and b.strip()})
        unknown = [b for b in banks if b != "*" and b not in self.cfg.banks]
        if unknown:
            raise PortalError(400, f"沒有這些銀行：{', '.join(unknown)}")
        if "*" in banks:
            banks = ["*"]
        return email, role, banks

    def _guard_last_sa(self, email: str, new_role: str | None, new_enabled: bool) -> None:
        """不能把最後一個啟用中的 SA 降級、停用或刪除，避免沒人能管理。"""
        cur = self.row(email)
        if not cur or cur["role"] != "SA" or not cur["enabled"]:
            return
        if new_role == "SA" and new_enabled:
            return
        others = self.db.one(
            "SELECT COUNT(*) AS n FROM users WHERE role='SA' AND enabled=1 AND email != ?",
            (email,))
        if not others or others["n"] == 0:
            raise PortalError(400, "至少要保留一位啟用中的 SA")

    # ------------------------------------------------------------ 修改（SA）
    def upsert(self, actor: User, email: str, role: str, banks: list[str],
               enabled: bool = True, note: str | None = None) -> dict[str, Any]:
        email, role, banks = self._clean(email, role, banks)
        self._guard_last_sa(email, role, enabled)
        before = self.row(email)
        now = self.now()
        self.db.execute(
            "INSERT INTO users(email, role, banks, enabled, note, created_at, updated_at, updated_by) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
            "role=excluded.role, banks=excluded.banks, enabled=excluded.enabled, "
            "note=excluded.note, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (email, role, json.dumps(banks), int(bool(enabled)), note, now, now, actor.email))
        after = self.row(email)
        self.audit(actor.email, "user_update" if before else "user_create", target=email,
                   before={k: before[k] for k in ("role", "banks", "enabled")} if before else None,
                   after={k: after[k] for k in ("role", "banks", "enabled")})
        return after

    def delete(self, actor: User, email: str) -> None:
        email = email.strip().lower()
        if self.row(email) is None:
            raise PortalError(404, "找不到這個使用者")
        self._guard_last_sa(email, None, False)
        self.db.execute("DELETE FROM users WHERE email = ?", (email,))
        self.audit(actor.email, "user_delete", target=email)

    # ------------------------------------------------------------ 權限申請
    def request_access(self, email: str, role: str, banks: list[str], reason: str) -> dict[str, Any]:
        email, role, banks = self._clean(email, role, banks)
        if "*" in banks:
            raise PortalError(400, "申請時請選擇具體的銀行")
        reason = (reason or "").strip()
        if not reason:
            raise PortalError(400, "請填寫申請原因")
        if self.get(email):
            raise PortalError(409, "你已經有權限了")
        if self.pending_for(email):
            raise PortalError(409, "你已經有一筆申請在等待審核")
        cur = self.db.execute(
            "INSERT INTO access_requests(email, role, banks, reason, status, created_at) "
            "VALUES(?, ?, ?, ?, 'pending', ?)",
            (email, role, json.dumps(banks), reason, self.now()))
        self.audit(email, "access_request", role=role, banks=banks)
        return self.request(cur.lastrowid)

    def request(self, req_id: int) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM access_requests WHERE id = ?", (req_id,))
        if r is None:
            raise PortalError(404, "找不到這筆申請")
        r["banks"] = json.loads(r["banks"])
        return r

    def pending_for(self, email: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT id FROM access_requests WHERE email=? AND status='pending'",
                        (email.strip().lower(),))
        return self.request(r["id"]) if r else None

    def requests(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            rows = self.db.query("SELECT id FROM access_requests WHERE status=? ORDER BY id DESC LIMIT ?",
                                 (status, limit))
        else:
            rows = self.db.query("SELECT id FROM access_requests ORDER BY id DESC LIMIT ?", (limit,))
        return [self.request(r["id"]) for r in rows]

    def decide(self, actor: User, req_id: int, approve: bool, role: str | None = None,
               banks: list[str] | None = None, note: str | None = None) -> dict[str, Any]:
        r = self.request(req_id)
        if r["status"] != "pending":
            raise PortalError(409, "這筆申請已經處理過了")
        if approve:
            # SA 可以在核准時調整角色或銀行
            self.upsert(actor, r["email"], role or r["role"],
                        banks if banks is not None else r["banks"], True,
                        note=f"申請 #{req_id} 核准")
        self.db.execute(
            "UPDATE access_requests SET status=?, decided_by=?, decided_at=?, decision_note=? "
            "WHERE id=?",
            ("approved" if approve else "rejected", actor.email, self.now(), note, req_id))
        self.audit(actor.email, "access_approve" if approve else "access_reject",
                   request=req_id, target=r["email"])
        return self.request(req_id)
