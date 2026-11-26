"""VmrunDriver：用 VMware Workstation Pro 的 vmrun.exe 控制 VM。

用到的指令：
  vmrun -T ws start  <vmx> nogui
  vmrun -T ws stop   <vmx> soft
  vmrun -T ws list
  vmrun -T ws getGuestIPAddress <vmx>      （需要 VM 內裝 VMware Tools）
"""
from __future__ import annotations

import os
import base64
import subprocess
import threading
import time

from .base import DriverError, PowerState, VMDriver, VMSpec

POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
DEFAULT_VMRUN = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.strip()))


class VmrunDriver(VMDriver):
    name = "vmrun"

    def __init__(self, vmrun_path: str = DEFAULT_VMRUN, host_type: str = "ws",
                 timeout: float = 120.0, list_cache_seconds: float = 3.0):
        self.vmrun_path = vmrun_path
        self.host_type = host_type
        self.timeout = timeout
        self.list_cache_seconds = list_cache_seconds
        self._list_cache: tuple[float, set[str]] | None = None
        self._lock = threading.Lock()

    # ---- 低階 ----
    def _run(self, *args: str, timeout: float | None = None) -> str:
        cmd = [self.vmrun_path, "-T", self.host_type, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout or self.timeout,
                                  stdin=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as e:
            raise DriverError(f"找不到 vmrun：{self.vmrun_path}") from e
        except subprocess.TimeoutExpired as e:
            raise DriverError(f"vmrun 逾時：{' '.join(args)}") from e
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise DriverError(f"vmrun {' '.join(args)} 失敗：{out.strip()}")
        return proc.stdout or ""

    def _vmx(self, vm: VMSpec) -> str:
        if not vm.vmx:
            raise DriverError(f"VM {vm.name} 沒有設定 vmx 路徑")
        return vm.vmx

    def _running_set(self) -> set[str]:
        with self._lock:
            now = time.monotonic()
            if self._list_cache and now - self._list_cache[0] < self.list_cache_seconds:
                return self._list_cache[1]
            out = self._run("list", timeout=30)
            # 第一行是 "Total running VMs: N"
            running = {_norm(line) for line in out.splitlines()[1:] if line.strip()}
            self._list_cache = (now, running)
            return running

    def _invalidate(self) -> None:
        with self._lock:
            self._list_cache = None

    # ---- 介面 ----
    def start(self, vm: VMSpec) -> None:
        if self.status(vm) == PowerState.RUNNING:
            return
        self._run("start", self._vmx(vm), "nogui")
        self._invalidate()

    def stop(self, vm: VMSpec) -> None:
        if self.status(vm) == PowerState.STOPPED:
            return
        self._run("stop", self._vmx(vm), "soft")
        self._invalidate()

    def run_in_guest(self, vm: VMSpec, user: str, password: str, script: str,
                     timeout: float = 300) -> None:
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        self._run("-gu", user, "-gp", password, "runProgramInGuest", self._vmx(vm), POWERSHELL,
                  f"-NoProfile -ExecutionPolicy Bypass -EncodedCommand {enc}", timeout=timeout)

    def clone(self, src: VMSpec, snapshot: str, dst_path: str, name: str, linked: bool = True) -> str:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        self._run("clone", self._vmx(src), dst_path, "linked" if linked else "full",
                  f"-snapshot={snapshot}", f"-cloneName={name}", timeout=3600)
        return dst_path

    def status(self, vm: VMSpec) -> PowerState:
        try:
            running = self._running_set()
        except DriverError:
            return PowerState.UNKNOWN
        return PowerState.RUNNING if _norm(self._vmx(vm)) in running else PowerState.STOPPED

    def ip(self, vm: VMSpec) -> str | None:
        if vm.host:
            return vm.host
        try:
            out = self._run("getGuestIPAddress", self._vmx(vm), timeout=15).strip()
        except DriverError:
            return None
        return out or None
