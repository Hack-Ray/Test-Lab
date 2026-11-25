from __future__ import annotations

from typing import Any

from .base import DriverError, PowerState, VMDriver, VMSpec
from .mock import MockDriver
from .vmrun import VmrunDriver

__all__ = ["DriverError", "PowerState", "VMDriver", "VMSpec", "MockDriver",
           "VmrunDriver", "create_driver"]


def create_driver(cfg: dict[str, Any]) -> VMDriver:
    """依設定檔 [driver] 區段建立 driver。要加 vSphere 時在這裡多一個分支。"""
    kind = cfg.get("type", "mock")
    if kind == "mock":
        return MockDriver(boot_seconds=float(cfg.get("mock_boot_seconds", 5)))
    if kind == "vmrun":
        kwargs = {}
        if cfg.get("vmrun_path"):
            kwargs["vmrun_path"] = cfg["vmrun_path"]
        if cfg.get("host_type"):
            kwargs["host_type"] = cfg["host_type"]
        return VmrunDriver(**kwargs)
    raise ValueError(f"未知的 driver 類型：{kind}（目前支援 mock、vmrun）")
