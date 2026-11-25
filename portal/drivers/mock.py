"""MockDriver：沒有 VM 也能開發與測試。

開機後經過 boot_seconds 秒才「健康」，模擬 Windows 開機時間。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .base import PowerState, VMDriver, VMSpec


class MockDriver(VMDriver):
    name = "mock"

    def __init__(self, boot_seconds: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.boot_seconds = boot_seconds
        self.clock = clock
        self._started_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []  # 測試用：記錄呼叫

    def start(self, vm: VMSpec) -> None:
        with self._lock:
            self.calls.append(("start", vm.name))
            self._started_at.setdefault(vm.name, self.clock())

    def stop(self, vm: VMSpec) -> None:
        with self._lock:
            self.calls.append(("stop", vm.name))
            self._started_at.pop(vm.name, None)

    def status(self, vm: VMSpec) -> PowerState:
        return PowerState.RUNNING if vm.name in self._started_at else PowerState.STOPPED

    def ip(self, vm: VMSpec) -> str | None:
        if vm.name not in self._started_at:
            return None
        return vm.host or f"10.0.0.{abs(hash(vm.name)) % 200 + 10}"

    def health(self, vm: VMSpec, timeout: float = 2.0) -> dict[int, bool]:
        started = self._started_at.get(vm.name)
        ok = started is not None and self.clock() - started >= self.boot_seconds
        return {p: ok for p in vm.ports}

    # 測試用：模擬 Windows 閒置自動關機
    def simulate_guest_shutdown(self, name: str) -> None:
        with self._lock:
            self._started_at.pop(name, None)
