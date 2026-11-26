"""從 Portal 網頁新增主機（SA）。

流程（背景執行，一次一台）：
  1. 從同類型的範本 VM 的 clean 快照複製（預設 linked clone，快又省空間）
  2. 開機，等 VMware Tools 能用
  3. 在 VM 裡改固定 IP、電腦名稱、時區、關掉關機原因視窗，重開機
  4. 等新 IP 的 3389 能連
  5. Cloudflare：建 RDP 目標、加進瀏覽器 RDP 應用程式
  6. 寫進 extra 檔（Portal 與部署腳本都會讀），關機待命

設定在 portal.toml 的 [provision]：
  vm_root、ip_prefix（例如 192.168.18）、prefix_length、gateway、dns、ip_range = [10, 99]、
  template_client / template_server（範本 VM 名稱，預設同類型第一台）、snapshot = "clean"、linked = true、
  vnet_id、rdp_client_host、rdp_server_host
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from .cloudflare import RdpTargets, SyncError
from .config import Bank, add_vm, load_extras, save_extras
from .drivers import DriverError, VMSpec
from .drivers.base import ps_literal
from .errors import PortalError

if TYPE_CHECKING:
    from .service import Portal

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,14}$")      # Windows 電腦名稱最多 15 字
BANK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def reconfigure_script(name: str, ip: str, prefix_length: int, gateway: str, dns: str) -> str:
    """在複製出來的 VM 裡改 IP 和電腦名稱，最後排一個重開機（讓這支程式先正常結束）。"""
    return f"""
$nic = Get-NetAdapter | Where-Object Status -eq 'Up' | Sort-Object ifIndex | Select-Object -First 1
if (-not $nic) {{ $nic = Get-NetAdapter | Sort-Object ifIndex | Select-Object -First 1 }}
Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
Get-NetRoute -InterfaceIndex $nic.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress {ps_literal(ip)} -PrefixLength {int(prefix_length)} -DefaultGateway {ps_literal(gateway)} | Out-Null
Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses {ps_literal(dns)}
Set-TimeZone -Id 'Taipei Standard Time'
$rel = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows NT\\Reliability'
New-Item -Path $rel -Force | Out-Null
Set-ItemProperty $rel -Name ShutdownReasonOn -Value 0 -Type DWord
if ($env:COMPUTERNAME -ne {ps_literal(name.upper())}) {{ Rename-Computer -NewName {ps_literal(name.upper())} -Force }}
& shutdown.exe /r /t 10 /d p:0:0
"""


class Provisioner:
    def __init__(self, portal: "Portal", sleep: Callable[[float], None] = time.sleep,
                 cf: RdpTargets | None = None, run_async: bool = True):
        self.portal = portal
        self.cfg = portal.cfg
        self.db = portal.db
        self.sleep = sleep
        self.run_async = run_async
        self._lock = threading.Lock()
        self._cf = cf

    # ------------------------------------------------------------ 設定
    @property
    def pcfg(self) -> dict[str, Any]:
        return self.cfg.raw.get("provision", {})

    @property
    def enabled(self) -> bool:
        return bool(self.pcfg.get("vm_root")) and bool(self.pcfg.get("ip_prefix"))

    def template_for(self, kind: str) -> VMSpec | None:
        name = self.pcfg.get(f"template_{kind}")
        if name and name in self.cfg.vms:
            return self.cfg.vms[name]
        return next((v for v in self.cfg.vms.values() if v.kind == kind and v.vmx), None)

    def next_ip(self) -> str:
        prefix = self.pcfg["ip_prefix"]
        lo, hi = self.pcfg.get("ip_range", [10, 99])
        used = {v.host for v in self.cfg.vms.values() if v.host}
        for job in self.jobs(active_only=True):
            used.add(job["ip"])
        for n in range(int(lo), int(hi) + 1):
            ip = f"{prefix}.{n}"
            if ip not in used:
                return ip
        raise PortalError(409, f"{prefix}.{lo}–{hi} 已經沒有可用的 IP")

    def info(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "templates": {k: (t.name if (t := self.template_for(k)) else None) for k in ("client", "server")},
            "next_ip": self.next_ip() if self.enabled else None,
            "jobs": self.jobs(),
        }

    def jobs(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM provision_jobs"
        if active_only:
            sql += " WHERE status IN ('queued', 'running')"
        return self.db.query(sql + " ORDER BY id DESC LIMIT 50")

    # ------------------------------------------------------------ 送出
    def request(self, email: str, name: str, kind: str, bank: str | None,
                new_bank_name: str | None, ports: list[int]) -> dict[str, Any]:
        if not self.enabled:
            raise PortalError(400, "portal.toml 沒有 [provision] 設定，請重跑 deploy.ps1 -Stage Portal")
        name = (name or "").strip().lower()
        if not NAME_RE.match(name):
            raise PortalError(400, "名稱只能用小寫英文、數字、減號，2–15 字，英文開頭")
        if name in self.cfg.vms or any(j["name"] == name for j in self.jobs(active_only=True)):
            raise PortalError(409, f"已經有 {name}")
        if kind not in ("client", "server"):
            raise PortalError(400, "類型要是 client 或 server")
        bank = (bank or "").strip().lower() or None
        if new_bank_name:
            if not bank or not BANK_ID_RE.match(bank):
                raise PortalError(400, "新銀行的代碼只能用小寫英文、數字、減號")
            if bank in self.cfg.banks:
                raise PortalError(409, f"銀行代碼 {bank} 已經存在")
        elif bank and bank not in self.cfg.banks:
            raise PortalError(404, "找不到這家銀行")
        if kind == "server":
            if not bank:
                raise PortalError(400, "server 要指定銀行")
            if self.cfg.server_for(bank):
                raise PortalError(409, f"{bank} 已經有 server（一家銀行一台）")
        if self.template_for(kind) is None:
            raise PortalError(400, f"沒有可以當範本的 {kind} VM")
        ports = sorted({int(p) for p in (ports or [3389])} | {3389})
        ip = self.next_ip()
        now = self.portal.now().isoformat(timespec="seconds")
        cur = self.db.execute(
            "INSERT INTO provision_jobs(name, kind, bank, bank_name, ports, ip, email, status, step, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'queued', '排隊中', ?)",
            (name, kind, bank, new_bank_name, json.dumps(ports), ip, email, now))
        job_id = cur.lastrowid
        self.portal.audit(email, "provision_request", job=job_id, name=name, kind=kind, bank=bank, ip=ip)
        if self.run_async:
            threading.Thread(target=self.run, args=(job_id,), daemon=True, name=f"provision-{name}").start()
        else:
            self.run(job_id)
        return self.job(job_id)

    def job(self, job_id: int) -> dict[str, Any]:
        j = self.db.one("SELECT * FROM provision_jobs WHERE id=?", (job_id,))
        if j is None:
            raise PortalError(404, "找不到這個工作")
        return j

    # ------------------------------------------------------------ 執行
    def _step(self, job_id: int, step: str, status: str = "running") -> None:
        log.info("provision #%s：%s", job_id, step)
        self.db.execute("UPDATE provision_jobs SET status=?, step=? WHERE id=?", (status, step, job_id))

    def _wait(self, check: Callable[[], bool], seconds: float, every: float = 10) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if check():
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(every)

    def run(self, job_id: int) -> None:
        with self._lock:   # 一次一台：clone 很吃硬碟
            try:
                self._run(job_id)
            except Exception as e:  # noqa: BLE001 — 任何錯誤都記到工作上
                log.exception("provision #%s 失敗", job_id)
                self.db.execute(
                    "UPDATE provision_jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                    (str(e), self.portal.now().isoformat(timespec="seconds"), job_id))
                j = self.job(job_id)
                self.portal.audit(j["email"], "provision_failed", job=job_id, name=j["name"], error=str(e))

    def _run(self, job_id: int) -> None:
        j = self.job(job_id)
        p = self.pcfg
        driver = self.portal.driver
        name, kind, ip = j["name"], j["kind"], j["ip"]
        tpl = self.template_for(kind)
        if tpl is None:
            raise RuntimeError(f"沒有 {kind} 範本")
        snapshot = p.get("snapshot", "clean")
        linked = bool(p.get("linked", True))
        dst = os.path.join(p["vm_root"], name, f"{name}.vmx")

        self._step(job_id, f"從 {tpl.name} 的 {snapshot} 快照複製（{'linked' if linked else 'full'} clone）")
        vmx = driver.clone(tpl, snapshot, dst, name, linked=linked)
        spec = VMSpec(name=name, kind=kind, bank=j["bank"], vmx=vmx, host=ip,
                      ports=tuple(json.loads(j["ports"])))

        self._step(job_id, "開機，等 VMware Tools")
        driver.start(spec)
        admin = p.get("admin_user", "Administrator")
        admin_pw = self.portal._secret(self.cfg.raw.get("rdp", {}).get("admin_key", "AdminPassword"))

        def tools_ready() -> bool:
            try:
                driver.run_in_guest(spec, admin, admin_pw, "exit 0", timeout=60)
                return True
            except DriverError:
                return False
        if not self._wait(tools_ready, float(p.get("tools_timeout_seconds", 900))):
            raise RuntimeError("等不到 VMware Tools（範本裡沒裝 Tools，或 Administrator 密碼不對）")

        self._step(job_id, f"設定 IP {ip}、電腦名稱 {name.upper()}，重開機")
        driver.run_in_guest(spec, admin, admin_pw, reconfigure_script(
            name, ip, int(p.get("prefix_length", 24)), p.get("gateway", ""), p.get("dns", p.get("gateway", ""))),
            timeout=180)
        self.sleep(30)

        self._step(job_id, f"等 {ip}:3389 能連")
        if not self._wait(lambda: bool(driver.health(spec).get(3389)),
                          float(p.get("boot_timeout_seconds", 900))):
            raise RuntimeError(f"{ip}:3389 一直連不上，請在 VMware 打開 {name} 看畫面")

        rdp_url = None
        note = ""
        host = p.get(f"rdp_{kind}_host")
        vnet = p.get("vnet_id")
        if host and vnet:
            self._step(job_id, "Cloudflare：建立 RDP 目標")
            try:
                cf = self._cf or RdpTargets(self.cfg.raw.get("cloudflare_sync", {}))
                cf.upsert_target(name, ip, vnet)
                if not cf.add_to_app(host, name):
                    note = f"找不到 Cloudflare 應用程式 {host}，請手動把 {name} 加進它的 target hostname"
                rdp_url = f"https://{host}/rdp/{vnet}/{ip}/3389"
            except SyncError as e:
                note = f"Cloudflare 設定失敗（主機已建好）：{e}"

        self._step(job_id, "寫入設定")
        extras = load_extras(self.cfg)
        if j["bank_name"] and j["bank"] not in self.cfg.banks:
            extras["banks"].append({"id": j["bank"], "name": j["bank_name"]})
            self.cfg.banks[j["bank"]] = Bank(id=j["bank"], name=j["bank_name"])
        entry = {"name": name, "kind": kind, "vmx": vmx, "host": ip, "ports": list(spec.ports)}
        if j["bank"]:
            entry["bank"] = j["bank"]
        if rdp_url:
            entry["rdp_url"] = rdp_url
        extras["vms"] = [v for v in extras["vms"] if v["name"] != name] + [entry]
        save_extras(self.cfg, extras)
        add_vm(self.cfg, entry)

        self._step(job_id, "關機待命")
        try:
            driver.stop(spec)
        except DriverError as e:
            log.info("關機失敗（之後閒置會自己關）：%s", e)

        self.db.execute(
            "UPDATE provision_jobs SET status='done', step='完成', error=?, finished_at=? WHERE id=?",
            (note or None, self.portal.now().isoformat(timespec="seconds"), job_id))
        self.portal.audit(j["email"], "provision_done", job=job_id, name=name, ip=ip, note=note or None)
