"""Loads config.yaml + environment variables."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SECWATCH_DATA", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "secwatch.db"

DEFAULT_ALERTS = {
    "insider_buy": True,
    "insider_buy_min_usd": 0,
    "insider_sell": True,
    "insider_sell_min_usd": 1_000_000,
    "skip_10b5_1_sales": True,
    "forms": ["8-K", "SC 13D", "SCHEDULE 13D", "SC 13D/A", "SCHEDULE 13D/A"],
    "alert_window_minutes": 90,
}


def load_config() -> dict:
    path = Path(os.environ.get("SECWATCH_CONFIG", ROOT / "config.yaml"))
    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    alerts = {**DEFAULT_ALERTS, **(cfg.get("alerts") or {})}
    return {
        "watchlist": [str(t).upper().strip() for t in cfg.get("watchlist", [])],
        "poll_seconds": max(10, int(cfg.get("poll_seconds", 20))),
        "sweep_minutes": max(1, int(cfg.get("sweep_minutes", 5))),
        "backfill_days": int(cfg.get("backfill_days", 90)),
        "catchup_hours": int(cfg.get("catchup_hours", 24)),
        "alerts": alerts,
        "demo": os.environ.get("SECWATCH_DEMO", "").lower() in ("1", "true", "yes"),
        "user_agent": os.environ.get("SEC_USER_AGENT", "").strip(),
        "ntfy_server": os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        "ntfy_topic": os.environ.get("NTFY_TOPIC", "").strip(),
    }


def user_agent_ok(ua: str) -> bool:
    return bool(ua) and "@" in ua and "example.com" not in ua
