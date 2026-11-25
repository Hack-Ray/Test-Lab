from __future__ import annotations

import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portal.app import create_app
from portal.config import load_config
from portal.db import DB
from portal.drivers import MockDriver
from portal.service import Portal

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Taipei")


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def set(self, *args: int) -> None:
        self.dt = datetime(*args, tzinfo=TZ)

    def mono(self) -> float:
        return self.dt.timestamp()


@pytest.fixture
def raw_cfg():
    with open(ROOT / "portal.example.toml", "rb") as f:
        return tomllib.load(f)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 25, 10, 0, tzinfo=TZ))  # 週三 10:00


@pytest.fixture
def driver(clock):
    return MockDriver(boot_seconds=60, clock=clock.mono)


@pytest.fixture
def portal(raw_cfg, driver, clock):
    return Portal(load_config(raw_cfg), driver, DB(":memory:"), clock)


@pytest.fixture
def make_client(raw_cfg, driver, clock):
    def make(**auth_overrides):
        raw = dict(raw_cfg)
        raw["auth"] = {**raw_cfg["auth"], **auth_overrides.pop("auth", {})}
        cfg = load_config(raw)
        app = create_app(cfg, driver=driver, db=DB(":memory:"), clock=clock,
                         background=False, **auth_overrides)
        return TestClient(app)
    return make
