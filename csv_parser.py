"""Parses an uploaded NSE options CSV export into the normalized chain shape
analysis.py expects. Two NSE export formats are auto-detected:

1. "Most Active Contracts (by Volume)" - a LONG-format export (one row per
   strike + option side: SYMBOL, EXPIRY DATE, OPTION TYPE, STRIKE PRICE, LTP,
   VOLUME, OPEN INTEREST, VALUE OF UNDERLYING). Carries expiry and spot
   directly on every row, but never implied volatility or "change in OI
   since morning" - those leg keys are simply omitted (not set to 0, which
   would misrepresent "no data" as "zero"). It also only lists the
   most-active-by-volume contracts, not the full chain.

2. The full "Option Chain" export - a WIDE-format sheet (CALLS columns |
   STRIKE | PUTS columns), one row per strike, covering the whole chain for
   a single expiry. It carries real implied volatility and today's OI change
   per leg, but no spot value and no expiry anywhere in its own data:
     - expiry is recovered from the filename (NSE's own download name
       always embeds it, e.g. "option-chain-ED-NIFTY-21-Jul-2026.csv")
     - spot is estimated via put-call parity (C - P = S - K near the money:
       the strike where call and put LTP are closest gives the best local
       estimate) - a standard approximation, not a guess, but still an
       estimate, so callers should treat it as such (see "spot_source" in
       the returned dict).

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
                        # present only when the sheet actually provides them:
                        "changeinOpenInterest": int,
                        "impliedVolatility": float,
                        "change": float,
                    },
                    "PE": { ... same keys ... },
                },
                ...
            ],
            "expiryDates": ["21-Jul-2026", ...],   # sorted chronologically
            "underlyingValue": 24334.30,
            "timestamp": "<upload timestamp>",
        },
        "india_vix": None,   # never derivable from either supported format
        "spot_source": "underlying_value_column" | "put_call_parity_estimate",
    }
"""
import csv
import datetime as dt
import io
import re
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


def _clean_optional_number(raw: str) -> Optional[float]:
    """Like _clean_number but returns None (not 0.0) for a blank/"-" cell -
    for fields where "-" means "no data" rather than a legitimate zero (e.g.
    implied volatility on a strike that never traded)."""
    raw = (raw or "").strip().replace(",", "")
    if raw in ("", "-"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _find_col(headers: list, *prefixes: str) -> Optional[int]:
    for i, h in enumerate(headers):
        if any(h.startswith(p) for p in prefixes):
            return i
    return None


def _find_exact(headers: list, start: int, end: int, name: str) -> Optional[int]:
    for i in range(start, end):
        if headers[i] == name:
            return i
    return None


def _parse_expiry_date(date_str: str) -> dt.date:
    return dt.datetime.strptime(date_str.strip(), "%d-%b-%Y").date()


# ------------------------------------------------------------------------
# Format 1: "Most Active Contracts (by Volume)" - long format
# ------------------------------------------------------------------------


def _parse_long_format(rows: list, timestamp: str, symbol: str) -> dict:
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
        "spot_source": "underlying_value_column",
    }


# ------------------------------------------------------------------------
# Format 2: full Option Chain export - wide format (CALLS | STRIKE | PUTS)
# ------------------------------------------------------------------------

_FILENAME_DATE_RE = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4})")


def _expiry_from_filename(filename: Optional[str]) -> str:
    if filename:
        m = _FILENAME_DATE_RE.search(filename)
        if m:
            candidate = m.group(1)
            try:
                _parse_expiry_date(candidate)
                return candidate
            except ValueError:
                pass
    raise CSVParseError(
        "This option-chain export has no expiry date in its own data - it's "
        "normally embedded in NSE's filename (e.g. "
        "option-chain-ED-NIFTY-21-Jul-2026.csv). Re-download from NSE without "
        "renaming the file, or rename it to include the expiry as DD-Mon-YYYY."
    )


def _estimate_spot_put_call_parity(strikes: dict) -> Optional[float]:
    """Put-call parity (C - P = S - K near the money) to back out an implied
    spot when the sheet has no spot column at all. Uses the strike where call
    and put LTP are closest - the best local approximation of ATM."""
    best = None
    for entry in strikes.values():
        ce, pe = entry.get("CE"), entry.get("PE")
        if not ce or not pe:
            continue
        ce_ltp, pe_ltp = ce.get("lastPrice"), pe.get("lastPrice")
        if not ce_ltp or not pe_ltp:
            continue
        diff = abs(ce_ltp - pe_ltp)
        if best is None or diff < best[0]:
            best = (diff, entry["strikePrice"] + ce_ltp - pe_ltp)
    return round(best[1], 2) if best else None


def _is_wide_chain_format(rows: list) -> bool:
    return bool(rows) and any(_norm(c) == "CALLS" for c in rows[0])


def _parse_wide_format(
    rows: list, timestamp: str, symbol: str, filename: Optional[str]
) -> dict:
    header_row_idx = next(
        (i for i, r in enumerate(rows) if any(_norm(c) == "STRIKE" for c in r)), None
    )
    if header_row_idx is None:
        raise CSVParseError("Could not find a STRIKE column in this option-chain export.")
    headers = [_norm(c) for c in rows[header_row_idx]]
    strike_col = next(i for i, h in enumerate(headers) if h == "STRIKE")

    def side_cols(start: int, end: int) -> dict:
        return {
            "oi": _find_exact(headers, start, end, "OI"),
            "chg_oi": _find_exact(headers, start, end, "CHNG IN OI"),
            "volume": _find_exact(headers, start, end, "VOLUME"),
            "iv": _find_exact(headers, start, end, "IV"),
            "ltp": _find_exact(headers, start, end, "LTP"),
            "chg": _find_exact(headers, start, end, "CHNG"),
        }

    ce_cols = side_cols(0, strike_col)
    pe_cols = side_cols(strike_col + 1, len(headers))

    missing_ce = [k for k in ("oi", "volume", "ltp") if ce_cols[k] is None]
    missing_pe = [k for k in ("oi", "volume", "ltp") if pe_cols[k] is None]
    if missing_ce or missing_pe:
        raise CSVParseError(
            f"Could not find required CALLS column(s) {missing_ce} or PUTS "
            f"column(s) {missing_pe} in this option-chain export."
        )

    expiry = _expiry_from_filename(filename)

    def build_leg(row: list, cols: dict) -> dict:
        leg = {
            "lastPrice": _clean_number(row[cols["ltp"]]),
            "openInterest": int(_clean_number(row[cols["oi"]])),
            "totalTradedVolume": int(_clean_number(row[cols["volume"]])),
        }
        if cols["chg_oi"] is not None:
            val = _clean_optional_number(row[cols["chg_oi"]])
            if val is not None:
                leg["changeinOpenInterest"] = int(val)
        if cols["iv"] is not None:
            val = _clean_optional_number(row[cols["iv"]])
            if val is not None:
                leg["impliedVolatility"] = val
        if cols["chg"] is not None:
            val = _clean_optional_number(row[cols["chg"]])
            if val is not None:
                leg["change"] = val
        return leg

    strikes: dict = {}
    for row in rows[header_row_idx + 1 :]:
        if len(row) <= strike_col:
            continue
        strike = _clean_number(row[strike_col])
        if not strike:
            continue
        key = int(round(strike))
        entry = strikes.setdefault(key, {"strikePrice": key, "expiryDate": expiry})
        entry["CE"] = build_leg(row, ce_cols)
        entry["PE"] = build_leg(row, pe_cols)

    if not strikes:
        raise CSVParseError(f"No usable {symbol} strike rows found in the uploaded CSV.")

    spot = _estimate_spot_put_call_parity(strikes)
    if spot is None:
        raise CSVParseError(
            "Could not estimate the underlying spot value via put-call parity "
            "- no strike had both a CALL and PUT last-traded price."
        )

    return {
        "records": {
            "data": list(strikes.values()),
            "expiryDates": [expiry],
            "underlyingValue": spot,
            "timestamp": timestamp,
        },
        "india_vix": None,
        "spot_source": "put_call_parity_estimate",
    }


# ------------------------------------------------------------------------


def parse_option_chain_csv(
    content: Union[str, bytes],
    timestamp: str,
    symbol: str = "NIFTY",
    filename: Optional[str] = None,
) -> dict:
    """Parse one uploaded NSE CSV's raw text/bytes into the normalized chain
    shape, auto-detecting which of the two supported export formats it is."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    else:
        content = content.lstrip("﻿")

    rows = list(csv.reader(io.StringIO(content)))
    if not rows:
        raise CSVParseError("Uploaded CSV is empty")

    if _is_wide_chain_format(rows):
        return _parse_wide_format(rows, timestamp, symbol, filename)
    return _parse_long_format(rows, timestamp, symbol)
