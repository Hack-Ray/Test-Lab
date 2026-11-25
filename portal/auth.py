"""身分驗證。

cloudflare 模式：驗證 Cloudflare Access 放在 request 上的 Cf-Access-Jwt-Assertion
（RS256、簽章用 team 的公開金鑰、檢查 aud 和 iss），從 JWT 取 email 再查權限表。
不信任 Cf-Access-Authenticated-User-Email 這類純文字 header，因為只要繞過 Access
直連 Portal 就能偽造。

dev 模式：本機開發用，不驗證，用 X-Dev-User header / ?as= / dev_user cookie 指定身分。
"""
from __future__ import annotations

from typing import Any, Callable

import jwt
from fastapi import Request

from .config import Config, User
from .errors import PortalError

JWT_HEADER = "Cf-Access-Jwt-Assertion"
JWT_COOKIE = "CF_Authorization"


class Authenticator:
    def __init__(self, cfg: Config, key_resolver: Callable[[str], Any] | None = None,
                 lookup: Callable[[str], User | None] | None = None):
        self.cfg = cfg
        # 預設查設定檔；create_app 會換成查資料庫
        self.lookup = lookup or (lambda email: cfg.users.get(email))
        self.mode = cfg.auth.get("mode", "dev")
        if self.mode not in ("dev", "cloudflare"):
            raise ValueError("auth.mode 必須是 dev 或 cloudflare")
        self.team = (cfg.auth.get("team_domain") or "").strip().rstrip("/")
        self.team = self.team.removeprefix("https://")
        self.audience = cfg.auth.get("audience")
        self._key_resolver = key_resolver
        self._jwks: jwt.PyJWKClient | None = None
        if self.mode == "cloudflare":
            if not self.team or not self.audience:
                raise ValueError("cloudflare 模式需要設定 auth.team_domain 和 auth.audience")
            if key_resolver is None:
                self._jwks = jwt.PyJWKClient(
                    f"https://{self.team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)

    def _signing_key(self, token: str) -> Any:
        if self._key_resolver:
            return self._key_resolver(token)
        assert self._jwks is not None
        return self._jwks.get_signing_key_from_jwt(token).key

    def email_from_jwt(self, token: str) -> str:
        try:
            claims = jwt.decode(
                token, self._signing_key(token), algorithms=["RS256"],
                audience=self.audience, issuer=f"https://{self.team}",
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.PyJWTError as e:
            raise PortalError(401, f"Access JWT 驗證失敗：{e}")
        email = claims.get("email")
        if not email:
            raise PortalError(401, "JWT 裡沒有 email（服務權杖不能用來登入 Portal）")
        return str(email).strip().lower()

    def identify(self, request: Request) -> str:
        if self.mode == "dev":
            # 防呆：dev 模式不驗證身分，經過 Cloudflare 進來的 request 一律擋掉
            if request.headers.get("Cf-Ray") or request.headers.get(JWT_HEADER):
                raise PortalError(403, "Portal 目前是 dev 模式，不能從 Cloudflare 對外使用")
            email = (request.headers.get("X-Dev-User") or request.query_params.get("as")
                     or request.cookies.get("dev_user")
                     or self.cfg.auth.get("dev_default_user", ""))
            return email.strip().lower()
        token = request.headers.get(JWT_HEADER) or request.cookies.get(JWT_COOKIE)
        if not token:
            raise PortalError(401, "缺少 Cloudflare Access JWT，請經由 Access 網址進入")
        return self.email_from_jwt(token)

    def user(self, request: Request) -> User:
        email = self.identify(request)
        user = self.lookup(email)
        if user is None:
            raise PortalError(403, f"{email or '（未知）'} 不在權限表裡或已停用")
        return user
