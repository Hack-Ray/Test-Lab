"""Portal 核心邏輯：權限、開放時段、喚醒與分配、預暖機、紀錄。

和 web 層分開，方便直接寫測試。時間一律用 Portal 時區（預設 Asia/Taipei）。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .cloudflare import AccessGroupSync, SyncError
from .config import Config, User, parse_hhmm
from .db import DB
from .drivers import DriverError, PowerState, VMDriver, VMSpec
from .errors import PortalError
from .users import UserStore

ACTIVE = ("starting", "ready")


@dataclass
class OpenWindow:
    is_open: bool
    open: time | None
    close: time | None
    extended_until: time | None
    reason: str  # 給 UI 顯示


class Portal:
    def __init__(self, cfg: Config, driver: VMDriver, db: DB,
                 clock: Callable[[], datetime] | None = None,
                 access_sync: AccessGroupSync | None = None):
        self.cfg = cfg
        self.driver = driver
        self.db = db
        self.tz = ZoneInfo(cfg.portal.get("timezone", "Asia/Taipei"))
        self.clock = clock or (lambda: datetime.now(self.tz))
        self._alloc_lock = threading.RLock()
        self.users = UserStore(cfg, db, lambda: self.now().isoformat(timespec="seconds"),
                               self.audit)
        self.users.seed_from_config()
        self.access_sync = access_sync or AccessGroupSync(cfg.raw.get("cloudflare_sync", {}))

    # ------------------------------------------------------------ 共用
    def now(self) -> datetime:
        return self.clock().astimezone(self.tz)

    def audit(self, email: str | None, action: str, **detail: Any) -> None:
        self.db.execute(
            "INSERT INTO audit(ts, email, action, detail) VALUES(?, ?, ?, ?)",
            (self.now().isoformat(timespec="seconds"), email, action,
             json.dumps(detail, ensure_ascii=False) if detail else None),
        )

    def deny(self, user: User | None, status: int, message: str, **detail: Any) -> PortalError:
        self.audit(user.email if user else None, "denied", message=message, **detail)
        return PortalError(status, message)

    # ------------------------------------------------------------ 開放時段
    def _today_extension(self, now: datetime) -> time | None:
        row = self.db.one("SELECT MAX(until) AS u FROM extensions WHERE day = ?",
                          (now.date().isoformat(),))
        return parse_hhmm(row["u"]) if row and row["u"] else None

    def open_window(self, now: datetime | None = None) -> OpenWindow:
        now = now or self.now()
        s = self.cfg.schedule
        weekdays = s.get("weekdays", [1, 2, 3, 4, 5])
        if now.isoweekday() not in weekdays:
            return OpenWindow(False, None, None, None, "今天不是開放日")
        start = parse_hhmm(s.get("open", "08:00"))
        close = parse_hhmm(s.get("close", "17:30"))
        ext = self._today_extension(now)
        effective_close = max(close, ext) if ext else close
        t = now.time()
        if t < start:
            return OpenWindow(False, start, close, ext, f"尚未開放（{start:%H:%M} 開放）")
        if t >= effective_close:
            return OpenWindow(False, start, close, ext,
                              f"已過開放時段（{effective_close:%H:%M} 結束）")
        return OpenWindow(True, start, close, ext, f"開放中，到 {effective_close:%H:%M}")

    def extend_today(self, user: User, until: str, reason: str) -> dict[str, Any]:
        s = self.cfg.schedule
        if user.role not in s.get("extend_roles", ["PG", "SA"]):
            raise self.deny(user, 403, "只有 PG / SA 可以延長開放時段", op="extend")
        reason = (reason or "").strip()
        if not reason:
            raise PortalError(400, "延長必須填寫原因")
        try:
            until_t = parse_hhmm(until)
        except (ValueError, AttributeError):
            raise PortalError(400, "時間格式應為 HH:MM")
        now = self.now()
        if now.isoweekday() not in s.get("weekdays", [1, 2, 3, 4, 5]):
            raise PortalError(400, "非開放日不能延長")
        close = parse_hhmm(s.get("close", "17:30"))
        cap = parse_hhmm(s.get("extend_max_until", "22:00"))
        if until_t <= close:
            raise PortalError(400, f"延長時間必須晚於 {close:%H:%M}")
        if until_t > cap:
            raise PortalError(400, f"最多只能延長到 {cap:%H:%M}")
        if until_t <= now.time():
            raise PortalError(400, "延長時間必須晚於現在")
        self.db.execute(
            "INSERT INTO extensions(day, until, email, reason, created_at) VALUES(?,?,?,?,?)",
            (now.date().isoformat(), f"{until_t:%H:%M}", user.email, reason,
             now.isoformat(timespec="seconds")),
        )
        self.audit(user.email, "extend", until=f"{until_t:%H:%M}", reason=reason)
        return {"day": now.date().isoformat(), "until": f"{until_t:%H:%M}"}

    def extensions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM extensions ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ 使用者管理（SA）
    def _require_sa(self, user: User) -> None:
        if user.role != "SA":
            raise self.deny(user, 403, "只有 SA 可以管理使用者")

    def save_user(self, actor: User, data: dict[str, Any]) -> dict[str, Any]:
        self._require_sa(actor)
        row = self.users.upsert(actor, data.get("email", ""), data.get("role", ""),
                                data.get("banks") or [], bool(data.get("enabled", True)),
                                data.get("note"))
        return {"user": row, "sync": self.sync_access(actor.email)}

    def delete_user(self, actor: User, email: str) -> dict[str, Any]:
        self._require_sa(actor)
        self.users.delete(actor, email)
        return {"sync": self.sync_access(actor.email)}

    def decide_request(self, actor: User, req_id: int, approve: bool,
                       role: str | None = None, banks: list[str] | None = None,
                       note: str | None = None) -> dict[str, Any]:
        self._require_sa(actor)
        req = self.users.decide(actor, req_id, approve, role, banks, note)
        sync = self.sync_access(actor.email) if approve else {"skipped": True}
        return {"request": req, "sync": sync}

    def sync_access(self, actor: str | None) -> dict[str, Any]:
        """把啟用中的使用者同步到 Cloudflare Access Group。失敗不影響 Portal 本身的修改。"""
        if not self.access_sync.enabled:
            return {"skipped": True}
        at = self.now().isoformat(timespec="seconds")
        try:
            res = {"ok": True, **self.access_sync.sync(self.users.enabled_users()), "at": at}
            self.audit(actor, "cf_sync", **res)
        except SyncError as e:
            res = {"ok": False, "error": str(e), "at": at}
            self.audit(actor, "cf_sync_failed", error=str(e))
        self.db.set_setting("cf_sync_last", res)
        return res

    def sync_status(self) -> dict[str, Any]:
        return {"enabled": self.access_sync.enabled,
                "last": self.db.get_setting("cf_sync_last")}

    # ------------------------------------------------------------ 預暖機設定
    def prewarm_settings(self) -> dict[str, Any]:
        base = {
            "enabled": True, "standby_clients": 1, "start": "07:45", "end": "16:30",
            "max_running": 4,
        }
        base.update(self.cfg.prewarm)
        base.update(self.db.get_setting("prewarm") or {})
        return base

    def update_prewarm(self, user: User, changes: dict[str, Any]) -> dict[str, Any]:
        if user.role != "SA":
            raise self.deny(user, 403, "只有 SA 可以修改預暖機設定")
        current = self.db.get_setting("prewarm") or {}
        clean: dict[str, Any] = {}
        for k, v in changes.items():
            if k == "enabled":
                clean[k] = bool(v)
            elif k in ("standby_clients", "max_running"):
                n = int(v)
                if n < 0 or n > 50:
                    raise PortalError(400, f"{k} 超出範圍")
                clean[k] = n
            elif k in ("start", "end"):
                parse_hhmm(str(v))
                clean[k] = str(v)
            else:
                raise PortalError(400, f"未知的設定：{k}")
        current.update(clean)
        self.db.set_setting("prewarm", current)
        self.audit(user.email, "prewarm_update", **clean)
        return self.prewarm_settings()

    # ------------------------------------------------------------ VM 狀態
    def power(self, vm: VMSpec) -> PowerState:
        try:
            return self.driver.status(vm)
        except DriverError:
            return PowerState.UNKNOWN

    def running_count(self) -> int:
        return sum(1 for vm in self.cfg.vms.values() if self.power(vm) == PowerState.RUNNING)

    def active_allocations(self, vm_name: str | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM allocations WHERE status IN {ACTIVE}"
        if vm_name:
            return self.db.query(sql + " AND vm = ?", (vm_name,))
        return self.db.query(sql)

    def _ensure_running(self, vm: VMSpec, email: str, why: str) -> bool:
        """沒開機就開機；回傳是否真的有開機動作。"""
        if self.power(vm) == PowerState.RUNNING:
            return False
        cap = int(self.prewarm_settings()["max_running"])
        if self.running_count() >= cap:
            raise PortalError(503, f"同時開機的 VM 已達上限（{cap} 台），請稍後再試")
        try:
            self.driver.start(vm)
        except DriverError as e:
            self.audit(email, "vm_start_failed", vm=vm.name, error=str(e))
            raise PortalError(502, f"{vm.name} 開機失敗：{e}")
        self.audit(email, "vm_start", vm=vm.name, why=why)
        return True

    def vm_overview(self) -> list[dict[str, Any]]:
        out = []
        for vm in self.cfg.vms.values():
            allocs = self.active_allocations(vm.name)
            out.append({
                "name": vm.name, "kind": vm.kind, "bank": vm.bank,
                "power": self.power(vm).value,
                "users": [a["email"] for a in allocs],
            })
        return out

    # ------------------------------------------------------------ 喚醒與分配
    def visible_banks(self, user: User) -> list[dict[str, str]]:
        return [{"id": b.id, "name": b.name} for b in self.cfg.banks.values()
                if user.can_access_bank(b.id)]

    def wake(self, user: User, bank_id: str, kind: str) -> dict[str, Any]:
        if kind not in ("client", "server"):
            raise PortalError(400, "kind 必須是 client 或 server")
        if bank_id not in self.cfg.banks:
            raise PortalError(404, "找不到這家銀行")
        if not user.can_access_kind(kind):
            raise self.deny(user, 403, f"{user.role} 只能使用 client", bank=bank_id, kind=kind)
        if not user.can_access_bank(bank_id):
            raise self.deny(user, 403, "沒有這家銀行的權限", bank=bank_id, kind=kind)
        win = self.open_window()
        if not win.is_open:
            raise self.deny(user, 403, f"目前不在開放時段：{win.reason}", bank=bank_id, kind=kind)

        server = self.cfg.server_for(bank_id)
        if server is None:
            raise PortalError(500, "這家銀行沒有設定 server")

        with self._alloc_lock:
            # server 一定要先開：client 測試也要連 server
            self._ensure_running(server, user.email, f"wake:{bank_id}")
            if kind == "server":
                alloc = self._existing(user.email, server.name, bank_id)
                if alloc is None:
                    alloc = self._create_alloc(server, bank_id, user.email)
            else:
                alloc = self._allocate_client(user, bank_id)
        self.check_allocation(alloc["id"])
        return self.allocation(alloc["id"])

    def _existing(self, email: str, vm: str, bank: str) -> dict[str, Any] | None:
        return self.db.one(
            f"SELECT * FROM allocations WHERE email=? AND vm=? AND bank=? AND status IN {ACTIVE}",
            (email, vm, bank))

    def _create_alloc(self, vm: VMSpec, bank_id: str, email: str) -> dict[str, Any]:
        cur = self.db.execute(
            "INSERT INTO allocations(vm, kind, bank, email, status, created_at) "
            "VALUES(?, ?, ?, ?, 'starting', ?)",
            (vm.name, vm.kind, bank_id, email, self.now().isoformat(timespec="seconds")))
        self.audit(email, "allocate", vm=vm.name, bank=bank_id, kind=vm.kind)
        return self.allocation(cur.lastrowid)

    def _allocate_client(self, user: User, bank_id: str) -> dict[str, Any]:
        candidates = self.cfg.clients_for(bank_id)
        names = {c.name for c in candidates}
        # 一人一台：已經有 client 就沿用
        mine = self.db.query(
            f"SELECT * FROM allocations WHERE email=? AND kind='client' AND status IN {ACTIVE}",
            (user.email,))
        for a in mine:
            if a["bank"] == bank_id:
                return a
            if a["vm"] in names:
                # 換銀行但同一台 client：結束舊紀錄，開新紀錄
                self._end_alloc(a["id"], "released", "switch_bank")
                return self._create_alloc(self.cfg.vms[a["vm"]], bank_id, user.email)
            raise PortalError(409, f"你已經在使用 {a['vm']}（{a['bank']}），請先釋放")

        busy = {a["vm"] for a in self.active_allocations()}
        free = [c for c in candidates if c.name not in busy]
        if not free:
            raise PortalError(503, "目前沒有空的 client，請稍後再試")
        # 已開機（預暖機）的優先
        free.sort(key=lambda c: 0 if self.power(c) == PowerState.RUNNING else 1)
        vm = free[0]
        self._ensure_running(vm, user.email, f"wake:{bank_id}")
        return self._create_alloc(vm, bank_id, user.email)

    def allocation(self, alloc_id: int) -> dict[str, Any]:
        a = self.db.one("SELECT * FROM allocations WHERE id = ?", (alloc_id,))
        if a is None:
            raise PortalError(404, "找不到分配紀錄")
        vm = self.cfg.vms.get(a["vm"])
        a["rdp_url"] = vm.rdp_url if vm else None
        if a["kind"] == "client":
            srv = self.cfg.server_for(a["bank"])
            a["server"] = srv.name if srv else None
        return a

    def rdp_credentials(self) -> dict[str, str] | None:
        """VM 共用測試帳號。密碼每次從 secrets 檔讀（主機上改密碼後不用重啟 Portal）。"""
        rdp = self.cfg.raw.get("rdp", {})
        user = rdp.get("username")
        if not user:
            return None
        password = rdp.get("password", "")
        path = rdp.get("secrets_file")
        if path:
            try:
                with open(path, encoding="utf-8-sig") as f:
                    password = json.load(f).get(rdp.get("secrets_key", "TesterPassword"), password)
            except (OSError, ValueError):
                pass
        return {"username": user, "password": password}

    def _end_alloc(self, alloc_id: int, status: str, note: str) -> None:
        self.db.execute(
            "UPDATE allocations SET status=?, ended_at=?, note=? WHERE id=?",
            (status, self.now().isoformat(timespec="seconds"), note, alloc_id))

    def release(self, user: User, alloc_id: int) -> dict[str, Any]:
        a = self.allocation(alloc_id)
        if a["email"] != user.email and user.role != "SA":
            raise self.deny(user, 403, "只能釋放自己的分配", alloc=alloc_id)
        if a["status"] in ACTIVE:
            self._end_alloc(alloc_id, "released", f"by:{user.email}")
            self.audit(user.email, "release", alloc=alloc_id, vm=a["vm"])
        return self.allocation(alloc_id)

    def check_allocation(self, alloc_id: int) -> dict[str, Any]:
        """健康檢查一次：client 檢查自己和 server，server 檢查自己。"""
        a = self.allocation(alloc_id)
        if a["status"] != "starting":
            return a
        targets = [self.cfg.vms[a["vm"]]]
        if a["kind"] == "client" and a.get("server"):
            targets.append(self.cfg.vms[a["server"]])
        results: dict[str, dict[int, bool]] = {}
        for vm in targets:
            try:
                results[vm.name] = self.driver.health(vm)
            except DriverError:
                results[vm.name] = {p: False for p in vm.ports}
        if all(all(r.values()) for r in results.values()):
            self.db.execute("UPDATE allocations SET status='ready', ready_at=?, note=NULL WHERE id=?",
                            (self.now().isoformat(timespec="seconds"), alloc_id))
            self.audit(a["email"], "ready", alloc=alloc_id, vm=a["vm"])
        else:
            failed = {n: [p for p, ok in r.items() if not ok] for n, r in results.items()}
            failed = {n: ps for n, ps in failed.items() if ps}
            created = datetime.fromisoformat(a["created_at"])
            limit = float(self.cfg.portal.get("health_timeout_seconds", 600))
            if (self.now() - created).total_seconds() > limit:
                self._end_alloc(alloc_id, "failed", json.dumps(failed))
                self.audit(a["email"], "health_timeout", alloc=alloc_id, failed=failed)
            else:
                # 等待中也記下還不通的 port，畫面上看得到卡在哪
                note = "等待連線：" + "、".join(f"{n} port {','.join(map(str, ps))}" for n, ps in failed.items())
                self.db.execute("UPDATE allocations SET note=? WHERE id=?", (note, alloc_id))
        return self.allocation(alloc_id)

    def process_pending(self) -> None:
        for a in self.db.query("SELECT id FROM allocations WHERE status='starting'"):
            self.check_allocation(a["id"])

    def allocations_for(self, user: User, mine_only: bool, limit: int = 100) -> list[dict[str, Any]]:
        if mine_only:
            rows = self.db.query(
                "SELECT id FROM allocations WHERE email=? ORDER BY id DESC LIMIT ?",
                (user.email, limit))
        else:
            rows = self.db.query("SELECT id FROM allocations ORDER BY id DESC LIMIT ?", (limit,))
        return [self.allocation(r["id"]) for r in rows]

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ reconcile
    def reconcile(self) -> dict[str, Any]:
        """每分鐘跑一次：同步 VM 狀態、補足待命台數。"""
        closed = []
        for a in self.db.query("SELECT * FROM allocations WHERE status='ready'"):
            vm = self.cfg.vms.get(a["vm"])
            if vm is None or self.power(vm) == PowerState.STOPPED:
                self._end_alloc(a["id"], "released", "vm_stopped")
                closed.append(a["id"])
        if closed:
            self.audit(None, "auto_release", allocs=closed)

        # 開放時段結束：把還在使用中的分配結束掉（VM 本身交給 Windows 閒置關機）
        after_hours = []
        if not self.open_window().is_open:
            for a in self.active_allocations():
                self._end_alloc(a["id"], "released", "after_hours")
                after_hours.append(a["id"])
            if after_hours:
                self.audit(None, "after_hours_release", allocs=after_hours)

        started = []
        s = self.prewarm_settings()
        now = self.now()
        in_window = (
            s["enabled"]
            and now.isoweekday() in self.cfg.schedule.get("weekdays", [1, 2, 3, 4, 5])
            and parse_hhmm(s["start"]) <= now.time() < parse_hhmm(s["end"])
        )
        if in_window:
            with self._alloc_lock:
                busy = {a["vm"] for a in self.active_allocations()}
                clients = [v for v in self.cfg.vms.values() if v.kind == "client"]
                idle_running = [c for c in clients if c.name not in busy
                                and self.power(c) == PowerState.RUNNING]
                need = int(s["standby_clients"]) - len(idle_running)
                for c in clients:
                    if need <= 0:
                        break
                    if c.name in busy or self.power(c) == PowerState.RUNNING:
                        continue
                    try:
                        self._ensure_running(c, "prewarm", "prewarm")
                    except PortalError:
                        break  # 到上限或開機失敗就先停
                    started.append(c.name)
                    need -= 1
        self.process_pending()
        return {"auto_released": closed, "after_hours_released": after_hours,
                "prewarmed": started, "prewarm_window": in_window}
