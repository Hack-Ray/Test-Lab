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
        token = os.environ.get(self.token_env, "")
        if not token and self.token_file and os.path.exists(self.token_file):
            with open(self.token_file, encoding="utf-8-sig") as f:
                token = f.read().strip()
        if not token:
            raise SyncError(f"沒有 Cloudflare API token（環境變數 {self.token_env} 或 api_token_file）")
        return _urllib_transport(token)

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
