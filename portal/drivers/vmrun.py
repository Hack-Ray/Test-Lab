"""VmrunDriver：用 VMware Workstation Pro 的 vmrun.exe 控制 VM。

用到的指令：
  vmrun -T ws start  <vmx> nogui
  vmrun -T ws stop   <vmx> soft
  vmrun -T ws list
  vmrun -T ws getGuestIPAddress <vmx>      （需要 VM 內裝 VMware Tools）
"""
from __future__ import annotations

from typing import Sequence

import os
import subprocess
import tempfile
import threading
import time
import uuid

from .base import DriverError, PowerState, VMDriver, VMSpec

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

    def run_program(self, vm: VMSpec, user: str, password: str, program: str,
                    args: Sequence[str] = (), timeout: float = 120) -> None:
        if isinstance(args, str):
            raise TypeError("args 要用 list，每個參數分開")
        self._run("-gu", user, "-gp", password, "runProgramInGuest", self._vmx(vm), program, *args,
                  timeout=timeout)

    def _guest(self, vm: VMSpec, user: str, password: str, *args: str, timeout: float = 120) -> str:
        return self._run("-gu", user, "-gp", password, args[0], self._vmx(vm), *args[1:], timeout=timeout)

    def run_in_guest(self, vm: VMSpec, user: str, password: str, script: str,
                     timeout: float = 300) -> None:
        """
        VMware Tools 直接啟動的 PowerShell 沒有載入使用者設定檔，會直接失敗（exit 1），
        所以把腳本複製進 VM，用排程工作以 SYSTEM 身分執行，等它寫出完成檔。
        """
        tag = f"testlab-{uuid.uuid4().hex[:8]}"
        g = rf"C:\Windows\Temp\{tag}"
        wrapped = (
            "$ErrorActionPreference = 'Stop'\n"
            f"try {{\n{script}\n'0' | Set-Content '{g}.done' }}\n"
            f"catch {{ $_ | Out-String | Set-Content '{g}.err'; '1' | Set-Content '{g}.done' }}\n"
        )
        # 排程工作的 /tr 只放一個沒有空白的 .cmd 路徑，避開 vmrun 傳參數的引號問題
        launcher = f"@powershell.exe -NoProfile -ExecutionPolicy Bypass -File {g}.ps1\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            host_ps1 = os.path.join(tmp, "s.ps1")
            with open(host_ps1, "w", encoding="utf-8-sig") as f:
                f.write(wrapped)
            host_cmd = os.path.join(tmp, "s.cmd")
            with open(host_cmd, "w", encoding="ascii", newline="") as f:
                f.write(launcher)
            self._guest(vm, user, password, "copyFileFromHostToGuest", host_ps1, g + ".ps1")
            self._guest(vm, user, password, "copyFileFromHostToGuest", host_cmd, g + ".cmd")
            schtasks = r"C:\Windows\System32\schtasks.exe"
            self._guest(vm, user, password, "runProgramInGuest", schtasks,
                        "/create", "/tn", tag, "/ru", "SYSTEM", "/sc", "once", "/st", "00:00",
                        "/rl", "highest", "/f", "/tr", g + ".cmd")
            try:
                self._guest(vm, user, password, "runProgramInGuest", schtasks, "/run", "/tn", tag)
                deadline = time.monotonic() + timeout
                while True:
                    out = self._guest(vm, user, password, "fileExistsInGuest", g + ".done", timeout=30)
                    if "not" not in out.lower():
                        break
                    if time.monotonic() > deadline:
                        raise DriverError(f"VM 裡的腳本 {timeout:.0f} 秒內沒跑完")
                    time.sleep(3)
                done = os.path.join(tmp, "done")
                self._guest(vm, user, password, "copyFileFromGuestToHost", g + ".done", done)
                with open(done, encoding="utf-8-sig", errors="replace") as f:
                    ok = f.read().strip().startswith("0")
                if not ok:
                    err = os.path.join(tmp, "err")
                    msg = ""
                    try:
                        self._guest(vm, user, password, "copyFileFromGuestToHost", g + ".err", err)
                        with open(err, encoding="utf-8-sig", errors="replace") as f:
                            msg = f.read().strip()[:500]
                    except DriverError:
                        pass
                    raise DriverError(f"VM 裡的腳本失敗：{msg}")
            finally:
                for cleanup in (("runProgramInGuest", schtasks, "/delete", "/tn", tag, "/f"),
                                ("deleteFileInGuest", g + ".ps1"), ("deleteFileInGuest", g + ".cmd"), ("deleteFileInGuest", g + ".done"),
                                ("deleteFileInGuest", g + ".err")):
                    try:
                        self._guest(vm, user, password, *cleanup, timeout=30)
                    except DriverError:
                        pass

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
