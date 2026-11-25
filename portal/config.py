from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

from .drivers import VMSpec

ROLES = ("PM", "SE", "PG", "SA")
SERVER_ROLES = ("PG", "SA")


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Bank:
    id: str
    name: str


@dataclass(frozen=True)
class User:
    email: str
    role: str
    banks: tuple[str, ...]

    def can_access_bank(self, bank_id: str) -> bool:
        return "*" in self.banks or bank_id in self.banks

    def can_access_kind(self, kind: str) -> bool:
        return kind == "client" or self.role in SERVER_ROLES


@dataclass
class Config:
    raw: dict[str, Any]
    banks: dict[str, Bank] = field(default_factory=dict)
    users: dict[str, User] = field(default_factory=dict)
    vms: dict[str, VMSpec] = field(default_factory=dict)

    @property
    def portal(self) -> dict[str, Any]:
        return self.raw.get("portal", {})

    @property
    def auth(self) -> dict[str, Any]:
        return self.raw.get("auth", {})

    @property
    def driver(self) -> dict[str, Any]:
        return self.raw.get("driver", {})

    @property
    def schedule(self) -> dict[str, Any]:
        return self.raw.get("schedule", {})

    @property
    def prewarm(self) -> dict[str, Any]:
        return self.raw.get("prewarm", {})

    def server_for(self, bank_id: str) -> VMSpec | None:
        for vm in self.vms.values():
            if vm.kind == "server" and vm.bank == bank_id:
                return vm
        return None

    def clients_for(self, bank_id: str) -> list[VMSpec]:
        """該銀行專屬 client 優先，再來是共用池。"""
        own = [v for v in self.vms.values() if v.kind == "client" and v.bank == bank_id]
        shared = [v for v in self.vms.values() if v.kind == "client" and v.bank is None]
        return own + shared


def load_config(data: dict[str, Any]) -> Config:
    cfg = Config(raw=data)
    for b in data.get("banks", []):
        cfg.banks[b["id"]] = Bank(id=b["id"], name=b.get("name", b["id"]))
    for u in data.get("users", []):
        role = u["role"].upper()
        if role not in ROLES:
            raise ValueError(f"使用者 {u['email']} 的角色 {role} 不合法，應為 {ROLES}")
        email = u["email"].strip().lower()
        cfg.users[email] = User(email=email, role=role, banks=tuple(u.get("banks", [])))
    for v in data.get("vms", []):
        kind = v["kind"]
        if kind not in ("client", "server"):
            raise ValueError(f"VM {v['name']} 的 kind 應為 client 或 server")
        if kind == "server" and not v.get("bank"):
            raise ValueError(f"server {v['name']} 必須指定 bank")
        cfg.vms[v["name"]] = VMSpec(
            name=v["name"], kind=kind, bank=v.get("bank"), vmx=v.get("vmx"),
            vsphere_id=v.get("vsphere_id"), host=v.get("host"),
            ports=tuple(v.get("ports", [3389])), rdp_url=v.get("rdp_url"),
        )
    return cfg


def load_config_file(path: str | Path) -> Config:
    with open(path, "rb") as f:
        return load_config(tomllib.load(f))
