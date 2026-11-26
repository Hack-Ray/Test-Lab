"""把 Portal 的使用者名單同步到 Cloudflare Access Group。

兩個群組（都要先在 Zero Trust 手動建好，把 ID 填進設定檔）：
  users_group_id   所有啟用中的使用者 → 給 Portal 和 client 的 Access 應用程式用
  server_group_id  啟用中的 PG / SA    → 給 server 的 Access 應用程式用

這兩個群組的 include 清單由 Portal 管理，每次同步會整個覆蓋，不要手動改。
API token 從環境變數讀（預設 CF_API_TOKEN），權限只需要
Account → Access: Organizations, Identity Providers, and Groups → Edit。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

API = "https://api.cloudflare.com/client/v4"

Transport = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


class SyncError(RuntimeError):
    pass


def _urllib_transport(token: str) -> Transport:
    def call(method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise SyncError(f"Cloudflare API {e.code}：{detail}") from e
        except urllib.error.URLError as e:
            raise SyncError(f"連不到 Cloudflare API：{e.reason}") from e
    return call


class AccessGroupSync:
    def __init__(self, cfg: dict[str, Any], transport: Transport | None = None):
        self.enabled = bool(cfg.get("enabled", False))
        self.account_id = cfg.get("account_id", "")
        self.groups = {
            "users": cfg.get("users_group_id", ""),
            "server": cfg.get("server_group_id", ""),
        }
        self._transport = transport
        self.token_env = cfg.get("api_token_env", "CF_API_TOKEN")
        self.token_file = cfg.get("api_token_file", "")  # 一鍵部署會把 token 放在只有本人能讀的檔案

    def _call(self) -> Transport:
        if self._transport:
            return self._transport
        return token_transport({"api_token_env": self.token_env, "api_token_file": self.token_file})

    def check_config(self) -> None:
        missing = [k for k, v in (("account_id", self.account_id),
                                  ("users_group_id", self.groups["users"]),
                                  ("server_group_id", self.groups["server"])) if not v]
        if missing:
            raise SyncError(f"cloudflare_sync 缺少設定：{', '.join(missing)}")

    def _put_group(self, call: Transport, group_id: str, emails: list[str]) -> None:
        url = f"{API}/accounts/{self.account_id}/access/groups/{group_id}"
        current = call("GET", url, None)
        if not current.get("success"):
            raise SyncError(f"讀取群組 {group_id} 失敗：{current.get('errors')}")
        g = current["result"]
        if not emails:
            # Cloudflare 的群組至少要有一條 include；放一個不存在的網域讓群組等於空的
            include = [{"email_domain": {"domain": "nobody.invalid"}}]
        else:
            include = [{"email": {"email": e}} for e in emails]
        body = {"name": g["name"], "include": include,
                "exclude": g.get("exclude") or [], "require": g.get("require") or []}
        res = call("PUT", url, body)
        if not res.get("success"):
            raise SyncError(f"更新群組 {g['name']} 失敗：{res.get('errors')}")

    def sync(self, users: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """users：啟用中的使用者（需要 email、role）。回傳各群組的人數。"""
        if not self.enabled:
            return {"skipped": True}
        self.check_config()
        call = self._call()
        users = list(users)
        all_emails = sorted({u["email"] for u in users})
        server_emails = sorted({u["email"] for u in users if u["role"] in ("PG", "SA")})
        self._put_group(call, self.groups["users"], all_emails)
        self._put_group(call, self.groups["server"], server_emails)
        return {"users": len(all_emails), "server": len(server_emails)}


def token_transport(cfg: dict[str, Any]) -> Transport:
    """從 [cloudflare_sync] 的 api_token_env / api_token_file 讀 token。"""
    env = cfg.get("api_token_env", "CF_API_TOKEN")
    token = os.environ.get(env, "")
    path = cfg.get("api_token_file", "")
    if not token and path and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            token = f.read().strip()
    if not token:
        raise SyncError(f"沒有 Cloudflare API token（環境變數 {env} 或 api_token_file）")
    return _urllib_transport(token)


class RdpTargets:
    """新增主機時：建 Cloudflare 的 RDP 目標，並把它加進瀏覽器 RDP 應用程式的目標清單。"""

    READ_ONLY = ("id", "uid", "aud", "created_at", "updated_at", "scim_config")

    def __init__(self, cfg: dict[str, Any], transport: Transport | None = None):
        self.cfg = cfg
        self.account_id = cfg.get("account_id", "")
        self._transport = transport

    def _call(self) -> Transport:
        return self._transport or token_transport(self.cfg)

    def _ok(self, res: dict[str, Any], what: str) -> Any:
        if not res.get("success"):
            raise SyncError(f"{what}失敗：{res.get('errors')}")
        return res.get("result")

    def upsert_target(self, hostname: str, ip: str, vnet_id: str) -> None:
        call = self._call()
        base = f"{API}/accounts/{self.account_id}/infrastructure/targets"
        found = [t for t in (self._ok(call("GET", f"{base}?hostname={hostname}", None), "查 RDP 目標") or [])
                 if t.get("hostname") == hostname]
        body = {"hostname": hostname, "ip": {"ipv4": {"ip_addr": ip, "virtual_network_id": vnet_id}}}
        if found:
            self._ok(call("PUT", f"{base}/{found[0]['id']}", body), "更新 RDP 目標")
        else:
            self._ok(call("POST", base, body), "建立 RDP 目標")

    def add_to_app(self, app_host: str, hostname: str) -> bool:
        """回傳 False 表示找不到應用程式（要手動加）。"""
        call = self._call()
        base = f"{API}/accounts/{self.account_id}/access/apps"
        apps = self._ok(call("GET", f"{base}?per_page=100", None), "查 Access 應用程式") or []
        app = next((a for a in apps if a.get("domain") == app_host), None)
        if app is None:
            return False
        crit = app.get("target_criteria") or [{"port": 3389, "protocol": "RDP", "target_attributes": {}}]
        names = list(crit[0].setdefault("target_attributes", {}).get("hostname", []))
        if hostname in names:
            return True
        crit[0]["target_attributes"]["hostname"] = names + [hostname]
        body = {k: v for k, v in app.items() if k not in self.READ_ONLY}
        body["target_criteria"] = crit
        body["policies"] = [{"id": p["id"], "precedence": p.get("precedence", i + 1)}
                            for i, p in enumerate(app.get("policies") or []) if isinstance(p, dict) and p.get("id")]
        self._ok(call("PUT", f"{base}/{app['id']}", body), "更新瀏覽器 RDP 應用程式")
        return True
