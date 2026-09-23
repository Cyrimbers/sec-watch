"""Decide whether a filing deserves a phone alert, and send it via ntfy."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .edgar import ITEMS_8K
from .form4 import fmt_money, fmt_shares

log = logging.getLogger("secwatch.alerts")


def _age_ok(filed_at: str, window_min: int) -> bool:
    try:
        filed = datetime.fromisoformat(filed_at)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - filed <= timedelta(minutes=window_min)


def build_alert(f: dict, detail: dict | None, rules: dict) -> dict | None:
    """f = filings row, detail = parsed Form 4 summary (or None). Returns a message or None."""
    if not _age_ok(f["filed_at"], int(rules.get("alert_window_minutes", 90))):
        return None
    return matches_rules(f, detail, rules)


def matches_rules(f: dict, detail: dict | None, rules: dict) -> dict | None:
    """The rule check on its own, with no freshness test — also used for the catch-up digest."""
    ticker = f.get("ticker") or f.get("company") or "?"
    form = f["form"]
    url = f.get("url")

    if form in ("4", "4/A") and detail:
        who = detail.get("owner") or "Insider"
        role = detail.get("role") or ""
        who_line = f"{who} ({role})" if role else who
        if detail["signal"] == "BUY" and rules.get("insider_buy"):
            if detail["buy_value"] < float(rules.get("insider_buy_min_usd", 0)):
                return None
            price = f" @ ${detail['buy_avg_price']:,.2f}" if detail.get("buy_avg_price") else ""
            return {
                "title": f"{ticker}: insider BUY {fmt_money(detail['buy_value'])}",
                "message": f"{who_line} bought {fmt_shares(detail['buy_shares'])} sh{price}\n"
                           f"Form {form} · code P (open market or private purchase)",
                "priority": 5, "tags": ["green_circle", "moneybag"], "click": url,
            }
        if detail["signal"] == "SELL" and rules.get("insider_sell"):
            if detail["sell_value"] < float(rules.get("insider_sell_min_usd", 0)):
                return None
            if rules.get("skip_10b5_1_sales") and detail.get("sell_all_10b5_1"):
                return None
            price = f" @ ${detail['sell_avg_price']:,.2f}" if detail.get("sell_avg_price") else ""
            # the plan marker belongs in the title: a collapsed phone notification truncates
            # the body, and "scheduled months ago" vs "decided this week" is the whole point
            plan = " (10b5-1)" if detail.get("sell_all_10b5_1") else ""
            return {
                "title": f"{ticker}: insider SELL {fmt_money(detail['sell_value'])}{plan}",
                "message": f"{who_line} sold {fmt_shares(detail['sell_shares'])} sh{price}\n"
                           f"Form {form} · code S (open market or private sale)",
                "priority": 4, "tags": ["red_circle"], "click": url,
            }
        return None

    forms = {x.upper() for x in rules.get("forms", [])}
    if form.upper() in forms:
        extra = ""
        if form.upper().startswith("8-K") and f.get("items"):
            labels = [ITEMS_8K.get(i.strip(), i.strip()) for i in f["items"].split(",")
                      if i.strip() and i.strip() != "9.01"]
            extra = ", ".join(labels)
        return {
            "title": f"{ticker}: new {form}",
            "message": (extra or f"{f.get('company') or ticker} filed a {form}"),
            "priority": 4 if "13D" in form.upper() else 3,
            "tags": ["page_facing_up"], "click": url,
        }
    return None


def build_digest(items: list[dict], hours: int) -> dict | None:
    """One notification summarising alert-worthy filings that landed while the app was off.

    Without this, anything filed overnight is only ever visible on the dashboard: it is
    already outside alert_window_minutes by the time the app starts, so it is skipped.
    """
    if not items:
        return None
    buys = [i for i in items if i["kind"] == "BUY"]
    sells = [i for i in items if i["kind"] == "SELL"]
    others = [i for i in items if i["kind"] == "OTHER"]
    bits = []
    for label, group in (("buy", buys), ("sale", sells), ("filing", others)):
        if group:
            bits.append(f"{len(group)} {label}{'' if len(group) == 1 else 's'}")
    lines = []
    for i in (buys + sells + others)[:6]:
        mark = {"BUY": "+", "SELL": "-", "OTHER": " "}[i["kind"]]
        amount = f" {fmt_money(i['value'])}" if i.get("value") else ""
        who = f" {i['who']}" if i.get("who") else ""
        lines.append(f"{mark} {i['ticker']}{amount}{who}")
    extra = len(items) - len(lines)
    if extra > 0:
        lines.append(f"  +{extra} more on the dashboard")
    return {
        "title": f"While you were away: {', '.join(bits)}",
        "message": f"Filed in the last {hours}h:\n" + "\n".join(lines),
        "priority": 4,
        "tags": ["bell"],
        "click": (buys or sells or others)[0].get("url"),
    }


async def send_ntfy(server: str, topic: str, alert: dict, http: httpx.AsyncClient | None = None) -> bool:
    if not topic:
        log.warning("NTFY_TOPIC not set; alert not sent: %s", alert["title"])
        return False
    payload = {
        "topic": topic,
        "title": alert["title"],
        "message": alert["message"],
        "priority": alert.get("priority", 3),
        "tags": alert.get("tags", []),
    }
    if alert.get("click"):
        payload["click"] = alert["click"]
        payload["actions"] = [{"action": "view", "label": "Open filing", "url": alert["click"]}]
    own = http is None
    http = http or httpx.AsyncClient(timeout=15)
    try:
        r = await http.post(server + "/", json=payload)
        ok = r.status_code < 300
        if not ok:
            log.warning("ntfy %s: %s", r.status_code, r.text[:200])
        return ok
    except httpx.HTTPError as e:
        log.warning("ntfy failed: %r", e)
        return False
    finally:
        if own:
            await http.aclose()
