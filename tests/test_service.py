from __future__ import annotations

import pytest

from portal.service import PortalError


def u(portal, email):
    return portal.cfg.users[email]


# ---------------------------------------------------------------- 權限
def test_pm_cannot_wake_server(portal):
    with pytest.raises(PortalError) as e:
        portal.wake(u(portal, "pm@example.com"), "demo-a", "server")
    assert e.value.status == 403
    assert portal.audit_log()[0]["action"] == "denied"


def test_bank_permission(portal):
    with pytest.raises(PortalError) as e:
        portal.wake(u(portal, "pm@example.com"), "demo-b", "client")
    assert e.value.status == 403


def test_sa_wildcard_bank(portal):
    a = portal.wake(u(portal, "sa@example.com"), "demo-b", "server")
    assert a["vm"] == "srv-demo-b"


# ---------------------------------------------------------------- 開放時段
def test_closed_on_weekend(portal, clock):
    clock.set(2026, 11, 28, 10, 0)  # 週六
    with pytest.raises(PortalError) as e:
        portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    assert "開放" in e.value.message


def test_closed_after_hours_then_extend(portal, clock):
    clock.set(2026, 11, 25, 17, 0)
    pg = u(portal, "pg@example.com")
    portal.extend_today(pg, "19:00", "客戶要求今晚驗證")
    clock.set(2026, 11, 25, 18, 30)
    assert portal.open_window().is_open
    portal.wake(pg, "demo-a", "client")
    clock.set(2026, 11, 25, 19, 0)
    assert not portal.open_window().is_open


def test_extend_rules(portal, clock):
    clock.set(2026, 11, 25, 17, 0)
    with pytest.raises(PortalError):
        portal.extend_today(u(portal, "se@example.com"), "19:00", "x")  # 角色不對
    pg = u(portal, "pg@example.com")
    with pytest.raises(PortalError):
        portal.extend_today(pg, "19:00", "  ")  # 沒原因
    with pytest.raises(PortalError):
        portal.extend_today(pg, "23:00", "x")  # 超過上限
    with pytest.raises(PortalError):
        portal.extend_today(pg, "17:00", "x")  # 沒晚於收工
    assert portal.extensions() == []


def test_extension_only_today(portal, clock):
    clock.set(2026, 11, 25, 17, 0)
    portal.extend_today(u(portal, "pg@example.com"), "20:00", "x")
    clock.set(2026, 11, 26, 18, 0)
    assert not portal.open_window().is_open


# ---------------------------------------------------------------- 喚醒與分配
def test_wake_client_starts_server_and_client(portal, driver, clock):
    a = portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    assert a["status"] == "starting"
    assert ("start", "srv-demo-a") in driver.calls
    assert ("start", a["vm"]) in driver.calls
    clock.set(2026, 11, 25, 10, 2)
    portal.process_pending()
    a = portal.allocation(a["id"])
    assert a["status"] == "ready"
    assert a["rdp_url"].startswith("https://")


def test_one_client_per_person(portal):
    pm = u(portal, "pm@example.com")
    a1 = portal.wake(pm, "demo-a", "client")
    a2 = portal.wake(pm, "demo-a", "client")
    assert a1["id"] == a2["id"]


def test_switch_bank_keeps_same_client(portal):
    se = u(portal, "se@example.com")
    a1 = portal.wake(se, "demo-a", "client")
    a2 = portal.wake(se, "demo-b", "client")
    assert a2["vm"] == a1["vm"] and a2["bank"] == "demo-b"
    assert portal.allocation(a1["id"])["status"] == "released"


def test_no_free_client(portal):
    portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    portal.wake(u(portal, "se@example.com"), "demo-a", "client")
    with pytest.raises(PortalError) as e:
        portal.wake(u(portal, "pg@example.com"), "demo-a", "client")
    assert e.value.status == 503


def test_max_running_cap(portal):
    sa = u(portal, "sa@example.com")
    portal.update_prewarm(sa, {"max_running": 1})
    portal.wake(sa, "demo-a", "server")  # 第 1 台
    with pytest.raises(PortalError) as e:
        portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    assert e.value.status == 503


def test_prefers_prewarmed_client(portal, driver):
    driver.start(portal.cfg.vms["cli-02"])
    a = portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    assert a["vm"] == "cli-02"


def test_health_timeout_marks_failed(portal, clock, driver):
    driver.boot_seconds = 10_000
    a = portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    clock.set(2026, 11, 25, 10, 11)
    portal.process_pending()
    assert portal.allocation(a["id"])["status"] == "failed"


def test_release_own_only(portal):
    a = portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    with pytest.raises(PortalError):
        portal.release(u(portal, "se@example.com"), a["id"])
    assert portal.release(u(portal, "pm@example.com"), a["id"])["status"] == "released"


# ---------------------------------------------------------------- reconcile
def test_reconcile_prewarm_and_auto_release(portal, driver, clock):
    clock.set(2026, 11, 25, 8, 0)
    r = portal.reconcile()
    assert r["prewarm_window"] and len(r["prewarmed"]) == 1
    r = portal.reconcile()
    assert r["prewarmed"] == []  # 已有一台待命

    a = portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    clock.set(2026, 11, 25, 8, 5)
    portal.process_pending()
    assert portal.allocation(a["id"])["status"] == "ready"
    driver.simulate_guest_shutdown(a["vm"])  # Windows 閒置自己關機
    r = portal.reconcile()
    assert a["id"] in r["auto_released"]


def test_prewarm_outside_window(portal, clock):
    clock.set(2026, 11, 25, 20, 0)
    assert portal.reconcile()["prewarmed"] == []


def test_prewarm_settings_sa_only(portal):
    with pytest.raises(PortalError):
        portal.update_prewarm(u(portal, "pg@example.com"), {"standby_clients": 2})
    s = portal.update_prewarm(u(portal, "sa@example.com"), {"standby_clients": 2})
    assert s["standby_clients"] == 2


def test_after_hours_release(portal, clock):
    a = portal.wake(u(portal, "pm@example.com"), "demo-a", "client")
    clock.set(2026, 11, 25, 17, 0)
    assert portal.reconcile()["after_hours_released"] == []
    clock.set(2026, 11, 25, 17, 31)
    r = portal.reconcile()
    assert r["after_hours_released"] == [a["id"]]
    assert portal.allocation(a["id"])["note"] == "after_hours"


def test_extension_delays_after_hours_release(portal, clock):
    clock.set(2026, 11, 25, 17, 0)
    pg = u(portal, "pg@example.com")
    a = portal.wake(pg, "demo-a", "client")
    portal.extend_today(pg, "19:00", "加班驗證")
    clock.set(2026, 11, 25, 18, 0)
    assert portal.reconcile()["after_hours_released"] == []
    clock.set(2026, 11, 25, 19, 0)
    assert portal.reconcile()["after_hours_released"] == [a["id"]]
