"""Computes options-chain analytics from raw NSE data.

All functions operate on the raw NSE JSON structure (or the row list
extracted from it) and are pure/side-effect free so they're easy to test.
"""
import datetime as dt
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)


class DataQualityError(RuntimeError):
    """Raised when the raw NSE payload fails basic sanity checks."""


def verify_raw_chain(raw: dict) -> None:
    """Sanity-check the raw NSE payload before it is trusted for analysis.

    India VIX is intentionally not checked here - it's None whenever the
    data source (e.g. the Most Active Contracts CSV) doesn't provide implied
    volatility at all, which is a normal, expected state, not a data-quality
    problem.
    """
    records = raw.get("records") or {}
    data_rows = records.get("data") or []
    spot = records.get("underlyingValue")
    expiries = records.get("expiryDates") or []

    if not data_rows:
        raise DataQualityError("Option chain contains no strike rows")
    if not spot or spot <= 0:
        raise DataQualityError(f"Implausible underlying spot value: {spot}")
    if not expiries:
        raise DataQualityError("Option chain contains no expiry dates")


def nearest_expiry(raw: dict) -> str:
    return raw["records"]["expiryDates"][0]


def filter_rows(raw: dict, expiry: Optional[str] = None) -> list[dict]:
    """Return the strike rows for a single expiry (nearest by default)."""
    expiry = expiry or nearest_expiry(raw)
    rows = raw["records"]["data"]
    return [r for r in rows if r.get("expiryDate") == expiry]


def lookup_current_price(
    raw: dict, strike: float, option_type: str, expiry: str
) -> Optional[float]:
    """Current LTP for one (strike, option_type, expiry), for marking an open
    paper position to market. Returns None if that instrument isn't present
    in this run's chain - e.g. its expiry has already lapsed."""
    try:
        rows = filter_rows(raw, expiry)
    except (KeyError, IndexError):
        return None
    row = next((r for r in rows if r["strikePrice"] == strike), None)
    if not row:
        return None
    leg = row.get(option_type)
    if not leg:
        return None
    return leg.get("lastPrice")


def _atm_strike(spot: float) -> float:
    step = config.STRIKE_STEP
    return round(spot / step) * step


def extract_analysis_rows(
    raw: dict,
) -> tuple[float, Optional[float], str, float, list[dict], list[dict]]:
    """The single source of truth for which strikes are analyzed.

    Returns (spot, india_vix, expiry, atm_strike, rows, all_rows) where
    `rows` is the nearest-expiry, ATM-windowed strike list - the same set
    used for the per-strike metrics, the data-integrity comparison, and the
    persisted snapshot, so all three are always looking at the same "sheet".
    `all_rows` is the full (un-windowed) nearest-expiry chain, used for
    chain-wide totals like Premium Money. `india_vix` is None when the data
    source doesn't provide implied volatility at all.
    """
    verify_raw_chain(raw)

    records = raw["records"]
    spot = float(records["underlyingValue"])
    india_vix = raw.get("india_vix")
    expiry = nearest_expiry(raw)
    all_rows = filter_rows(raw, expiry)

    atm = _atm_strike(spot)
    window = config.STRIKES_AROUND_ATM * config.STRIKE_STEP
    rows = [r for r in all_rows if abs(r["strikePrice"] - atm) <= window]

    return spot, india_vix, expiry, atm, rows, all_rows


def _parse_expiry_date(date_str: str) -> dt.date:
    return dt.datetime.strptime(date_str, "%d-%b-%Y").date()


def classify_expiries(raw: dict) -> tuple[str, str]:
    """Nearest weekly expiry and nearest monthly expiry (the last available
    expiry within a calendar month) from the broker's expiry list."""
    expiries = raw["records"]["expiryDates"]
    weekly = expiries[0]
    weekly_date = _parse_expiry_date(weekly)

    by_month: dict = {}
    for e in expiries:
        d = _parse_expiry_date(e)
        key = (d.year, d.month)
        if key not in by_month or d > by_month[key][1]:
            by_month[key] = (e, d)

    monthly_candidates = sorted(by_month.values(), key=lambda x: x[1])
    monthly = next(
        (e for e, d in monthly_candidates if d >= weekly_date),
        monthly_candidates[-1][0],
    )
    return weekly, monthly


def compute_pcr(rows: list[dict]) -> tuple[float, float]:
    """Put-Call Ratio by open interest and by traded volume."""
    ce_oi = sum(r["CE"]["openInterest"] for r in rows if "CE" in r)
    pe_oi = sum(r["PE"]["openInterest"] for r in rows if "PE" in r)
    ce_vol = sum(r["CE"].get("totalTradedVolume", 0) for r in rows if "CE" in r)
    pe_vol = sum(r["PE"].get("totalTradedVolume", 0) for r in rows if "PE" in r)

    pcr_oi = round(pe_oi / ce_oi, 3) if ce_oi else 0.0
    pcr_volume = round(pe_vol / ce_vol, 3) if ce_vol else 0.0
    return pcr_oi, pcr_volume


def compute_max_pain(rows: list[dict]) -> float:
    """Strike at which option writers collectively lose the least."""
    strikes = sorted({r["strikePrice"] for r in rows})
    ce_oi = {r["strikePrice"]: r["CE"]["openInterest"] for r in rows if "CE" in r}
    pe_oi = {r["strikePrice"]: r["PE"]["openInterest"] for r in rows if "PE" in r}

    best_strike, best_loss = None, None
    for candidate in strikes:
        loss = 0.0
        for k in strikes:
            loss += ce_oi.get(k, 0) * max(0.0, candidate - k)
            loss += pe_oi.get(k, 0) * max(0.0, k - candidate)
        if best_loss is None or loss < best_loss:
            best_strike, best_loss = candidate, loss
    return best_strike


def compute_support_resistance(
    rows: list[dict], top_n: int = config.TOP_N_LEVELS
) -> tuple[list[dict], list[dict]]:
    """Resistance = highest CE OI strikes, Support = highest PE OI strikes."""
    ce_ranked = sorted(
        (r for r in rows if "CE" in r),
        key=lambda r: r["CE"]["openInterest"],
        reverse=True,
    )[:top_n]
    pe_ranked = sorted(
        (r for r in rows if "PE" in r),
        key=lambda r: r["PE"]["openInterest"],
        reverse=True,
    )[:top_n]

    resistance = [
        {"strike": r["strikePrice"], "oi": r["CE"]["openInterest"]} for r in ce_ranked
    ]
    support = [
        {"strike": r["strikePrice"], "oi": r["PE"]["openInterest"]} for r in pe_ranked
    ]
    return support, resistance


def _classify(oi_change: float, ltp_change: float) -> str:
    if oi_change > 0 and ltp_change > 0:
        return "Long Buildup"
    if oi_change > 0 and ltp_change < 0:
        return "Short Buildup"
    if oi_change < 0 and ltp_change < 0:
        return "Long Unwinding"
    if oi_change < 0 and ltp_change > 0:
        return "Short Covering"
    return "Neutral"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(pct * (len(s) - 1))))
    return s[idx]


def _tag_conviction(entries: list[dict], pct: float = config.CONVICTION_PERCENTILE) -> None:
    """Institutional Conviction = high volume + high |Interval Delta|.
    Intraday Churn/Scalping = high volume + low |Interval Delta|.

    Interval Delta (vs. the last saved snapshot) drives this, not today's
    cumulative change - it's undefined ("Insufficient History") on a strike
    with no prior snapshot to diff against, most commonly the very first run.
    """
    ranked = [e for e in entries if e["oi_interval_delta"] is not None]
    if not ranked:
        for e in entries:
            e["conviction"] = "Insufficient History"
        return

    vol_high = _percentile([e["volume"] for e in ranked], 1 - pct)
    deltas = [abs(e["oi_interval_delta"]) for e in ranked]
    delta_high = _percentile(deltas, 1 - pct)
    delta_low = _percentile(deltas, pct)

    for e in entries:
        if e["oi_interval_delta"] is None:
            e["conviction"] = "Insufficient History"
            continue
        vol = e["volume"]
        delta = abs(e["oi_interval_delta"])
        if vol >= vol_high and delta >= delta_high:
            e["conviction"] = "Institutional Conviction"
        elif vol >= vol_high and delta <= delta_low:
            e["conviction"] = "Intraday Churn/Scalping"
        else:
            e["conviction"] = "Neutral"


def compute_strike_metrics(
    rows: list[dict],
    prev_rows: Optional[list[dict]],
) -> list[dict]:
    """Full per-strike/per-side metrics: absolute OI, today's cumulative OI
    change (broker field, 0 if the data source doesn't provide it), Interval
    Delta (vs. last saved snapshot), Premium Money, buildup/unwinding signal,
    and the Conviction tag.
    """
    prev_index = {}
    if prev_rows:
        for r in prev_rows:
            for side in ("CE", "PE"):
                if side in r:
                    prev_index[(r["strikePrice"], side)] = r[side]

    entries = []
    for r in rows:
        strike = r["strikePrice"]
        for side in ("CE", "PE"):
            leg = r.get(side)
            if not leg:
                continue
            oi = leg["openInterest"]
            oi_change_today = leg.get("changeinOpenInterest", 0)
            ltp = leg.get("lastPrice", 0.0)
            volume = leg.get("totalTradedVolume", 0)

            prev_leg = prev_index.get((strike, side))
            if prev_leg is not None:
                oi_interval_delta = oi - prev_leg["openInterest"]
                ltp_change = ltp - prev_leg.get("lastPrice", 0.0)
            else:
                oi_interval_delta = None
                ltp_change = leg.get("change", 0.0)

            primary_delta = (
                oi_interval_delta if oi_interval_delta is not None else oi_change_today
            )
            premium_money_cr = round(
                (ltp * oi * config.NIFTY_LOT_SIZE) / 1_00_00_000, 2
            )

            entries.append(
                {
                    "strike": strike,
                    "option_type": side,
                    "absolute_oi": oi,
                    "oi_change_today": oi_change_today,
                    "oi_interval_delta": oi_interval_delta,
                    "ltp": ltp,
                    "ltp_change": round(ltp_change, 2),
                    "volume": volume,
                    "premium_money_cr": premium_money_cr,
                    "classification": _classify(primary_delta, ltp_change),
                }
            )

    _tag_conviction(entries)
    return entries


def compute_total_premium_money(rows: list[dict]) -> tuple[float, float]:
    """Total Call Money vs. Total Put Money (Cr) across the full chain -
    the "global bias" figure, so it's computed over the un-windowed sheet."""
    call_money = sum(
        (r["CE"]["lastPrice"] * r["CE"]["openInterest"] * config.NIFTY_LOT_SIZE)
        / 1_00_00_000
        for r in rows
        if "CE" in r
    )
    put_money = sum(
        (r["PE"]["lastPrice"] * r["PE"]["openInterest"] * config.NIFTY_LOT_SIZE)
        / 1_00_00_000
        for r in rows
        if "PE" in r
    )
    return round(call_money, 2), round(put_money, 2)


def money_bias(call_money_cr: float, put_money_cr: float) -> str:
    if call_money_cr <= 0 and put_money_cr <= 0:
        return "Balanced"
    ratio = call_money_cr / put_money_cr if put_money_cr else float("inf")
    if ratio > 1.05:
        return "Call-Heavy (Bullish Tilt)"
    if ratio < 0.95:
        return "Put-Heavy (Bearish Tilt)"
    return "Balanced"


def compute_atm_straddle(rows: list[dict], atm: float) -> Optional[float]:
    """None (not 0.0) when either ATM leg is missing - a partial data source
    (e.g. "most active contracts only") may simply not include both sides of
    the ATM strike, and 0.0 would misrepresent that as a real straddle value."""
    atm_row = next((r for r in rows if r["strikePrice"] == atm), None)
    if not atm_row:
        return None
    ce_leg, pe_leg = atm_row.get("CE"), atm_row.get("PE")
    if not ce_leg or not pe_leg:
        return None
    return round(ce_leg["lastPrice"] + pe_leg["lastPrice"], 2)


def compute_iv_skew(
    rows: list[dict], spot: float
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """ATM call IV, ATM put IV, and the put-over-call IV skew - all None when
    the data source doesn't provide implied volatility at all."""
    atm = _atm_strike(spot)
    atm_row = next((r for r in rows if r["strikePrice"] == atm), None)
    if not atm_row:
        return None, None, None

    ce_leg = atm_row.get("CE") or {}
    pe_leg = atm_row.get("PE") or {}
    if "impliedVolatility" not in ce_leg or "impliedVolatility" not in pe_leg:
        return None, None, None

    ce_iv = ce_leg["impliedVolatility"]
    pe_iv = pe_leg["impliedVolatility"]
    skew = round(pe_iv - ce_iv, 2)
    return ce_iv, pe_iv, skew


def build_metrics(
    raw: dict,
    prev_rows: Optional[list[dict]] = None,
    symbol: str = config.NSE_SYMBOL,
) -> dict:
    """Assemble the full metrics payload used by Gemini, the ledger and the PDF.

    `prev_rows` is the previous run's analyzed row set (already the same
    nearest-expiry/ATM-windowed shape as `extract_analysis_rows` returns),
    typically loaded from the last saved snapshot by snapshot_store.py.
    """
    spot, india_vix, expiry, atm, rows, all_rows = extract_analysis_rows(raw)
    weekly_expiry, monthly_expiry = classify_expiries(raw)

    pcr_oi, pcr_volume = compute_pcr(rows)
    support, resistance = compute_support_resistance(rows)
    atm_iv_ce, atm_iv_pe, iv_skew = compute_iv_skew(rows, spot)
    atm_straddle = compute_atm_straddle(rows, atm)

    max_pain_weekly = compute_max_pain(filter_rows(raw, weekly_expiry))
    max_pain_monthly = compute_max_pain(filter_rows(raw, monthly_expiry))

    total_call_money_cr, total_put_money_cr = compute_total_premium_money(all_rows)

    strike_metrics = compute_strike_metrics(rows, prev_rows)
    oi_buildup = sorted(
        strike_metrics,
        key=lambda e: abs(
            e["oi_interval_delta"]
            if e["oi_interval_delta"] is not None
            else e["oi_change_today"]
        ),
        reverse=True,
    )[: config.TOP_N_MOVERS]

    total_ce_oi = sum(r["CE"]["openInterest"] for r in rows if "CE" in r)
    total_pe_oi = sum(r["PE"]["openInterest"] for r in rows if "PE" in r)

    return {
        "run_time": dt.datetime.now(config.IST).isoformat(timespec="seconds"),
        "nse_timestamp": raw["records"].get("timestamp"),
        "symbol": symbol,
        "spot": spot,
        "spot_source": raw.get("spot_source", "underlying_value_column"),
        "india_vix": india_vix,
        "expiry": expiry,
        "weekly_expiry": weekly_expiry,
        "monthly_expiry": monthly_expiry,
        "atm_strike": atm,
        "pcr_oi": pcr_oi,
        "pcr_volume": pcr_volume,
        "max_pain_weekly": max_pain_weekly,
        "max_pain_monthly": max_pain_monthly,
        "atm_straddle": atm_straddle,
        "support": support,
        "resistance": resistance,
        "atm_iv_ce": atm_iv_ce,
        "atm_iv_pe": atm_iv_pe,
        "iv_skew": iv_skew,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "total_call_money_cr": total_call_money_cr,
        "total_put_money_cr": total_put_money_cr,
        "money_bias": money_bias(total_call_money_cr, total_put_money_cr),
        "strike_metrics": strike_metrics,
        "oi_buildup": oi_buildup,
        "has_previous_snapshot": prev_rows is not None,
    }
