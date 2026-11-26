"""VM driver 介面。

Portal 只透過這個介面開關機、查狀態，所以換平台（VMware Workstation → vSphere）
時只要多寫一個 driver，Portal 其他部分不用動。
"""
from __future__ import annotations

from typing import Sequence

import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


NET_EXE = r"C:\Windows\System32\net.exe"


class PowerState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VMSpec:
    """設定檔裡的一台 VM。driver 需要的欄位都放這裡。"""

    name: str
    kind: str  # "client" | "server"
    bank: str | None = None  # server 必填；client 為 None 表示共用池
    vmx: str | None = None  # VmrunDriver 用：.vmx 路徑
    vsphere_id: str | None = None  # 之後 vSphere driver 用（moref 或 inventory path）
    host: str | None = None  # 固定 IP/主機名；沒填就問 driver
    ports: tuple[int, ...] = field(default_factory=lambda: (3389,))
    rdp_url: str | None = None  # Cloudflare 瀏覽器 RDP 的網址


class VMDriver(ABC):
    """所有 driver 都要實作 start / stop / status / ip，health 有預設實作。"""

    name = "base"

    @abstractmethod
    def start(self, vm: VMSpec) -> None:
        """開機（已開機時不應報錯）。"""

    @abstractmethod
    def stop(self, vm: VMSpec) -> None:
        """關機（盡量用 soft/guest shutdown）。"""

    @abstractmethod
    def status(self, vm: VMSpec) -> PowerState:
        """回傳電源狀態。"""

    @abstractmethod
    def ip(self, vm: VMSpec) -> str | None:
        """回傳 VM 的 IP；還拿不到時回 None。"""

    # ---- 選用功能：不支援的 driver 丟 DriverError ----
    def run_program(self, vm: VMSpec, user: str, password: str, program: str,
                    args: Sequence[str] = (), timeout: float = 120) -> None:
        """用 VM 裡的帳號直接執行一支程式（需要 guest tools）。

        args 每個參數分開給，不要合成一個字串：vmrun 會把整串當成一個參數傳進 VM，
        net.exe 之類的程式就會失敗（exit 1）。參數裡不能有空白。"""
        raise DriverError(f"{self.name} driver 不支援在 VM 裡執行程式")

    def run_in_guest(self, vm: VMSpec, user: str, password: str, script: str,
                     timeout: float = 300) -> None:
        """在 VM 裡以 SYSTEM 執行一段 PowerShell（user/password 用來登入 guest tools）。"""
        raise DriverError(f"{self.name} driver 不支援在 VM 裡執行程式")

    def clone(self, src: VMSpec, snapshot: str, dst_path: str, name: str, linked: bool = True) -> str:
        """從 src 的快照複製出一台新 VM，回傳新 VM 的識別（vmx 路徑等）。"""
        raise DriverError(f"{self.name} driver 不支援複製 VM")

    def set_guest_password(self, vm: VMSpec, admin_user: str, admin_password: str,
                           user: str, new_password: str) -> None:
        """在 VM 裡改本機帳號密碼（一次性密碼用）。直接用 net.exe，不經過 PowerShell。"""
        if any(c in new_password + user for c in ' "'):
            raise DriverError("帳號和密碼不能有空白或雙引號")
        self.run_program(vm, admin_user, admin_password, NET_EXE, ["user", user, new_password], timeout=60)

    def health(self, vm: VMSpec, timeout: float = 2.0) -> dict[int, bool]:
        """對 vm.ports 逐一做 TCP 連線檢查（3389 / AP / SQL）。"""
        host = vm.host or self.ip(vm)
        if not host:
            return {p: False for p in vm.ports}
        return {p: tcp_check(host, p, timeout) for p in vm.ports}


def ps_literal(s: str) -> str:
    """PowerShell 單引號字串。"""
    return "'" + s.replace("'", "''") + "'"


def tcp_check(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DriverError(RuntimeError):
    pass
