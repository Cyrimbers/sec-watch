import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pytest
from defusedxml.common import EntitiesForbidden

os.environ.setdefault("SECWATCH_DATA", tempfile.mkdtemp())

from app import edgar  # noqa: E402
from app.alerts import build_alert  # noqa: E402
from app.db import DB  # noqa: E402
from app.form4 import is_plan_footnote, parse_ownership_xml, tidy_name  # noqa: E402
from app.poller import Poller  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
RULES = {"insider_buy": True, "insider_buy_min_usd": 0, "insider_sell": True,
         "insider_sell_min_usd": 1_000_000, "skip_10b5_1_sales": True,
         "forms": ["8-K", "SC 13D"], "alert_window_minutes": 90}


def now_iso(minus_min=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minus_min)).replace(microsecond=0).isoformat()


# ---------------- Form 4 parsing ----------------
def test_buy_parsed_and_aggregated():
    d = parse_ownership_xml((FIX / "form4_buy.xml").read_text())
    assert d["ticker"] == "BSX"
    assert d["owner"] == "Doe Jane A"          # as EDGAR files it, capitals tidied
    assert d["role"] == "Chief Executive Officer, Director"
    assert d["signal"] == "BUY"
    assert d["buy_shares"] == 20000
    assert d["buy_value"] == 12000 * 98.25 + 8000 * 98.75   # 1,969,000
    assert round(d["buy_avg_price"], 2) == 98.45
    assert len(d["transactions"]) == 2


def test_sale_under_10b5_1_detected_and_derivatives_ignored():
    d = parse_ownership_xml((FIX / "form4_sale_plan.xml").read_text())
    assert d["signal"] == "SELL"
    assert d["sell_value"] == 5000 * 505.10
    assert d["sell_all_10b5_1"] is True
    assert "M" in d["codes"]
    assert any(t["derivative"] for t in d["transactions"])


def test_tidy_name_fixes_capitals_but_never_reorders():
    assert tidy_name("MAHONEY MICHAEL F") == "Mahoney Michael F"
    assert tidy_name("BERKSHIRE HATHAWAY LLC") == "Berkshire Hathaway LLC"
    assert tidy_name("O'CONNOR PADRAIG ANDREW") == "O'Connor Padraig Andrew"
    assert tidy_name("SECHAN II ROBERT J") == "Sechan II Robert J"
    assert tidy_name("Verma Shiv") == "Verma Shiv"        # mixed case: left exactly as filed
    assert tidy_name("ACI Investment Partners, LLC") == "ACI Investment Partners, LLC"


def test_10b5_1_footnote_negation_is_not_a_plan():
    assert is_plan_footnote("The sale was effected pursuant to a Rule 10b5-1 trading plan.")
    assert not is_plan_footnote("These shares were sold to cover taxes.")
    assert not is_plan_footnote("This transaction was not made pursuant to a Rule 10b5-1 plan.")
    assert not is_plan_footnote("No 10b5-1 trading plan was in effect at the time of sale.")


# ---------------- EDGAR feeds ----------------
def test_current_feed_parsing_and_roles():
    rows = edgar.parse_current_feed((FIX / "current_feed.xml").read_text())
    assert len(rows) == 4
    issuer = [r for r in rows if r["role"] == "issuer"][0]
    assert issuer["cik"] == "0000885725"
    assert issuer["accession"] == "0000885725-26-000123"
    assert issuer["form"] == "4"
    assert issuer["filed_at"] == "2026-09-18T20:05:12+00:00"   # 16:05 EDT -> UTC


def test_submissions_acceptance_time_is_really_utc():
    # verified against a filing's "Accepted" field (printed in US Eastern) on its index page:
    # JSON 2026-09-22T00:41:22Z == "2026-09-21 20:41:22" EDT
    assert edgar.edgar_acceptance_to_utc("2026-09-22T00:41:22.000Z") == "2026-09-22T00:41:22+00:00"
    assert edgar.edgar_acceptance_to_utc("2026-01-15T16:30:00.000Z") == "2026-01-15T16:30:00+00:00"
    data = {"cik": "885725", "name": "BOSTON SCIENTIFIC CORP", "filings": {"recent": {
        "accessionNumber": ["0000885725-26-000123", "0000885725-25-000001"],
        "filingDate": ["2026-09-18", "2025-01-02"],
        "acceptanceDateTime": ["2026-09-18T16:05:12.000Z", "2025-01-02T09:00:00.000Z"],
        "form": ["4", "10-K"],
        "primaryDocument": ["xslF345X05/wk-form4_1.xml", "bsx-10k.htm"],
        "items": ["", ""]}}}
    rows = edgar.parse_submissions(data, since_iso="2026-01-01T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["cik"] == "0000885725"
    assert edgar.raw_xml_from_primary(rows[0]["primary_doc"]) == "wk-form4_1.xml"


def test_8k_items_and_xml_pick():
    html = "<div>Items</div><div>Item 2.02: Results of Operations</div><div>Item 9.01: Exhibits</div>"
    assert edgar.parse_8k_items_from_index(html) == "2.02,9.01"
    assert edgar.pick_form4_xml(["0000885725-26-000123-index.htm", "wk-form4_1758.xml",
                                 "xslF345X05/wk-form4_1758.xml"]) == "wk-form4_1758.xml"


# ---------------- alert rules ----------------
def _detail(path):
    d = parse_ownership_xml((FIX / path).read_text())
    return d


def test_buy_alert_text():
    f = {"ticker": "BSX", "form": "4", "filed_at": now_iso(3), "url": "u"}
    a = build_alert(f, _detail("form4_buy.xml"), RULES)
    assert a["title"] == "BSX: insider BUY $1.97M"
    assert "Doe Jane A (Chief Executive Officer, Director) bought 20,000 sh @ $98.45" in a["message"]
    assert a["priority"] == 5


def test_no_alert_for_old_or_small_or_planned():
    f = {"ticker": "BSX", "form": "4", "filed_at": now_iso(500), "url": "u"}
    assert build_alert(f, _detail("form4_buy.xml"), RULES) is None           # too old
    f["filed_at"] = now_iso(1)
    assert build_alert(f, _detail("form4_buy.xml"), {**RULES, "insider_buy_min_usd": 5e6}) is None
    assert build_alert(f, _detail("form4_sale_plan.xml"), {**RULES, "insider_sell_min_usd": 0}) is None


def test_8k_alert_uses_item_labels():
    f = {"ticker": "SOFI", "form": "8-K", "filed_at": now_iso(1), "url": "u", "items": "2.02,9.01"}
    a = build_alert(f, None, RULES)
    assert a["title"] == "SOFI: new 8-K" and a["message"] == "Results"


# ---------------- end-to-end pipeline with a fake SEC ----------------
class FakeClient:
    last_error = None

    def __init__(self):
        feed = (FIX / "current_feed.xml").read_text()
        et_now = datetime.now(timezone(timedelta(hours=-4))).replace(microsecond=0).isoformat()
        self.feed = feed.replace("2026-09-18T16:05:12-04:00", et_now).replace(
            "2026-09-18T16:04:00-04:00", et_now)
        self.urls = []

    async def current_feed(self, start=0, count=100):
        return edgar.parse_current_feed(self.feed) if start == 0 else []

    async def filing_index(self, cik, acc):
        return ["0000885725-26-000123-index.htm", "wk-form4_1758.xml"]

    async def fetch_text(self, url):
        self.urls.append(url)
        if url.endswith(".xml"):
            return (FIX / "form4_buy.xml").read_text()
        return "<div>Item 5.02: Departure of Directors</div><div>Item 9.01</div>"

    async def close(self):
        pass


def test_pipeline_live_feed_to_alert(monkeypatch):
    db = DB(Path(tempfile.mkdtemp()) / "t.db")
    cfg = {"watchlist": [], "poll_seconds": 20, "sweep_minutes": 5, "backfill_days": 90,
           "catchup_hours": 24, "alerts": RULES, "user_agent": "K test@test.io",
           "ntfy_server": "x", "ntfy_topic": "t"}
    p = Poller(db, cfg)
    p.client = FakeClient()
    db.set_setting("alert_rules", RULES)
    db.add_ticker("BSX", "0000885725", "BOSTON SCIENTIFIC CORP")
    db.add_ticker("SOFI", "0001818874", "SoFi Technologies, Inc.")
    p.refresh_watch()

    sent = []

    async def fake_send(server, topic, alert, http=None):
        sent.append(alert)
        return True
    monkeypatch.setattr("app.poller.send_ntfy", fake_send)

    asyncio.run(p.poll_live())

    rows = {r["accession"]: dict(r) for r in db.conn.execute("SELECT * FROM filings")}
    assert set(rows) == {"0000885725-26-000123", "0001818874-26-000099"}   # 13G for other co ignored
    bsx = rows["0000885725-26-000123"]
    assert bsx["ticker"] == "BSX" and bsx["signal"] == "BUY" and bsx["alerted"] == 1
    assert bsx["insider"] == "Doe Jane A"
    assert rows["0001818874-26-000099"]["items"] == "5.02,9.01"
    titles = [a["title"] for a in sent]
    assert "BSX: insider BUY $1.97M" in titles
    assert "SOFI: new 8-K" in titles
    # a second poll must not duplicate anything
    asyncio.run(p.poll_live())
    assert len(sent) == 2


# ---------------- catch-up digest ----------------
def test_digest_summarises_what_was_missed(monkeypatch):
    db = DB(Path(tempfile.mkdtemp()) / "d.db")
    cfg = {"watchlist": [], "poll_seconds": 20, "sweep_minutes": 5, "backfill_days": 90,
           "catchup_hours": 24, "alerts": RULES, "user_agent": "K test@test.io",
           "ntfy_server": "x", "ntfy_topic": "t"}
    p = Poller(db, cfg)
    db.set_setting("alert_rules", RULES)
    sent = []

    async def fake_send(server, topic, alert, http=None):
        sent.append(alert)
        return True
    monkeypatch.setattr("app.poller.send_ntfy", fake_send)

    # with no filings yet there is nothing to report, but the marker is written
    asyncio.run(p.send_catchup_digest())
    assert sent == [] and db.get_setting("last_digest_at")

    db.set_setting("last_digest_at", now_iso(60 * 20))          # last digest 20h ago
    buy = _detail("form4_buy.xml")
    for i, (tic, mins) in enumerate([("GRAB", 400), ("BSX", 300)]):
        acc = f"000000000{i}-26-000001"
        db.insert_filing({"accession": acc, "cik": "0000885725", "ticker": tic, "company": tic,
                          "form": "4", "filed_at": now_iso(mins), "url": f"u{i}", "source": "sweep"})
        db.replace_transactions(acc, buy["transactions"])
        db.update_filing(acc, parsed=1, alerted=1, signal="BUY", buy_value=buy["buy_value"],
                         insider=buy["owner"], insider_role=buy["role"], codes="P")
    # something filed 3 days ago is outside the catch-up window
    db.insert_filing({"accession": "0000000099-26-000001", "cik": "0000885725", "ticker": "OLD",
                      "company": "OLD", "form": "8-K", "filed_at": now_iso(60 * 72),
                      "url": "u9", "source": "backfill"})
    db.update_filing("0000000099-26-000001", parsed=1, alerted=1)

    asyncio.run(p.send_catchup_digest())
    assert len(sent) == 1
    d = sent[0]
    assert d["title"] == "While you were away: 2 buys"
    assert "+ GRAB $1.97M Doe Jane A" in d["message"]
    assert "OLD" not in d["message"]

    # running again must not repeat the same digest
    asyncio.run(p.send_catchup_digest())
    assert len(sent) == 1


def test_holdings_only_form4_is_labelled():
    xml = (FIX / "form4_buy.xml").read_text()
    start = xml.index("<nonDerivativeTable>")
    end = xml.index("</nonDerivativeTable>") + len("</nonDerivativeTable>")
    holding = """<nonDerivativeTable><nonDerivativeHolding>
        <securityTitle><value>Common Stock</value></securityTitle>
        <postTransactionAmounts><sharesOwnedFollowingTransaction><value>142000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
        <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeHolding></nonDerivativeTable>"""
    d = parse_ownership_xml(xml[:start] + holding + xml[end:])
    assert d["transactions"] == [] and d["holdings_only"] is True
    assert d["signal"] == "OTHER" and d["buy_value"] == 0


def test_migration_repairs_old_acceptance_times():
    # what the buggy version stored for GRAB: real 00:41:22Z read as Eastern -> 04:41:22Z
    assert edgar.undo_et_misread("2026-09-22T04:41:22+00:00") == "2026-09-22T00:41:22+00:00"
    # and in winter, when Eastern is UTC-5
    assert edgar.undo_et_misread("2026-01-15T21:30:00+00:00") == "2026-01-15T16:30:00+00:00"

    path = Path(tempfile.mkdtemp()) / "m.db"
    db = DB(path)
    db.conn.execute("DELETE FROM settings WHERE key='acceptance_tz_fixed'")
    for acc, src, ts in [("a-26-1", "backfill", "2026-09-22T04:41:22+00:00"),
                         ("b-26-1", "live", "2026-09-22T04:41:22+00:00")]:
        db.insert_filing({"accession": acc, "cik": "1", "ticker": "X", "company": "X",
                          "form": "4", "filed_at": ts, "url": "u", "source": src})
    db.conn.commit()
    DB(path)                                    # reopen: migration runs
    got = {r["accession"]: r["filed_at"]
           for r in DB(path).conn.execute("SELECT accession, filed_at FROM filings")}
    assert got["a-26-1"] == "2026-09-22T00:41:22+00:00"     # backfill row corrected
    assert got["b-26-1"] == "2026-09-22T04:41:22+00:00"     # live row untouched


def test_footnotes_are_captured_per_transaction():
    d = parse_ownership_xml((FIX / "form4_buy.xml").read_text())
    first, second = d["transactions"]
    assert "Weighted average price" in first["footnotes"]     # footnote F1 on the price
    assert second["footnotes"] is None                        # no footnote on that line
    # and code P is labelled the way SEC defines it, covering private purchases too
    from app.form4 import CODE_LABELS
    assert CODE_LABELS["P"] == "Open market or private purchase"


def test_footnotes_survive_a_round_trip_through_the_db():
    db = DB(Path(tempfile.mkdtemp()) / "fn.db")
    d = parse_ownership_xml((FIX / "form4_buy.xml").read_text())
    db.insert_filing({"accession": "x-26-1", "cik": "1", "ticker": "BSX", "company": "B",
                      "form": "4", "filed_at": now_iso(1), "url": "u", "source": "live"})
    db.replace_transactions("x-26-1", d["transactions"])
    got = db.transactions("x-26-1")
    assert "Weighted average price" in got[0]["footnotes"]


def test_xml_bomb_is_rejected():
    """Filings are untrusted input: defusedxml must refuse entity expansion."""
    bomb = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
      <!ENTITY lol "lol">
      <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
      <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
    ]>
    <ownershipDocument><documentType>4</documentType>
      <issuer><issuerTradingSymbol>&lol3;</issuerTradingSymbol></issuer>
    </ownershipDocument>"""
    # the feed parser sees the whole document, DOCTYPE included: defusedxml refuses it
    with pytest.raises(EntitiesForbidden):
        edgar.parse_current_feed(bomb)

    # the ownership parser slices from <ownershipDocument, so the DTD never reaches the
    # parser at all and the undefined entity is a plain parse error. Either way: no expansion.
    with pytest.raises((EntitiesForbidden, ParseError)):
        parse_ownership_xml(bomb)


def test_sell_alert_title_carries_the_plan_marker():
    # a collapsed phone notification truncates the body, so 10b5-1 has to be in the title
    f = {"ticker": "HOOD", "form": "4", "filed_at": now_iso(2), "url": "u"}
    a = build_alert(f, _detail("form4_sale_plan.xml"), {**RULES, "insider_sell_min_usd": 0,
                                                       "skip_10b5_1_sales": False})
    assert a["title"].endswith("(10b5-1)")
    assert "code S (open market or private sale)" in a["message"]


def test_concurrent_evaluators_send_one_alert_not_two(monkeypatch):
    """Regression: the live poller and the worker loop both evaluate pending alerts.

    Before claim_alert(), both could read the same pending row while the other was awaiting
    ntfy, and the user got the same notification twice.
    """
    db = DB(Path(tempfile.mkdtemp()) / "race.db")
    cfg = {"watchlist": [], "poll_seconds": 20, "sweep_minutes": 5, "backfill_days": 90,
           "catchup_hours": 24, "alerts": RULES, "user_agent": "K test@test.io",
           "ntfy_server": "x", "ntfy_topic": "t"}
    p = Poller(db, cfg)
    db.set_setting("alert_rules", RULES)
    db.insert_filing({"accession": "race-26-1", "cik": "1", "ticker": "ENHA", "company": "E",
                      "form": "8-K", "filed_at": now_iso(1), "url": "u", "source": "live"})
    db.update_filing("race-26-1", parsed=1, items="5.02,7.01")

    sent = []

    async def slow_send(server, topic, alert, http=None):
        sent.append(alert)
        await asyncio.sleep(0.05)          # the window the race used to open in
        return True
    monkeypatch.setattr("app.poller.send_ntfy", slow_send)

    async def both_at_once():
        await asyncio.gather(p.evaluate_alerts(only="race-26-1"), p.evaluate_alerts())

    asyncio.run(both_at_once())

    assert len(sent) == 1, f"sent {len(sent)} alerts for one filing"
    assert len(db.recent_alerts()) == 1
    assert db.conn.execute("SELECT alerted FROM filings WHERE accession='race-26-1'"
                           ).fetchone()["alerted"] == 1


def test_catchup_window_covers_actual_downtime():
    """Regression: the routine sweep looks back 2 days, so a longer outage left a hole.

    Real case: machine off from Wed 23:15 to Sat 11:30. The sweep recovered only the last
    two days, and Wednesday's post-close filings were never collected at all.
    """
    from app.poller import catchup_since

    now = datetime.now(timezone.utc)

    # down for 60 hours: the window must reach back past the whole outage
    last = (now - timedelta(hours=60)).isoformat()
    since = datetime.fromisoformat(catchup_since(last, backfill_days=90))
    assert since < now - timedelta(hours=60), "window must cover the full downtime"
    assert since >= now - timedelta(hours=62), "with an hour of overlap, not more"

    # a database untouched for a year is capped at backfill_days, not a year of requests
    stale = (now - timedelta(days=365)).isoformat()
    capped = datetime.fromisoformat(catchup_since(stale, backfill_days=90))
    assert capped >= now - timedelta(days=90, minutes=1)

    # a brief restart isn't worth a catch-up; the normal sweep covers it
    assert catchup_since((now - timedelta(minutes=5)).isoformat(), backfill_days=90) is None
