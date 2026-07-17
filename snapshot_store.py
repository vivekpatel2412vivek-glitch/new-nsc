"""Persists and reloads the analyzed option-chain "sheet" as timestamped CSVs
in data/snapshots/, for the data-integrity comparison and OI-buildup diffing.

Only the rows analysis.extract_analysis_rows() actually analyzes (nearest
expiry, ATM-windowed) are snapshotted - that's the same "sheet" the data
integrity protocol compares run-over-run.
"""
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

FIELDNAMES = [
    "symbol",
    "nse_timestamp",
    "run_time",
    "spot",
    "india_vix",
    "expiry",
    "strike",
    "ce_ltp",
    "ce_oi",
    "ce_oi_change",
    "ce_volume",
    "ce_iv",
    "pe_ltp",
    "pe_oi",
    "pe_oi_change",
    "pe_volume",
    "pe_iv",
]

_LEG_FIELDS = ("lastPrice", "openInterest", "changeinOpenInterest", "totalTradedVolume", "impliedVolatility")


def _leg_to_columns(leg: Optional[dict], prefix: str) -> dict:
    if leg is None:
        return {f"{prefix}_{k}": "" for k in ("ltp", "oi", "oi_change", "volume", "iv")}
    return {
        f"{prefix}_ltp": leg.get("lastPrice", ""),
        f"{prefix}_oi": leg.get("openInterest", ""),
        f"{prefix}_oi_change": leg.get("changeinOpenInterest", ""),
        f"{prefix}_volume": leg.get("totalTradedVolume", ""),
        f"{prefix}_iv": leg.get("impliedVolatility", ""),
    }


def _columns_to_leg(row: dict, prefix: str) -> Optional[dict]:
    if row.get(f"{prefix}_oi", "") == "":
        return None
    return {
        "lastPrice": float(row[f"{prefix}_ltp"]) if row[f"{prefix}_ltp"] != "" else None,
        "openInterest": int(float(row[f"{prefix}_oi"])) if row[f"{prefix}_oi"] != "" else None,
        "changeinOpenInterest": (
            int(float(row[f"{prefix}_oi_change"])) if row[f"{prefix}_oi_change"] != "" else None
        ),
        "totalTradedVolume": int(float(row[f"{prefix}_volume"])) if row[f"{prefix}_volume"] != "" else None,
        "impliedVolatility": float(row[f"{prefix}_iv"]) if row[f"{prefix}_iv"] != "" else None,
    }


def save_snapshot(
    symbol: str,
    nse_timestamp: str,
    run_time: datetime,
    spot: float,
    india_vix: float,
    expiry: str,
    rows: list[dict],
) -> Path:
    filename = f"{symbol}_{run_time.strftime('%Y%m%dT%H%M%S')}.csv"
    path = config.SNAPSHOT_DIR / filename

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in rows:
            row = {
                "symbol": symbol,
                "nse_timestamp": nse_timestamp,
                "run_time": run_time.isoformat(timespec="seconds"),
                "spot": spot,
                "india_vix": india_vix,
                "expiry": expiry,
                "strike": r["strikePrice"],
            }
            row.update(_leg_to_columns(r.get("CE"), "ce"))
            row.update(_leg_to_columns(r.get("PE"), "pe"))
            writer.writerow(row)

    return path


def _latest_snapshot_path():
    files = sorted(config.SNAPSHOT_DIR.glob("*.csv"))
    return files[-1] if files else None


def load_latest_snapshot() -> Optional[list[dict]]:
    """Return the previous run's analyzed rows in the nested CE/PE shape used
    throughout analysis.py, or None if no snapshot has been saved yet."""
    path = _latest_snapshot_path()
    if path is None:
        return None

    with open(path, newline="", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))

    rows = []
    for row in csv_rows:
        entry = {"strikePrice": int(float(row["strike"])), "expiryDate": row["expiry"]}
        ce = _columns_to_leg(row, "ce")
        pe = _columns_to_leg(row, "pe")
        if ce is not None:
            entry["CE"] = ce
        if pe is not None:
            entry["PE"] = pe
        rows.append(entry)

    return rows
