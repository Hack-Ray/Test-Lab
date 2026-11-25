from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from portal.auth import Authenticator
from portal.config import load_config

TEAM = "demo-team.cloudflareaccess.com"
AUD = "test-aud"


@pytest.fixture(scope="module")
def keys():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, other


def token(priv, email="pm@example.com", aud=AUD, iss=f"https://{TEAM}", exp_in=300):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in},
                      priv, algorithm="RS256")


@pytest.fixture
def cf_client(make_client, raw_cfg, keys):
    priv, _ = keys
    raw = dict(raw_cfg)
    raw["auth"] = {**raw_cfg["auth"], "mode": "cloudflare", "team_domain": TEAM, "audience": AUD}
    authn = Authenticator(load_config(raw), key_resolver=lambda _t: priv.public_key())
    return make_client(auth={"mode": "cloudflare", "team_domain": TEAM, "audience": AUD},
                       authenticator=authn)


def test_dev_mode_switch_user(make_client):
    c = make_client()
    assert c.get("/api/me").json()["email"] == "sa@example.com"
    r = c.get("/api/me", headers={"X-Dev-User": "pm@example.com"}).json()
    assert r["role"] == "PM" and r["can_server"] is False


def test_unknown_user_rejected(make_client):
    c = make_client()
    r = c.get("/api/me", headers={"X-Dev-User": "nobody@example.com"})
    assert r.status_code == 403


def test_cf_valid_jwt(cf_client, keys):
    r = cf_client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token(keys[0])})
    assert r.status_code == 200 and r.json()["email"] == "pm@example.com"


def test_cf_email_header_alone_not_trusted(cf_client):
    r = cf_client.get("/api/me", headers={"Cf-Access-Authenticated-User-Email": "sa@example.com",
                                          "X-Dev-User": "sa@example.com"})
    assert r.status_code == 401


@pytest.mark.parametrize("kw", [
    {"aud": "wrong"}, {"iss": "https://evil.example.com"}, {"exp_in": -10},
])
def test_cf_bad_claims(cf_client, keys, kw):
    r = cf_client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token(keys[0], **kw)})
    assert r.status_code == 401


def test_cf_wrong_signature(cf_client, keys):
    r = cf_client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token(keys[1])})
    assert r.status_code == 401


def test_api_flow(make_client, clock):
    c = make_client()
    pm = {"X-Dev-User": "pm@example.com"}
    r = c.post("/api/wake", json={"bank": "demo-a", "kind": "server"}, headers=pm)
    assert r.status_code == 403
    r = c.post("/api/wake", json={"bank": "demo-a", "kind": "client"}, headers=pm)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert c.get("/api/vms", headers=pm).status_code == 403
    assert len(c.get("/api/allocations?all=true").json()) == 1  # SA 看全部
    assert c.get("/api/audit").status_code == 200
    r = c.post(f"/api/allocations/{aid}/release", headers=pm)
    assert r.json()["status"] == "released"


def test_index_served(make_client):
    r = make_client().get("/")
    assert r.status_code == 200 and "測試環境 Portal" in r.text


def test_dev_mode_blocks_requests_via_cloudflare(make_client):
    r = make_client().get("/api/me", headers={"Cf-Ray": "abc123-TPE"})
    assert r.status_code == 403
