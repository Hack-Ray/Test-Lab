from __future__ import annotations

import json
from datetime import timedelta

import pytest

from portal.config import load_config
from portal.db import DB
from portal.errors import PortalError
from portal.provision import Provisioner
from portal.service import Portal


class FakeCf:
    def __init__(self, has_app: bool = True):
        self.targets: list[tuple[str, str, str]] = []
        self.app_hosts: list[tuple[str, str]] = []
        self.has_app = has_app

    def upsert_target(self, hostname, ip, vnet_id):
        self.targets.append((hostname, ip, vnet_id))

    def add_to_app(self, app_host, hostname):
        self.app_hosts.append((app_host, hostname))
        return self.has_app


@pytest.fixture
def setup(raw_cfg, driver, clock, tmp_path):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"AdminPassword": "Adm-1", "TesterPassword": "T-1"}), encoding="utf-8")
    raw = dict(raw_cfg)
    raw["portal"] = {**raw_cfg["portal"], "extra_file": str(tmp_path / "extra.json")}
    raw["rdp"] = {"username": "tester", "secrets_file": str(secrets)}
    raw["provision"] = {"vm_root": str(tmp_path / "VMs"), "ip_prefix": "192.168.18", "ip_range": [10, 99],
                        "gateway": "192.168.18.2", "vnet_id": "vnet-1",
                        "rdp_client_host": "lab-rdp.example.com", "rdp_server_host": "lab-rdp-srv.example.com"}
    portal = Portal(load_config(raw), driver, DB(":memory:"), clock)
    cf = FakeCf()

    def sleep(s):
        clock.dt += timedelta(seconds=s)
    prov = Provisioner(portal, sleep=sleep, cf=cf, run_async=False)
    return portal, prov, cf, raw, tmp_path


def test_provision_client_end_to_end(setup, driver):
    portal, prov, cf, raw, tmp = setup
    job = prov.request("sa@example.com", "cli-03", "client", None, None, [3389])
    assert job["status"] == "done", job
    assert job["ip"] == "192.168.18.10"
    assert ("clone", "cli-01->cli-03") in driver.calls
    # 在 VM 裡改了 IP 和名稱
    scripts = [s for (vm, user, s) in driver.guest_scripts if vm == "cli-03" and "New-NetIPAddress" in s]
    assert scripts and "'192.168.18.10'" in scripts[0] and "'CLI-03'" in scripts[0]
    assert cf.targets == [("cli-03", "192.168.18.10", "vnet-1")]
    assert cf.app_hosts == [("lab-rdp.example.com", "cli-03")]
    vm = portal.cfg.vms["cli-03"]
    assert vm.rdp_url == "https://lab-rdp.example.com/rdp/vnet-1/192.168.18.10/3389"
    # 寫進 extra 檔，重新載入設定也看得到
    again = load_config(raw)
    assert "cli-03" in again.vms and again.vms["cli-03"].host == "192.168.18.10"
    # 建完關機待命
    assert ("stop", "cli-03") in driver.calls


def test_provision_server_with_new_bank(setup):
    portal, prov, cf, raw, tmp = setup
    job = prov.request("sa@example.com", "srv-demo-c", "server", "demo-c", "示範銀行 C", [3389, 8080])
    assert job["status"] == "done", job
    assert portal.cfg.banks["demo-c"].name == "示範銀行 C"
    assert portal.cfg.server_for("demo-c").name == "srv-demo-c"
    assert portal.cfg.vms["srv-demo-c"].ports == (3389, 8080)
    assert load_config(raw).banks["demo-c"].name == "示範銀行 C"
    assert cf.app_hosts == [("lab-rdp-srv.example.com", "srv-demo-c")]


def test_provision_validation(setup):
    portal, prov, cf, raw, tmp = setup
    with pytest.raises(PortalError):
        prov.request("sa@example.com", "CLI 03", "client", None, None, [])      # 名稱不合法
    with pytest.raises(PortalError):
        prov.request("sa@example.com", "cli-01", "client", None, None, [])      # 已存在
    with pytest.raises(PortalError):
        prov.request("sa@example.com", "srv-x", "server", "demo-a", None, [])   # 已經有 server
    with pytest.raises(PortalError):
        prov.request("sa@example.com", "srv-x", "server", None, None, [])       # server 要銀行


def test_ip_skips_used(setup):
    portal, prov, cf, raw, tmp = setup
    portal.cfg.vms["cli-01"] = portal.cfg.vms["cli-01"].__class__(
        name="cli-01", kind="client", vmx="x", host="192.168.18.10")
    assert prov.next_ip() == "192.168.18.11"


def test_cloudflare_app_missing_is_a_note_not_failure(setup):
    portal, prov, cf, raw, tmp = setup
    cf.has_app = False
    job = prov.request("sa@example.com", "cli-04", "client", None, None, [])
    assert job["status"] == "done"
    assert "手動" in (job["error"] or "")
