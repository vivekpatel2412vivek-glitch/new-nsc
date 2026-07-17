"""Parses an uploaded NSE "Most Active Contracts (by Volume)" CSV export
into the normalized chain shape analysis.py expects.

This is a LONG-format export (one row per strike + option side: SYMBOL,
EXPIRY DATE, OPTION TYPE, STRIKE PRICE, LTP, VOLUME, OPEN INTEREST, VALUE OF
UNDERLYING), unlike NSE's wide-format full option-chain export. It carries
expiry date and underlying (spot) value directly on every row, so neither
needs to be supplied separately.

It does NOT carry implied volatility or a "change in OI since morning"
field - those columns simply don't exist in this report, so the
corresponding leg keys are omitted entirely (not set to 0, which would
misrepresent "no data" as "zero"). Gamma, ATM IV, IV Skew, India VIX, and
"Today's Change in OI" are all unavailable from this data source as a
result, and the rest of the pipeline is expected to render them as
"N/A - not available from this data source" rather than a misleading number.

It also only lists the most-active-by-volume contracts, not the full option
chain - Max Pain / PCR / Support-Resistance computed from it are directional
estimates, not exact full-chain figures.

Normalized return shape::

    {
        "records": {
            "data": [
                {
                    "strikePrice": 24300,
                    "expiryDate": "21-Jul-2026",
                    "CE": {
                        "lastPrice": float,
                        "openInterest": int,
                        "totalTradedVolume": int,
                        # "changeinOpenInterest" and "impliedVolatility" are
                        # intentionally absent - not provided by this export.
                    },
                    "PE": { ... same keys ... },
                },
                ...
            ],
            "expiryDates": ["21-Jul-2026", ...],   # sorted chronologically
            "underlyingValue": 24334.30,
            "timestamp": "<upload timestamp>",
        },
        "india_vix": None,   # not derivable from this data source
    }
"""
import csv
import datetime as dt
import io
from typing import Optional, Union


class CSVParseError(RuntimeError):
    """Raised when the uploaded CSV can't be parsed into a usable chain."""


def _norm(s: str) -> str:
    return " ".join((s or "").strip().upper().split())


def _clean_number(raw: str) -> float:
    raw = (raw or "").strip().replace(",", "")
    if raw in ("", "-"):
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _find_col(headers: list, *prefixes: str) -> Optional[int]:
    for i, h in enumerate(headers):
        if any(h.startswith(p) for p in prefixes):
            return i
    return None


def _parse_expiry_date(date_str: str) -> dt.date:
    return dt.datetime.strptime(date_str.strip(), "%d-%b-%Y").date()


def parse_option_chain_csv(
    content: Union[str, bytes], timestamp: str, symbol: str = "NIFTY"
) -> dict:
    """Parse one uploaded "Most Active Contracts" CSV's raw text/bytes into
    the normalized chain shape."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    else:
        content = content.lstrip("﻿")

    rows = list(csv.reader(io.StringIO(content)))
    if not rows:
        raise CSVParseError("Uploaded CSV is empty")

    headers = [_norm(c) for c in rows[0]]
    col = {
        "symbol": _find_col(headers, "SYMBOL"),
        "expiry": _find_col(headers, "EXPIRY"),
        "option_type": _find_col(headers, "OPTION TYPE"),
        "strike": _find_col(headers, "STRIKE"),
        "ltp": _find_col(headers, "LTP"),
        "volume": _find_col(headers, "VOLUME"),
        "oi": _find_col(headers, "OPEN INTEREST"),
        "underlying": _find_col(headers, "VALUE OF UNDERLYING"),
    }
    missing = [k for k, v in col.items() if v is None and k != "symbol"]
    if missing:
        raise CSVParseError(
            f"Could not find column(s) {missing} in the CSV header - check "
            "it matches NSE's Most Active Contracts export format."
        )

    required_idx = [v for v in col.values() if v is not None]
    strikes: dict = {}
    expiries: set = set()
    spot: Optional[float] = None
    symbol_upper = symbol.upper()

    for row in rows[1:]:
        if len(row) <= max(required_idx):
            continue
        if col["symbol"] is not None and _norm(row[col["symbol"]]) != symbol_upper:
            continue

        strike = _clean_number(row[col["strike"]])
        if not strike:
            continue
        expiry = row[col["expiry"]].strip()
        if not expiry:
            continue
        option_label = _norm(row[col["option_type"]])
        if option_label.startswith("CALL") or option_label == "CE":
            option_type = "CE"
        elif option_label.startswith("PUT") or option_label == "PE":
            option_type = "PE"
        else:
            continue

        leg = {
            "lastPrice": _clean_number(row[col["ltp"]]),
            "openInterest": int(_clean_number(row[col["oi"]])),
            "totalTradedVolume": int(_clean_number(row[col["volume"]])),
        }

        key = (int(round(strike)), expiry)
        entry = strikes.setdefault(
            key, {"strikePrice": int(round(strike)), "expiryDate": expiry}
        )
        entry[option_type] = leg
        expiries.add(expiry)

        if spot is None:
            underlying = _clean_number(row[col["underlying"]])
            spot = underlying or None

    if not strikes:
        raise CSVParseError(f"No usable {symbol} strike rows found in the uploaded CSV.")
    if spot is None:
        raise CSVParseError("Could not read the underlying (spot) value from the uploaded CSV.")

    try:
        sorted_expiries = sorted(expiries, key=_parse_expiry_date)
    except ValueError as exc:
        raise CSVParseError(f"Could not parse an expiry date in the CSV: {exc}") from exc

    return {
        "records": {
            "data": list(strikes.values()),
            "expiryDates": sorted_expiries,
            "underlyingValue": spot,
            "timestamp": timestamp,
        },
        "india_vix": None,
    }
