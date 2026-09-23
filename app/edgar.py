"""Talking to SEC EDGAR: rate-limited client, ticker map, live feed, per-company history."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from defusedxml.ElementTree import fromstring as safe_fromstring

log = logging.getLogger("secwatch.edgar")
ET_TZ = ZoneInfo("America/New_York")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CURRENT_FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"

ATOM = "{http://www.w3.org/2005/Atom}"
# e.g. "4 - Boston Scientific Corp (0000885725) (Issuer)"
TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)\s*$")
ACC_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
# roles that mean "this filing is ABOUT the company" (not the insider/fund doing the filing)
COMPANY_ROLES = {"issuer", "filer", "subject"}


class SecError(Exception):
    pass


class EdgarClient:
    """SEC fair-access rules: identify yourself, stay under 10 requests/second."""

    def __init__(self, user_agent: str, max_rps: float = 6.0):
        self.min_interval = 1.0 / max_rps
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.last_error: str | None = None
        self.http = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )

    async def close(self):
        await self.http.aclose()

    async def get(self, url: str, params: dict | None = None) -> httpx.Response:
        for attempt in range(4):
            async with self._lock:
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            try:
                r = await self.http.get(url, params=params)
            except httpx.HTTPError as e:
                self.last_error = f"network: {e!r}"
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                self.last_error = None
                return r
            if r.status_code == 403:
                self.last_error = ("SEC returned 403 Forbidden. Check SEC_USER_AGENT in .env "
                                   "(needs a name and a real email).")
                raise SecError(self.last_error)
            if r.status_code == 404:
                raise SecError(f"404 {url}")
            if r.status_code in (429, 500, 502, 503, 504):
                self.last_error = f"SEC {r.status_code}, backing off"
                await asyncio.sleep(10 * (attempt + 1))
                continue
            raise SecError(f"HTTP {r.status_code} for {url}")
        raise SecError(self.last_error or f"failed: {url}")

    # ---------- ticker <-> CIK ----------
    async def ticker_map(self) -> dict[str, dict]:
        r = await self.get(TICKERS_URL)
        return parse_ticker_map(r.json())

    # ---------- live feed (every company, newest first) ----------
    async def current_feed(self, start: int = 0, count: int = 100) -> list[dict]:
        r = await self.get(CURRENT_FEED_URL, params={
            "action": "getcurrent", "type": "", "company": "", "dateb": "",
            "owner": "include", "start": start, "count": count, "output": "atom",
        })
        return parse_current_feed(r.text)

    # ---------- per-company history ----------
    async def submissions(self, cik: str) -> dict:
        r = await self.get(SUBMISSIONS_URL.format(cik=cik.zfill(10)))
        return r.json()

    async def filing_index(self, cik: str, accession: str) -> list[str]:
        """File names inside a filing folder."""
        url = f"{ARCHIVE_BASE}/{int(cik)}/{accession.replace('-', '')}/index.json"
        r = await self.get(url)
        return [i["name"] for i in r.json().get("directory", {}).get("item", [])]

    async def fetch_text(self, url: str) -> str:
        return (await self.get(url)).text


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested without network)
# ---------------------------------------------------------------------------

def parse_ticker_map(data: dict) -> dict[str, dict]:
    out = {}
    for row in data.values():
        t = str(row["ticker"]).upper()
        out[t] = {"ticker": t, "cik": str(row["cik_str"]).zfill(10), "name": row["title"]}
    return out


def parse_current_feed(xml_text: str) -> list[dict]:
    root = safe_fromstring(xml_text)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        title = (e.findtext(f"{ATOM}title") or "").strip()
        m = TITLE_RE.match(title)
        if not m:
            continue
        link = e.find(f"{ATOM}link")
        href = link.get("href") if link is not None else None
        acc = None
        for src in (e.findtext(f"{ATOM}id") or "", e.findtext(f"{ATOM}summary") or "", href or ""):
            am = ACC_RE.search(src)
            if am:
                acc = am.group(1)
                break
        if not acc:
            continue
        out.append({
            "accession": acc,
            "form": m["form"].strip(),
            "company": m["name"].strip(),
            "cik": m["cik"],
            "role": m["role"].strip().lower(),
            "filed_at": to_utc_iso(e.findtext(f"{ATOM}updated") or ""),
            "url": href,
        })
    return out


def parse_submissions(data: dict, since_iso: str | None = None) -> list[dict]:
    """Recent filings from data.sec.gov/submissions JSON."""
    cik = str(data.get("cik", "")).zfill(10)
    name = data.get("name")
    rec = data.get("filings", {}).get("recent", {})
    accs = rec.get("accessionNumber", [])
    out = []
    for i, acc in enumerate(accs):
        accepted = rec.get("acceptanceDateTime", [""] * len(accs))[i]
        # no acceptance time (rare, older filings): fall back to the filing date at
        # EDGAR's opening time, 06:00 US Eastern, so DST is handled by the zone itself
        filed_at = edgar_acceptance_to_utc(accepted) if accepted else \
            to_utc_iso(rec["filingDate"][i] + "T06:00:00")
        if since_iso and filed_at < since_iso:
            continue
        primary = (rec.get("primaryDocument") or [""] * len(accs))[i]
        out.append({
            "accession": acc,
            "cik": cik,
            "company": name,
            "form": rec["form"][i],
            "filed_at": filed_at,
            "items": (rec.get("items") or [""] * len(accs))[i] or None,
            "primary_doc": primary,
            "url": f"{ARCHIVE_BASE}/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm",
        })
    return out


def to_utc_iso(s: str) -> str:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET_TZ)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def edgar_acceptance_to_utc(s: str) -> str:
    """acceptanceDateTime from the submissions JSON.

    The 'Z' is accurate: these really are UTC. Verified against a filing's "Accepted"
    field on its EDGAR index page, which SEC prints in US Eastern —
    JSON 2026-09-22T00:41Z == index page "2026-09-21 20:41:22" (EDT, UTC-4).
    Treating it as Eastern stamps every filing 4-5 hours into the future, which
    silently widens the alert window.
    """
    s = s.strip().rstrip("Z").split(".")[0]
    dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def undo_et_misread(stored_iso: str) -> str:
    """Repair a filed_at written by the versions that read acceptanceDateTime as US Eastern.

    Those versions did ET.localize(V) -> UTC, so the stored timestamp's *Eastern wall time*
    is the original value V, which was UTC all along. Reversible exactly, DST included.
    """
    dt = datetime.fromisoformat(stored_iso)
    return dt.astimezone(ET_TZ).replace(tzinfo=timezone.utc).isoformat()


def pick_form4_xml(names: list[str]) -> str | None:
    xmls = [n for n in names if n.lower().endswith(".xml") and "index" not in n.lower()
            and "/" not in n]
    # prefer obvious ownership-document names
    for n in xmls:
        low = n.lower()
        if "form4" in low or "form5" in low or "form3" in low or low.startswith(("doc4", "wf-", "edgar")):
            return n
    return xmls[0] if xmls else None


def raw_xml_from_primary(primary_doc: str) -> str | None:
    """'xslF345X05/wk-form4_1.xml' is the rendered view; the raw XML is the bare file name."""
    if not primary_doc or not primary_doc.lower().endswith(".xml"):
        return None
    return primary_doc.rsplit("/", 1)[-1]


ITEMS_8K = {
    "1.01": "Material agreement", "1.02": "Agreement terminated", "1.03": "Bankruptcy",
    "1.05": "Cybersecurity incident", "2.01": "Acquisition / disposal", "2.02": "Results",
    "2.03": "New debt", "2.04": "Debt acceleration", "2.05": "Restructuring costs",
    "2.06": "Impairment", "3.01": "Delisting notice", "3.02": "Unregistered share sale",
    "3.03": "Holder rights changed", "4.01": "Auditor change", "4.02": "Non-reliance on financials",
    "5.01": "Change in control", "5.02": "Exec / director change", "5.03": "Bylaws amended",
    "5.07": "Shareholder vote", "7.01": "Reg FD disclosure", "8.01": "Other events",
    "9.01": "Exhibits",
}
ITEM_RE = re.compile(r"Item\s+(\d\.\d{2})", re.I)


def parse_8k_items_from_index(html: str) -> str | None:
    found = []
    for m in ITEM_RE.finditer(html):
        if m.group(1) not in found:
            found.append(m.group(1))
    return ",".join(found) or None
