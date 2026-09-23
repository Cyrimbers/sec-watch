"""Parse SEC ownership documents (Forms 3, 4, 5 XML) into transactions + a one-line verdict."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # types only; parsing goes through defusedxml

from defusedxml.ElementTree import fromstring as safe_fromstring

CODE_LABELS = {
    # SEC's own wording: P and S cover private, negotiated transactions as well as
    # on-exchange ones. A PIPE subscription and a market buy share the code P, so a price
    # well away from the market is not necessarily an error — the footnotes say which it was.
    "P": "Open market or private purchase",
    "S": "Open market or private sale",
    "A": "Grant / award",
    "M": "Option exercise",
    "X": "Option exercise (in the money)",
    "C": "Conversion",
    "F": "Shares withheld for tax",
    "G": "Gift",
    "D": "Sold back to issuer",
    "J": "Other",
    "K": "Equity swap",
    "I": "Discretionary (plan)",
    "L": "Small acquisition",
    "W": "Inheritance",
    "Z": "Voting trust",
    "E": "Short derivative expired",
    "H": "Long derivative expired",
    "O": "OTM option exercise",
    "U": "Tender of shares",
    "V": "Voluntarily reported",
}


def _txt(node, path: str) -> str | None:
    if node is None:
        return None
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    s = (v.text if v is not None else el.text) or ""
    s = s.strip()
    return s or None


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in ("1", "true", "y", "yes")


NEGATION_RE = re.compile(
    r"\b(?:not|no|without|non)\b[^.;]{0,80}?(?:10b5-?1|trading\s+plan)"
    r"|(?:10b5-?1|trading\s+plan)[^.;]{0,40}?\bwas\s+not\b", re.I)
PLAN_RE = re.compile(r"10b5-?1", re.I)


def is_plan_footnote(text: str) -> bool:
    """True only when a footnote affirmatively says the trade was made under a 10b5-1 plan."""
    if not PLAN_RE.search(text):
        return False
    return not NEGATION_RE.search(text)


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def owner_role(rel) -> str:
    if rel is None:
        return ""
    parts = []
    if _truthy(_txt(rel, "isOfficer")):
        parts.append(_txt(rel, "officerTitle") or "Officer")
    if _truthy(_txt(rel, "isDirector")):
        parts.append("Director")
    if _truthy(_txt(rel, "isTenPercentOwner")):
        parts.append("10% Owner")
    if _truthy(_txt(rel, "isOther")):
        parts.append(_txt(rel, "otherText") or "Other")
    return ", ".join(parts)


KEEP_UPPER = {"LLC", "LP", "LLP", "PLC", "PLLC", "LLLP", "AG", "NV", "SA", "SE", "GP", "BV",
              "II", "III", "IV", "V", "VI", "VII", "PC", "PA", "USA", "UK"}


def tidy_name(name: str | None) -> str:
    """Normalise capitalisation only, keeping EDGAR's own word order.

    EDGAR records individuals surname-first ('MAHONEY MICHAEL F'), but filing agents are
    inconsistent about case, and there is no reliable way to tell 'Verma Shiv' (surname
    first) from a name genuinely entered first-name-first. Reordering therefore gets some
    names wrong; leaving the order alone never does. This also matches how OpenInsider and
    the other insider trackers display names, so filings are easy to cross-check.
    """
    if not name:
        return ""
    name = " ".join(name.split())
    if not (name.isupper() or name.islower()):
        return name                                   # mixed case: filer typed it, leave it
    out = []
    for token in name.split():
        bare = token.strip(".,")
        if bare.upper() in KEEP_UPPER:
            out.append(token.upper())
        else:
            # capitalise after each space, apostrophe or hyphen: O'CONNOR -> O'Connor
            out.append(re.sub(r"(^|['\-])([a-z])", lambda m: m.group(1) + m.group(2).upper(),
                              token.lower()))
    return " ".join(out)


def parse_ownership_xml(xml_text: str) -> dict:
    # some filings ship with a leading BOM/whitespace or wrapped in <XML> tags
    start = xml_text.find("<ownershipDocument")
    end = xml_text.rfind("</ownershipDocument>")
    if start == -1 or end == -1:
        raise ValueError("not an ownershipDocument")
    root = safe_fromstring(xml_text[start:end + len("</ownershipDocument>")])
    _strip_ns(root)

    issuer = root.find("issuer")
    owners = []
    for ro in root.findall("reportingOwner"):
        owners.append({
            "name": tidy_name(_txt(ro, "reportingOwnerId/rptOwnerName")),
            "role": owner_role(ro.find("reportingOwnerRelationship")),
        })
    owner_name = " & ".join(o["name"] for o in owners if o["name"])
    role = next((o["role"] for o in owners if o["role"]), "")

    # 10b5-1 plan detection.
    # Forms from 2023 on carry a document-level checkbox, which is authoritative when present.
    # Otherwise fall back to the footnotes — but only when the wording is affirmative: plenty
    # of filings carry a footnote saying the trade was NOT made under a plan, and matching
    # "10b5-1" alone would flag those too (and then the skip-plan-sales rule mutes a real
    # discretionary sale).
    cb = root.find("aff10b5One")
    doc_flag = _truthy(_txt(root, "aff10b5One")) if cb is not None else None
    plan_footnotes = set()
    footnote_text: dict[str, str] = {}
    for fn in root.findall("footnotes/footnote"):
        text = " ".join("".join(fn.itertext()).split())
        footnote_text[fn.get("id")] = text
        if is_plan_footnote(text):
            plan_footnotes.add(fn.get("id"))
    doc_10b5_1 = bool(doc_flag)

    txs = []
    for table, tag, deriv in (("nonDerivativeTable", "nonDerivativeTransaction", False),
                              ("derivativeTable", "derivativeTransaction", True)):
        for t in root.findall(f"{table}/{tag}"):
            code = _txt(t, "transactionCoding/transactionCode")
            shares = _num(_txt(t, "transactionAmounts/transactionShares"))
            price = _num(_txt(t, "transactionAmounts/transactionPricePerShare"))
            fn_ids = {f.get("id") for f in t.iter("footnoteId")}
            notes = " ".join(footnote_text[i] for i in sorted(fn_ids) if footnote_text.get(i))
            txs.append({
                "owner": owner_name,
                "role": role,
                "security": _txt(t, "securityTitle"),
                "date": _txt(t, "transactionDate"),
                "code": code,
                "acq_disp": _txt(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                "shares": shares,
                "price": price,
                "value": round(shares * price, 2) if shares and price else 0.0,
                "owned_after": _num(_txt(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                "direct": _txt(t, "ownershipNature/directOrIndirectOwnership"),
                "plan_10b5_1": doc_10b5_1 or (doc_flag is not False and bool(fn_ids & plan_footnotes)),
                "derivative": deriv,
                # the filing's own explanation of the line: private placement, weighted
                # average price, indirect holding, and so on
                "footnotes": notes or None,
            })

    holdings = len(root.findall("nonDerivativeTable/nonDerivativeHolding")) + \
        len(root.findall("derivativeTable/derivativeHolding"))

    return {
        "form": _txt(root, "documentType") or "",
        "ticker": (_txt(issuer, "issuerTradingSymbol") or "").upper(),
        "issuer": _txt(issuer, "issuerName"),
        "issuer_cik": (_txt(issuer, "issuerCik") or "").zfill(10),
        "owner": owner_name,
        "role": role,
        "transactions": txs,
        # a Form 4 with holdings but no transactions is valid: it restates a position
        # without anything having been bought or sold
        "holdings_only": not txs and holdings > 0,
        **summarise(txs),
    }


def summarise(txs: list[dict]) -> dict:
    """Roll individual lines up to: was this a buy, a sale or neither, and how big."""
    buys = [t for t in txs if t["code"] == "P" and not t["derivative"]]
    sells = [t for t in txs if t["code"] == "S" and not t["derivative"]]
    buy_val = sum(t["value"] or 0 for t in buys)
    sell_val = sum(t["value"] or 0 for t in sells)
    buy_sh = sum(t["shares"] or 0 for t in buys)
    sell_sh = sum(t["shares"] or 0 for t in sells)
    if buys and buy_val >= sell_val:
        signal = "BUY"
    elif sells:
        signal = "SELL"
    else:
        signal = "OTHER"
    codes = []
    for t in txs:
        if t["code"] and t["code"] not in codes:
            codes.append(t["code"])
    return {
        "signal": signal,
        "buy_value": round(buy_val, 2),
        "sell_value": round(sell_val, 2),
        "buy_shares": buy_sh,
        "sell_shares": sell_sh,
        "buy_avg_price": round(buy_val / buy_sh, 4) if buy_sh else None,
        "sell_avg_price": round(sell_val / sell_sh, 4) if sell_sh else None,
        "sell_all_10b5_1": bool(sells) and all(t["plan_10b5_1"] for t in sells),
        "codes": codes,
    }


def fmt_money(v: float | None) -> str:
    if not v:
        return "$0"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def fmt_shares(n: float | None) -> str:
    return f"{n:,.0f}" if n else "0"
