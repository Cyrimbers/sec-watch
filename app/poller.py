"""Background loops: live feed (fast), worker (parse/backfill/sweep), alerting."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from . import edgar
from .alerts import build_alert, build_digest, matches_rules, send_ntfy
from .config import DATA_DIR, user_agent_ok
from .db import DB, now_iso
from .form4 import parse_ownership_xml, summarise

log = logging.getLogger("secwatch.poller")

OWNERSHIP_FORMS = {"3", "4", "5", "3/A", "4/A", "5/A"}
MIN_CATCHUP_GAP = timedelta(minutes=30)


def catchup_since(newest_stored: str, backfill_days: int) -> str | None:
    """How far back a start-up catch-up should re-read, or None if the gap is trivial.

    Gap measured from the newest filing already stored, minus an hour of overlap so a
    filing accepted moments before shutdown can't fall between the two windows. Capped at
    backfill_days so a database untouched for months doesn't trigger an enormous sweep.
    """
    now = datetime.now(timezone.utc)
    last = datetime.fromisoformat(newest_stored)
    if now - last < MIN_CATCHUP_GAP:
        return None
    floor = now - timedelta(days=backfill_days)
    return max(floor, last - timedelta(hours=1)).isoformat()


class Poller:
    def __init__(self, db: DB, cfg: dict):
        self.db = db
        self.cfg = cfg
        self.client = edgar.EdgarClient(cfg["user_agent"] or "sec-watch (unconfigured)")
        self.tickers: dict[str, dict] = {}
        self.watch: dict[str, str] = {}          # cik -> ticker
        self.seen: set[str] = set()
        self.seen_order: deque[str] = deque()
        self.in_progress: set[str] = set()
        self._primary_docs: dict[str, str] = {}   # accession -> primary doc name (from submissions)
        self.backfill_queue: asyncio.Queue[str] = asyncio.Queue()
        self.last_sweep = 0.0
        self._digest_done = False
        self.status = {
            "started_at": now_iso(), "last_poll": None, "last_poll_ok": None,
            "last_sweep": None, "last_new_filing": None, "error": None,
            "ua_ok": user_agent_ok(cfg["user_agent"]), "ntfy_ok": bool(cfg["ntfy_topic"]),
            "backfilling": None, "polls": 0,
        }
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ setup
    def rules(self) -> dict:
        return self.db.get_setting("alert_rules", self.cfg["alerts"])

    async def start(self):
        if self.db.get_setting("alert_rules") is None:
            self.db.set_setting("alert_rules", self.cfg["alerts"])
        if self.cfg.get("demo"):
            # demo mode: serve the seeded sample data, never touch SEC
            self.status.update(error="Demo mode — showing sample filings, not polling SEC.",
                               last_poll=now_iso(), last_poll_ok=True)
            self.refresh_watch()
            log.info("demo mode: poller disabled")
            return
        if not self.status["ua_ok"]:
            self.status["error"] = ("SEC_USER_AGENT is not set in .env. SEC blocks anonymous "
                                    "requests. Add 'YourName you@email.com' and restart.")
            log.error(self.status["error"])
            return
        self._tasks = [asyncio.create_task(self._boot_then_run())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await self.client.close()

    async def _boot_then_run(self):
        while True:
            try:
                await self.load_ticker_map()
                break
            except Exception as e:  # noqa: BLE001
                self.status["error"] = f"Couldn't load SEC ticker list: {e}"
                log.error(self.status["error"])
                await asyncio.sleep(60)
        if not self.db.get_setting("seeded"):
            for t in self.cfg["watchlist"]:
                info = self.resolve(t)
                if info:
                    self.db.add_ticker(info["ticker"], info["cik"], info["name"])
                else:
                    log.warning("Ticker %s not found in SEC list, skipped", t)
            self.db.set_setting("seeded", True)
        self.refresh_watch()
        for w in self.db.watchlist():
            if not w["backfilled"]:
                self.backfill_queue.put_nowait(w["ticker"])
        await self.startup_catchup()
        self._tasks += [asyncio.create_task(self._live_loop()),
                        asyncio.create_task(self._worker_loop())]

    async def startup_catchup(self):
        """Re-read far enough back to cover however long the app was down.

        The routine sweep only looks back sweep_days, so a machine that was off for longer
        than that leaves a permanent hole: nothing polled at the time, and nothing ever goes
        back for it. Here the window is the actual downtime instead — from the newest filing
        already stored, with an hour of overlap, capped at backfill_days.
        """
        row = self.db.conn.execute("SELECT MAX(filed_at) m FROM filings").fetchone()
        if not row or not row["m"]:
            return                                   # fresh database: backfill handles it
        since = catchup_since(row["m"], self.cfg["backfill_days"])
        if since is None:
            return                                   # gap too small to bother with
        down_for = datetime.now(timezone.utc) - datetime.fromisoformat(row["m"])
        log.info("catch-up: app was down ~%.1fh, re-reading filings since %s",
                 down_for.total_seconds() / 3600, since[:16])
        self.status["backfilling"] = "catch-up"
        try:
            for cik, ticker in list(self.watch.items()):
                try:
                    await self._ingest_company(cik, ticker, since, "sweep")
                except Exception as e:  # noqa: BLE001
                    log.warning("catch-up %s failed: %s", ticker, e)
        finally:
            self.status["backfilling"] = None

    async def load_ticker_map(self):
        cache = DATA_DIR / "company_tickers.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
            self.tickers = json.loads(cache.read_text())
            return
        self.tickers = await self.client.ticker_map()
        cache.write_text(json.dumps(self.tickers))

    def resolve(self, ticker: str) -> dict | None:
        t = ticker.upper().strip().replace(".", "-")
        return self.tickers.get(t)

    def refresh_watch(self):
        self.watch = {w["cik"]: w["ticker"] for w in self.db.watchlist() if w["cik"]}

    def _remember(self, acc: str):
        if acc in self.seen:
            return
        self.seen.add(acc)
        self.seen_order.append(acc)
        while len(self.seen_order) > 20000:
            self.seen.discard(self.seen_order.popleft())

    # ------------------------------------------------------------------ live feed
    async def _live_loop(self):
        first = True
        while True:
            t0 = time.monotonic()
            try:
                await self.poll_live(paginate=not first)
                self.status.update(last_poll=now_iso(), last_poll_ok=True, error=self.client.last_error)
                first = False
            except Exception as e:  # noqa: BLE001
                self.status.update(last_poll=now_iso(), last_poll_ok=False, error=str(e))
                log.warning("live poll failed: %s", e)
            self.status["polls"] += 1
            await asyncio.sleep(max(1.0, self.cfg["poll_seconds"] - (time.monotonic() - t0)))

    async def poll_live(self, paginate: bool = True):
        new_rows = []
        for page in range(3 if paginate else 1):
            entries = await self.client.current_feed(start=page * 100)
            if not entries:
                break
            unseen = [e for e in entries if e["accession"] not in self.seen]
            for e in entries:
                if e["cik"] in self.watch and e["role"] in edgar.COMPANY_ROLES:
                    row = {**e, "ticker": self.watch[e["cik"]], "source": "live"}
                    if self.db.insert_filing(row):
                        new_rows.append(row)
            for e in entries:
                self._remember(e["accession"])
            # only page further back if this whole page was new to us (a burst of filings)
            if len(unseen) < len(entries):
                break
        for row in new_rows:
            self.status["last_new_filing"] = now_iso()
            log.info("NEW %s %s %s", row["ticker"], row["form"], row["accession"])
            await self.process(row["accession"])

    # ------------------------------------------------------------------ worker
    async def _worker_loop(self):
        while True:
            try:
                for f in self.db.pending_parse(limit=10):
                    await self.process(f["accession"])
                if not self.backfill_queue.empty():
                    await self.backfill(self.backfill_queue.get_nowait())
                elif time.monotonic() - self.last_sweep > self.cfg["sweep_minutes"] * 60:
                    await self.sweep()
                    self.last_sweep = time.monotonic()
                await self.evaluate_alerts()
                # once the backlog is clear, tell the user what they missed while offline
                if (not self._digest_done and self.last_sweep > 0
                        and self.backfill_queue.empty() and not self.db.pending_parse(limit=1)):
                    await self.send_catchup_digest()
                    self._digest_done = True
            except Exception as e:  # noqa: BLE001
                log.warning("worker error: %s", e)
            await asyncio.sleep(2)

    async def backfill(self, ticker: str):
        w = next((x for x in self.db.watchlist() if x["ticker"] == ticker), None)
        if not w or not w["cik"]:
            return
        self.status["backfilling"] = ticker
        since = (datetime.now(timezone.utc) - timedelta(days=self.cfg["backfill_days"])).isoformat()
        try:
            await self._ingest_company(w["cik"], ticker, since, "backfill")
            self.db.mark_backfilled(ticker)
            log.info("backfilled %s", ticker)
        finally:
            self.status["backfilling"] = None

    async def sweep(self):
        since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        for cik, ticker in list(self.watch.items()):
            try:
                await self._ingest_company(cik, ticker, since, "sweep")
            except Exception as e:  # noqa: BLE001
                log.warning("sweep %s failed: %s", ticker, e)
        self.status["last_sweep"] = now_iso()

    async def _ingest_company(self, cik: str, ticker: str, since: str, source: str):
        data = await self.client.submissions(cik)
        for f in edgar.parse_submissions(data, since_iso=since):
            f.update(ticker=ticker, source=source)
            if self.db.insert_filing(f):
                if len(self._primary_docs) > 5000:      # bounded: this is a hint cache, not state
                    self._primary_docs.clear()
                self._primary_docs[f["accession"]] = f.get("primary_doc")
                if source == "sweep":
                    self.status["last_new_filing"] = now_iso()
                    log.info("SWEEP found %s %s %s", ticker, f["form"], f["accession"])

    # ------------------------------------------------------------------ per filing
    async def process(self, accession: str):
        if accession in self.in_progress:
            return
        self.in_progress.add(accession)
        try:
            row = self.db.conn.execute("SELECT * FROM filings WHERE accession=?", (accession,)).fetchone()
            if row is None or row["parsed"]:
                return
            f = dict(row)
            try:
                if f["form"] in OWNERSHIP_FORMS:
                    await self._parse_ownership(f)
                elif f["form"].upper().startswith("144"):
                    self.db.update_filing(accession, codes="144")
                elif f["form"].upper().startswith("8-K") and not f["items"] and f["url"]:
                    html = await self.client.fetch_text(f["url"])
                    self.db.update_filing(accession, items=edgar.parse_8k_items_from_index(html))
                self.db.update_filing(accession, parsed=1)
            except Exception as e:  # noqa: BLE001
                log.warning("parse %s failed: %s", accession, e)
                self.db.update_filing(accession, parsed=1, codes="ERR")
            await self.evaluate_alerts(only=accession)
        finally:
            self.in_progress.discard(accession)

    async def _parse_ownership(self, f: dict):
        acc = f["accession"]
        base = f"{edgar.ARCHIVE_BASE}/{int(f['cik'])}/{acc.replace('-', '')}"
        name = edgar.raw_xml_from_primary(self._primary_docs.pop(acc, "") or "")
        if not name:
            name = edgar.pick_form4_xml(await self.client.filing_index(f["cik"], acc))
        if not name:
            raise ValueError("no XML document in filing")
        doc = parse_ownership_xml(await self.client.fetch_text(f"{base}/{name}"))
        self.db.replace_transactions(acc, doc["transactions"])
        self.db.update_filing(
            acc, signal=doc["signal"], buy_value=doc["buy_value"], sell_value=doc["sell_value"],
            insider=doc["owner"], insider_role=doc["role"],
            codes="HOLD" if doc.get("holdings_only") else ",".join(doc["codes"]),
        )

    def detail_for(self, f: dict) -> dict | None:
        if f["form"] not in OWNERSHIP_FORMS:
            return None
        txs = self.db.transactions(f["accession"])
        if not txs:
            return None
        return {**summarise(txs), "owner": f.get("insider"), "role": f.get("insider_role")}

    async def send_catchup_digest(self):
        """Summarise alert-worthy filings that arrived while the app wasn't running."""
        hours = int(self.cfg["catchup_hours"])
        # the window is whichever is later: the last digest, or catchup_hours ago. The second
        # bound is what stops a first run (or a long absence) digesting a 90-day backfill.
        window_start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cutoff = max(self.db.get_setting("last_digest_at") or "", window_start)
        rules = self.rules()
        rows = [dict(r) for r in self.db.conn.execute(
            """SELECT * FROM filings WHERE filed_at >= ? AND parsed=1
               AND accession NOT IN (SELECT accession FROM alert_log WHERE accession IS NOT NULL)
               ORDER BY filed_at DESC""", (cutoff,))]
        items = []
        for f in rows:
            detail = self.detail_for(f)
            alert = matches_rules(f, detail, rules)
            if not alert:
                continue
            kind = detail["signal"] if detail and detail["signal"] in ("BUY", "SELL") else "OTHER"
            items.append({
                "kind": kind, "ticker": f.get("ticker") or f.get("company") or "?",
                "value": (detail or {}).get("buy_value") or (detail or {}).get("sell_value") or 0,
                "who": (f.get("insider") or "") if kind != "OTHER" else f.get("form"),
                "url": f.get("url"),
            })
        items.sort(key=lambda i: -(i["value"] or 0))
        digest = build_digest(items, hours)
        if digest:
            ok = await send_ntfy(self.cfg["ntfy_server"], self.cfg["ntfy_topic"], digest)
            self.db.log_alert(None, digest["title"], digest["message"], ok)
            log.info("CATCH-UP DIGEST: %s (%s)", digest["title"], "sent" if ok else "FAILED")
        self.db.set_setting("last_digest_at", now_iso())

    async def evaluate_alerts(self, only: str | None = None):
        """Send alerts for filings that haven't been evaluated yet.

        Two callers reach this concurrently — the live poller, for a filing it has just
        processed, and the worker loop, for everything outstanding. Sending is an await, so
        marking the row afterwards leaves a window in which the other caller sees the same
        row still pending and sends a second, identical notification. Claiming the row first
        closes that window: the UPDATE ... WHERE alerted=0 is atomic, and only the caller
        whose update actually changed a row goes on to send.
        """
        rules = self.rules()
        for f in self.db.pending_alert():
            if only and f["accession"] != only:
                continue
            if not self.db.claim_alert(f["accession"]):
                continue                      # another caller got there first
            alert = build_alert(f, self.detail_for(f), rules)
            if alert:
                ok = await send_ntfy(self.cfg["ntfy_server"], self.cfg["ntfy_topic"], alert)
                self.db.log_alert(f["accession"], alert["title"], alert["message"], ok)
                log.info("ALERT %s (%s)", alert["title"], "sent" if ok else "FAILED")
