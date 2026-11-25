from __future__ import annotations

import pytest

from portal.cloudflare import AccessGroupSync
from portal.config import load_config
from portal.db import DB
from portal.errors import PortalError
from portal.service import Portal


def sa(portal):
    return portal.users.get("sa@example.com")


def test_seed_from_config_once(portal):
    assert len(portal.users.list()) == 4
    portal.users.delete(sa(portal), "pm@example.com")
    assert portal.users.seed_from_config() == 0  # 已有資料就不再匯入
    assert portal.users.get("pm@example.com") is None


def test_sa_manages_users(portal):
    portal.save_user(sa(portal), {"email": "New@Example.com", "role": "se", "banks": ["demo-b"]})
    u = portal.users.get("new@example.com")
    assert u.role == "SE" and u.banks == ("demo-b",)
    portal.save_user(sa(portal), {"email": "new@example.com", "role": "SE", "banks": ["demo-b"],
                                  "enabled": False})
    assert portal.users.get("new@example.com") is None  # 停用就不能登入
    actions = [a["action"] for a in portal.audit_log()]
    assert "user_create" in actions and "user_update" in actions


def test_non_sa_cannot_manage(portal):
    pg = portal.users.get("pg@example.com")
    with pytest.raises(PortalError) as e:
        portal.save_user(pg, {"email": "x@example.com", "role": "PM", "banks": []})
    assert e.value.status == 403


@pytest.mark.parametrize("data", [
    {"email": "bad", "role": "PM", "banks": []},
    {"email": "a@example.com", "role": "BOSS", "banks": []},
    {"email": "a@example.com", "role": "PM", "banks": ["real-bank"]},
])
def test_validation(portal, data):
    with pytest.raises(PortalError):
        portal.save_user(sa(portal), data)


def test_keep_last_sa(portal):
    s = sa(portal)
    with pytest.raises(PortalError):
        portal.save_user(s, {"email": "sa@example.com", "role": "PG", "banks": ["*"]})
    with pytest.raises(PortalError):
        portal.delete_user(s, "sa@example.com")
    portal.save_user(s, {"email": "sa2@example.com", "role": "SA", "banks": ["*"]})
    portal.delete_user(s, "sa@example.com")  # 有第二位 SA 就可以


def test_access_request_flow(portal):
    req = portal.users.request_access("newbie@example.com", "SE", ["demo-a"], "負責示範銀行 A")
    with pytest.raises(PortalError):
        portal.users.request_access("newbie@example.com", "SE", ["demo-a"], "再一次")
    res = portal.decide_request(sa(portal), req["id"], True, role="PM", banks=["demo-a", "demo-b"])
    assert res["request"]["status"] == "approved"
    u = portal.users.get("newbie@example.com")
    assert u.role == "PM" and set(u.banks) == {"demo-a", "demo-b"}
    with pytest.raises(PortalError):
        portal.decide_request(sa(portal), req["id"], False)


def test_access_request_rules(portal):
    with pytest.raises(PortalError):
        portal.users.request_access("pm@example.com", "PM", ["demo-a"], "x")  # 已有權限
    with pytest.raises(PortalError):
        portal.users.request_access("z@example.com", "PM", ["demo-a"], " ")  # 沒原因
    r = portal.users.request_access("z@example.com", "PM", ["demo-a"], "x")
    portal.decide_request(sa(portal), r["id"], False)
    assert portal.users.get("z@example.com") is None


# ---------------------------------------------------------------- Cloudflare 同步
class FakeCF:
    def __init__(self, fail=False):
        self.groups = {"g-users": {"name": "test-lab-users", "include": [], "exclude": [],
                                   "require": []},
                       "g-server": {"name": "test-lab-server", "include": [], "exclude": [],
                                    "require": []}}
        self.fail = fail
        self.calls = []

    def __call__(self, method, url, body):
        self.calls.append((method, url))
        gid = url.rsplit("/", 1)[1]
        assert "/accounts/acc-1/access/groups/" in url
        if self.fail:
            return {"success": False, "errors": [{"message": "boom"}]}
        if method == "PUT":
            self.groups[gid].update(body)
        return {"success": True, "result": self.groups[gid]}

    def emails(self, gid):
        return sorted(i["email"]["email"] for i in self.groups[gid]["include"])


def cf_portal(raw_cfg, driver, clock, fake):
    cfg = load_config(raw_cfg)
    sync = AccessGroupSync({"enabled": True, "account_id": "acc-1",
                            "users_group_id": "g-users", "server_group_id": "g-server"},
                           transport=fake)
    return Portal(cfg, driver, DB(":memory:"), clock, access_sync=sync)


def test_cf_sync_on_change(raw_cfg, driver, clock):
    fake = FakeCF()
    p = cf_portal(raw_cfg, driver, clock, fake)
    res = p.save_user(sa(p), {"email": "new@example.com", "role": "PM", "banks": ["demo-a"]})
    assert res["sync"]["ok"]
    assert "new@example.com" in fake.emails("g-users")
    assert "new@example.com" not in fake.emails("g-server")
    assert fake.emails("g-server") == ["pg@example.com", "sa@example.com"]
    assert fake.groups["g-users"]["name"] == "test-lab-users"  # 名稱保留

    p.save_user(sa(p), {"email": "new@example.com", "role": "PM", "banks": ["demo-a"],
                        "enabled": False})
    assert "new@example.com" not in fake.emails("g-users")


def test_cf_sync_failure_does_not_block(raw_cfg, driver, clock):
    p = cf_portal(raw_cfg, driver, clock, FakeCF(fail=True))
    res = p.save_user(sa(p), {"email": "new@example.com", "role": "PM", "banks": ["demo-a"]})
    assert res["sync"]["ok"] is False and "boom" in res["sync"]["error"]
    assert p.users.get("new@example.com") is not None
    assert p.sync_status()["last"]["ok"] is False


def test_cf_sync_disabled_by_default(portal):
    assert portal.sync_access("x")["skipped"] is True


def test_api_unregistered_user_can_request(make_client):
    c = make_client()
    h = {"X-Dev-User": "newbie@example.com"}
    assert c.get("/api/me", headers=h).status_code == 403
    w = c.get("/api/whoami", headers=h).json()
    assert w["status"] == "unregistered"
    r = c.post("/api/access-requests", json={"role": "SE", "banks": ["demo-a"], "reason": "x"},
               headers=h)
    assert r.status_code == 200, r.text
    assert c.get("/api/whoami", headers=h).json()["pending_request"]["id"] == r.json()["id"]
    reqs = c.get("/api/access-requests?status=pending").json()  # SA
    c.post(f"/api/access-requests/{reqs[0]['id']}/decision", json={"approve": True})
    assert c.get("/api/me", headers=h).json()["role"] == "SE"
    assert c.get("/api/users", headers=h).status_code == 403
