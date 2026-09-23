"""FastAPI app: JSON API + dashboard. Run: uvicorn app.main:app --port 8080"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .alerts import send_ntfy
from .config import DB_PATH, load_config
from .db import DB
from .edgar import ITEMS_8K
from .form4 import CODE_LABELS
from .poller import OWNERSHIP_FORMS, Poller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

cfg = load_config()
db = DB(DB_PATH)
poller = Poller(db, cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await poller.start()
    yield
    await poller.stop()


app = FastAPI(title="sec-watch", lifespan=lifespan)

GROUPS = {
    "insider": "(form IN ('3','4','5','3/A','4/A','5/A','144','144/A'))",
    "events": "(form LIKE '8-K%' OR form LIKE '6-K%')",
    "periodic": "(form LIKE '10-K%' OR form LIKE '10-Q%' OR form LIKE '20-F%' OR form LIKE '40-F%')",
    "ownership": "(form LIKE 'SC 13%' OR form LIKE 'SCHEDULE 13%' OR form LIKE '13F%')",
    "offerings": "(form LIKE 'S-1%' OR form LIKE 'S-3%' OR form LIKE 'S-8%' OR form LIKE '424B%' OR form LIKE 'F-%')",
    "proxy": "(form LIKE 'DEF 14%' OR form LIKE 'DEFA14%' OR form LIKE 'PRE 14%')",
}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    return {**poller.status, "demo": cfg["demo"], "watch_count": len(poller.watch),
            "poll_seconds": cfg["poll_seconds"], "recent_alerts": db.recent_alerts(10)}


@app.get("/healthz")
async def healthz():
    """Liveness + a simple staleness check, for the Docker healthcheck and for monitoring."""
    last = poller.status.get("last_poll")
    stale = True
    if last:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        stale = age > max(120, cfg["poll_seconds"] * 6)
    ok = cfg["demo"] or (bool(poller.status["ua_ok"]) and not stale)
    body = {"status": "demo" if cfg["demo"] else ("ok" if ok else "degraded"), "last_poll": last,
            "polls": poller.status["polls"], "watch_count": len(poller.watch),
            "error": poller.status["error"]}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/api/meta")
async def meta():
    return {"codes": CODE_LABELS, "items_8k": ITEMS_8K, "groups": list(GROUPS)}


@app.get("/api/filings")
async def filings(tickers: str = "", group: str = "", signal: str = "", q: str = "",
                  limit: int = 100, before: str = ""):
    where, args = [], []
    if tickers:
        ts = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        where.append(f"ticker IN ({','.join('?' * len(ts))})")
        args += ts
    if group in GROUPS:
        where.append(GROUPS[group])
    elif group == "other":
        where.append("NOT (" + " OR ".join(GROUPS.values()) + ")")
    if signal in ("BUY", "SELL"):
        where.append("signal=?")
        args.append(signal)
    if q:
        where.append("(company LIKE ? OR insider LIKE ? OR form LIKE ? OR ticker LIKE ?)")
        args += [f"%{q}%"] * 4
    if before:
        where.append("filed_at < ?")
        args.append(before)
    sql = "SELECT * FROM filings" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY filed_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    rows = [dict(r) for r in db.conn.execute(sql, args)]
    for r in rows:
        r["transactions"] = db.transactions(r["accession"]) if r["form"] in OWNERSHIP_FORMS else []
    return rows


@app.get("/api/insider-summary")
async def insider_summary(days: int = 90):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for w in db.watchlist():
        rows = [dict(r) for r in db.conn.execute(
            "SELECT * FROM filings WHERE ticker=? AND filed_at>=? AND signal IN ('BUY','SELL')",
            (w["ticker"], since))]
        buys = [r for r in rows if r["signal"] == "BUY"]
        sells = [r for r in rows if r["signal"] == "SELL"]
        buyers = sorted({r["insider"] for r in buys if r["insider"]})
        # cluster = 2+ different insiders buying within any 30-day window
        cluster = False
        bdates = sorted((r["filed_at"], r["insider"]) for r in buys)
        for i, (d, _) in enumerate(bdates):
            end = (datetime.fromisoformat(d) + timedelta(days=30)).isoformat()
            if len({n for dd, n in bdates[i:] if dd <= end}) >= 2:
                cluster = True
                break
        out.append({
            "ticker": w["ticker"], "name": w["name"],
            "buy_count": len(buys), "buy_value": sum(r["buy_value"] or 0 for r in buys),
            "buyers": buyers, "sell_count": len(sells),
            "sell_value": sum(r["sell_value"] or 0 for r in sells),
            "last_buy": max((r["filed_at"] for r in buys), default=None),
            "cluster": cluster,
        })
    out.sort(key=lambda x: (-x["buy_value"], x["ticker"]))
    return out


# ---------------- watchlist ----------------
class TickerIn(BaseModel):
    ticker: str


@app.get("/api/watchlist")
async def get_watchlist():
    return db.watchlist()


@app.post("/api/watchlist")
async def add_watch(body: TickerIn):
    if not poller.tickers:
        raise HTTPException(503, "Still loading the SEC ticker list. Try again in a moment.")
    info = poller.resolve(body.ticker)
    if not info:
        raise HTTPException(404, f"{body.ticker.upper()} isn't in SEC's ticker list "
                                 "(it must be a US-listed SEC filer)")
    db.add_ticker(info["ticker"], info["cik"], info["name"])
    poller.refresh_watch()
    if not next(w for w in db.watchlist() if w["ticker"] == info["ticker"])["backfilled"]:
        poller.backfill_queue.put_nowait(info["ticker"])
    return info


@app.delete("/api/watchlist/{ticker}")
async def remove_watch(ticker: str):
    db.remove_ticker(ticker.upper())
    poller.refresh_watch()
    return {"ok": True}


# ---------------- alert rules ----------------
class Rules(BaseModel):
    insider_buy: bool
    insider_buy_min_usd: float
    insider_sell: bool
    insider_sell_min_usd: float
    skip_10b5_1_sales: bool
    forms: list[str]
    alert_window_minutes: int


@app.get("/api/rules")
async def get_rules():
    return poller.rules()


@app.put("/api/rules")
async def put_rules(r: Rules):
    data = r.model_dump()
    data["forms"] = [f.strip().upper() for f in data["forms"] if f.strip()]
    db.set_setting("alert_rules", data)
    return data


@app.post("/api/test-alert")
async def test_alert():
    ok = await send_ntfy(cfg["ntfy_server"], cfg["ntfy_topic"], {
        "title": "sec-watch is connected",
        "message": "Test alert. Insider buys on your watchlist will look like this.",
        "priority": 4, "tags": ["white_check_mark"],
    })
    db.log_alert(None, "Test alert", "", ok)
    if not ok:
        raise HTTPException(502, "ntfy didn't accept the message. Check NTFY_TOPIC in .env.")
    return {"ok": True, "topic": cfg["ntfy_topic"]}
