"""python -m portal [--config portal.toml]"""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .config import load_config_file


def main() -> None:
    ap = argparse.ArgumentParser(description="Test-Lab Portal")
    ap.add_argument("--config", default=os.environ.get("PORTAL_CONFIG", "portal.toml"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    if not os.path.exists(args.config):
        print(f"找不到 {args.config}，改用 portal.example.toml（示範設定）")
        args.config = "portal.example.toml"
    os.environ["PORTAL_CONFIG"] = args.config
    cfg = load_config_file(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("portal.app:app_from_env", factory=True,
                host=args.host or cfg.portal.get("host", "127.0.0.1"),
                port=args.port or int(cfg.portal.get("port", 8080)))


if __name__ == "__main__":
    main()
