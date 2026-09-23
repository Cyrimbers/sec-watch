"""Fill the database with sample filings so the dashboard can be explored offline.

    python tools/demo_seed.py            # writes data/secwatch.db
    SECWATCH_DEMO=1 uvicorn app.main:app --port 8080

In demo mode the poller never starts, so no SEC access (and no user agent) is needed.
The filings below are invented; the XML they are parsed from follows SEC's real
ownership-document schema, so every number goes through the same code path as live data.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DB_PATH  # noqa: E402
from app.db import DB  # noqa: E402
from app.form4 import parse_ownership_xml  # noqa: E402

NOW = datetime.now(timezone.utc)

COMPANIES = {
    "AAPL": ("0000320193", "Apple Inc."),
    "MSFT": ("0000789019", "Microsoft Corp"),
    "NVDA": ("0001045810", "NVIDIA Corp"),
    "JPM": ("0000019617", "JPMorgan Chase & Co"),
}


def form4(ticker: str, owner: str, title: str, code: str, lines, footnote: str | None = None,
          director: bool = False, officer: bool = True) -> str:
    cik, name = COMPANIES[ticker]
    owned_start = 250_000
    rows = "".join(f"""
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>{(NOW - timedelta(days=2)).date()}</value></transactionDate>
            <transactionCoding><transactionFormType>4</transactionFormType>
                <transactionCode>{code}</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{sh}</value></transactionShares>
                <transactionPricePerShare><value>{px}</value>{'<footnoteId id="F1"/>' if footnote else ''}</transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>{'A' if code in 'PA' else 'D'}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{owned}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>"""
                   for n, (sh, px) in enumerate(lines)
                   for owned in [owned_start + (sh if code in "PA" else -sh) * (n + 1)])
    notes = f"<footnotes><footnote id='F1'>{footnote}</footnote></footnotes>" if footnote else ""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0508</schemaVersion><documentType>4</documentType>
    <aff10b5One>{'1' if footnote and '10b5-1' in footnote else '0'}</aff10b5One>
    <issuer><issuerCik>{cik}</issuerCik><issuerName>{name}</issuerName>
        <issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0001111111</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isOfficer>{'1' if officer else '0'}</isOfficer>
            {f'<officerTitle>{title}</officerTitle>' if officer else ''}
            <isDirector>{'1' if director else '0'}</isDirector></reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>{rows}</nonDerivativeTable>{notes}
</ownershipDocument>"""


SAMPLES = [
    # (minutes ago, ticker, form, xml or None, items)
    (35, "NVDA", "4", form4("NVDA", "HARTLEY ANNA M", "Chief Financial Officer", "P",
                            [(18000, 121.4), (6500, 121.95)], director=True), None),
    (95, "JPM", "8-K", None, "2.02,9.01"),
    (240, "MSFT", "4", form4("MSFT", "OKONKWO DAVID", "Executive Vice President", "S",
                             [(12500, 498.2), (9800, 499.65)],
                             footnote="This transaction was effected pursuant to a Rule 10b5-1 "
                                      "trading plan adopted on 14 March 2026."), None),
    (1500, "AAPL", "4", form4("AAPL", "REYES CARMEN", "Chief Operating Officer", "A",
                              [(40000, 0)]), None),
    (2300, "NVDA", "4", form4("NVDA", "BARCLAY JOHN T", "", "P", [(9000, 118.75)],
                              footnote="Weighted average price; trades ranged from $118.50 to "
                                       "$119.10.", director=True, officer=False), None),
    (3100, "JPM", "SCHEDULE 13D/A", None, None),
    (4300, "AAPL", "144", None, None),
    (5200, "MSFT", "10-Q", None, None),
]


def main() -> None:
    db = DB(DB_PATH)
    for ticker, (cik, name) in COMPANIES.items():
        db.add_ticker(ticker, cik, name)
        db.mark_backfilled(ticker)
    db.set_setting("seeded", True)
    db.set_setting("last_digest_at", NOW.isoformat())

    for i, (mins, ticker, form, xml, items) in enumerate(SAMPLES):
        cik, company = COMPANIES[ticker]
        acc = f"{cik}-26-{i + 100:06d}"
        db.insert_filing({
            "accession": acc, "cik": cik, "ticker": ticker, "company": company, "form": form,
            "filed_at": (NOW - timedelta(minutes=mins)).replace(microsecond=0).isoformat(),
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
            "items": items, "source": "demo",
        })
        fields = {"parsed": 1, "alerted": 1}
        if xml:
            doc = parse_ownership_xml(xml)
            db.replace_transactions(acc, doc["transactions"])
            fields.update(signal=doc["signal"], buy_value=doc["buy_value"],
                          sell_value=doc["sell_value"], insider=doc["owner"],
                          insider_role=doc["role"], codes=",".join(doc["codes"]))
        db.update_filing(acc, **fields)

    db.log_alert(None, "NVDA: insider BUY $2.98M",
                 "Hartley Anna M (Chief Financial Officer, Director) bought 24,500 sh @ $121.55", 1)
    print(f"Seeded {len(SAMPLES)} sample filings into {DB_PATH}")
    print("Start with:  SECWATCH_DEMO=1 uvicorn app.main:app --port 8080")


if __name__ == "__main__":
    main()
