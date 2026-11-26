from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .auth import Authenticator
from .config import SERVER_ROLES, Config, User, load_config_file
from .db import DB
from .drivers import VMDriver, create_driver
from .service import Portal, PortalError

log = logging.getLogger("portal")
STATIC = Path(__file__).parent / "static"


class WakeIn(BaseModel):
    bank: str
    kind: str = "client"


class ExtendIn(BaseModel):
    until: str
    reason: str


class UserIn(BaseModel):
    email: str
    role: str
    banks: list[str] = []
    enabled: bool = True
    note: str | None = None


class AccessRequestIn(BaseModel):
    role: str
    banks: list[str]
    reason: str


class DecisionIn(BaseModel):
    approve: bool
    role: str | None = None
    banks: list[str] | None = None
    note: str | None = None


class BackgroundLoop:
    """健康檢查（每 health_poll_seconds）和 reconcile（每 reconcile_seconds）。"""

    def __init__(self, portal: Portal):
        self.portal = portal
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="portal-loop", daemon=True)

    def _run(self) -> None:
        p = self.portal.cfg.portal
        poll = float(p.get("health_poll_seconds", 5))
        every = float(p.get("reconcile_seconds", 60))
        elapsed = every  # 一啟動就先跑一次 reconcile
        while not self.stop.is_set():
            try:
                if elapsed >= every:
                    self.portal.reconcile()
                    elapsed = 0.0
                else:
                    self.portal.process_pending()
            except Exception:  # 迴圈不能因為單次錯誤停掉
                log.exception("背景迴圈發生錯誤")
            self.stop.wait(poll)
            elapsed += poll


def create_app(cfg: Config, driver: VMDriver | None = None, db: DB | None = None,
               clock: Callable[[], datetime] | None = None,
               authenticator: Authenticator | None = None,
               background: bool = True) -> FastAPI:
    driver = driver or create_driver(cfg.driver)
    db = db or DB(cfg.portal.get("db_path", "data/portal.db"))
    portal = Portal(cfg, driver, db, clock)
    auth = authenticator or Authenticator(cfg)
    auth.lookup = portal.users.get  # 使用者名單以資料庫為準

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = BackgroundLoop(portal) if background else None
        if loop:
            loop.thread.start()
        log.info("Portal 啟動：driver=%s auth=%s", driver.name, auth.mode)
        yield
        if loop:
            loop.stop.set()

    app = FastAPI(title="Test-Lab Portal", lifespan=lifespan)
    app.state.portal = portal
    app.state.auth = auth

    @app.exception_handler(PortalError)
    async def _portal_error(_: Request, exc: PortalError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    def current_user(request: Request) -> User:
        return auth.user(request)

    def require(*roles: str):
        def dep(user: User = Depends(current_user)) -> User:
            if user.role not in roles:
                raise portal.deny(user, 403, "權限不足")
            return user
        return dep

    # ---------------------------------------------------------------- 頁面
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "driver": driver.name, "auth": auth.mode}

    # ---------------------------------------------------------------- API
    @app.get("/api/me")
    def me(user: User = Depends(current_user)) -> dict[str, Any]:
        win = portal.open_window()
        out: dict[str, Any] = {
            "email": user.email, "role": user.role,
            "can_server": user.role in SERVER_ROLES,
            "can_extend": user.role in cfg.schedule.get("extend_roles", ["PG", "SA"]),
            "is_sa": user.role == "SA",
            "banks": portal.visible_banks(user),
            "window": {
                "is_open": win.is_open, "reason": win.reason,
                "open": win.open.strftime("%H:%M") if win.open else None,
                "close": win.close.strftime("%H:%M") if win.close else None,
                "extended_until": win.extended_until.strftime("%H:%M") if win.extended_until else None,
                "extend_max_until": cfg.schedule.get("extend_max_until", "22:00"),
            },
            "auth_mode": auth.mode, "driver": driver.name,
        }
        if auth.mode == "dev":
            out["dev_users"] = [{"email": u["email"], "role": u["role"]}
                                for u in portal.users.enabled_users()]
        return out

    @app.get("/api/rdp-credentials")
    def rdp_credentials(user: User = Depends(require("SA"))) -> dict[str, Any]:
        c = portal.rdp_credentials()
        if c is None:
            raise PortalError(404, "沒有設定 VM 測試帳號")
        return c

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict[str, Any]:
        """不需要在名單裡也能呼叫：給還沒有權限的人看自己的狀態、送申請。"""
        email = auth.identify(request)
        row = portal.users.row(email) if email else None
        return {
            "email": email,
            "status": "active" if row and row["enabled"] else ("disabled" if row else "unregistered"),
            "pending_request": portal.users.pending_for(email) if email else None,
            "banks": [{"id": b.id, "name": b.name} for b in cfg.banks.values()],
            "roles": ["PM", "SE", "PG", "SA"],
            "auth_mode": auth.mode,
        }

    @app.post("/api/access-requests")
    def request_access(body: AccessRequestIn, request: Request):
        email = auth.identify(request)
        row = portal.users.row(email)
        if row and not row["enabled"]:
            raise PortalError(403, "你的帳號已停用，請直接聯絡 SA")
        return portal.users.request_access(email, body.role, body.banks, body.reason)

    @app.get("/api/access-requests")
    def list_requests(status: str | None = None, user: User = Depends(require("SA"))):
        return portal.users.requests(status)

    @app.post("/api/access-requests/{req_id}/decision")
    def decide(req_id: int, body: DecisionIn, user: User = Depends(current_user)):
        return portal.decide_request(user, req_id, body.approve, body.role, body.banks, body.note)

    @app.get("/api/users")
    def list_users(user: User = Depends(require("SA"))):
        return portal.users.list()

    @app.put("/api/users")
    def save_user(body: UserIn, user: User = Depends(current_user)):
        return portal.save_user(user, body.model_dump())

    @app.delete("/api/users/{email}")
    def delete_user(email: str, user: User = Depends(current_user)):
        return portal.delete_user(user, email)

    @app.get("/api/cf-sync")
    def cf_sync_status(user: User = Depends(require("SA"))):
        return portal.sync_status()

    @app.post("/api/cf-sync")
    def cf_sync_now(user: User = Depends(require("SA"))):
        return portal.sync_access(user.email)

    def view(a: dict[str, Any], user: User) -> dict[str, Any]:
        """一次性密碼只給這筆分配的本人看，而且只在使用中時。"""
        a = dict(a)
        mine = a["email"] == user.email
        if mine and a["status"] == "ready":
            creds = portal.rdp_credentials()
            if creds:
                a["rdp_user"] = creds["username"]
                if not a.get("rdp_password"):
                    a["rdp_password"] = creds["password"]   # 一次性密碼沒設成功時用共用密碼
        if not (mine and a["status"] == "ready"):
            a.pop("rdp_password", None)
        return a

    @app.post("/api/wake")
    def wake(body: WakeIn, user: User = Depends(current_user)):
        return view(portal.wake(user, body.bank, body.kind), user)

    @app.get("/api/allocations")
    def allocations(all: bool = False, user: User = Depends(current_user)):
        see_all = all and user.role in SERVER_ROLES
        return [view(a, user) for a in portal.allocations_for(user, mine_only=not see_all)]

    @app.get("/api/allocations/{alloc_id}")
    def allocation(alloc_id: int, user: User = Depends(current_user)):
        a = portal.allocation(alloc_id)
        if a["email"] != user.email and user.role not in SERVER_ROLES:
            raise PortalError(403, "不能查看別人的分配")
        return view(a, user)

    @app.post("/api/allocations/{alloc_id}/release")
    def release(alloc_id: int, user: User = Depends(current_user)):
        return view(portal.release(user, alloc_id), user)

    @app.post("/api/extend")
    def extend(body: ExtendIn, user: User = Depends(current_user)):
        return portal.extend_today(user, body.until, body.reason)

    @app.get("/api/extensions")
    def extensions(user: User = Depends(require("PG", "SA"))):
        return portal.extensions()

    @app.get("/api/vms")
    def vms(user: User = Depends(require("PG", "SA"))):
        return portal.vm_overview()

    @app.get("/api/prewarm")
    def get_prewarm(user: User = Depends(require("PG", "SA"))):
        return portal.prewarm_settings()

    @app.put("/api/prewarm")
    def put_prewarm(body: dict[str, Any], user: User = Depends(current_user)):
        return portal.update_prewarm(user, body)

    @app.post("/api/reconcile")
    def reconcile(user: User = Depends(require("SA"))):
        return portal.reconcile()

    @app.get("/api/audit")
    def audit(user: User = Depends(require("SA"))):
        return portal.audit_log()

    return app


def app_from_env() -> FastAPI:
    """uvicorn 用：python -m portal 或 uvicorn portal.app:app_from_env --factory"""
    path = os.environ.get("PORTAL_CONFIG", "portal.toml")
    if not Path(path).exists():
        path = "portal.example.toml"
        log.warning("找不到 portal.toml，改用 portal.example.toml（示範設定）")
    return create_app(load_config_file(path))
