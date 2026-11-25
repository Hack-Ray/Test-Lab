from __future__ import annotations

import subprocess
from types import SimpleNamespace

from portal.drivers import PowerState, VMSpec, VmrunDriver

VM = VMSpec(name="cli-01", kind="client", vmx=r"D:\VMs\cli-01\cli-01.vmx")


def fake_run(running: list[str], log: list[list[str]]):
    def run(cmd, capture_output, text, timeout):
        log.append(cmd)
        sub = cmd[3]
        if sub == "list":
            out = f"Total running VMs: {len(running)}\n" + "".join(p + "\n" for p in running)
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if sub == "start":
            running.append(cmd[4])
        if sub == "getGuestIPAddress":
            return SimpleNamespace(returncode=0, stdout="192.168.50.11\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_vmrun_commands(monkeypatch):
    running: list[str] = []
    log: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", fake_run(running, log))
    d = VmrunDriver(vmrun_path="vmrun.exe", list_cache_seconds=0)
    assert d.status(VM) == PowerState.STOPPED
    d.start(VM)
    assert ["vmrun.exe", "-T", "ws", "start", VM.vmx, "nogui"] in log
    assert d.status(VM) == PowerState.RUNNING
    d.start(VM)  # 已開機不會再送 start
    assert sum(1 for c in log if c[3] == "start") == 1
    assert d.ip(VM) == "192.168.50.11"
    d.stop(VM)
    assert ["vmrun.exe", "-T", "ws", "stop", VM.vmx, "soft"] in log


def test_vmrun_missing_binary():
    d = VmrunDriver(vmrun_path="/nonexistent/vmrun")
    assert d.status(VM) == PowerState.UNKNOWN
