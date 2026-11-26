"""VM driver 介面。

Portal 只透過這個介面開關機、查狀態，所以換平台（VMware Workstation → vSphere）
時只要多寫一個 driver，Portal 其他部分不用動。
"""
from __future__ import annotations

import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


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

    def set_guest_password(self, vm: VMSpec, admin_user: str, admin_password: str,
                           user: str, new_password: str) -> None:
        """在 VM 裡改本機帳號密碼（一次性密碼用）。不支援的 driver 丟 DriverError。"""
        raise DriverError(f"{self.name} driver 不支援改 VM 密碼")

    def health(self, vm: VMSpec, timeout: float = 2.0) -> dict[int, bool]:
        """對 vm.ports 逐一做 TCP 連線檢查（3389 / AP / SQL）。"""
        host = vm.host or self.ip(vm)
        if not host:
            return {p: False for p in vm.ports}
        return {p: tcp_check(host, p, timeout) for p in vm.ports}


def tcp_check(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DriverError(RuntimeError):
    pass
