"""Reads/writes ledger.csv - the paper-trading position ledger.

PAPER SIMULATION ONLY: nothing here ever places a real order. One row per
position lifecycle (opened on "entry", updated in place on "hold" to mark it
to market, updated in place again on "exit" to close it) - at most one open
position at a time, so the open position (if any) is always the last row.
"""
import csv
from typing import Optional

import config

FIELDNAMES = [
    "timestamp",
    "instrument",
    "action",
    "qty",
    "entry_price",
    "exit_price",
    "stop_loss",
    "target",
    "realized_pnl",
    "unrealized_pnl",
    "running_capital",
]


def _read_all() -> list[dict]:
    if not config.LEDGER_PATH.exists():
        return []
    with open(config.LEDGER_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_all(rows: list[dict]) -> None:
    with open(config.LEDGER_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})


def build_instrument(symbol: str, strike: float, option_type: str, expiry: str) -> str:
    return f"{symbol} {strike:.0f} {option_type} {expiry}"


def parse_instrument(instrument: str) -> tuple[float, str, str]:
    """Inverse of build_instrument() - safe because every instrument string in
    this ledger was written by build_instrument(), never taken verbatim from
    Gemini's free-text label."""
    _symbol, strike, option_type, expiry = instrument.split(maxsplit=3)
    return float(strike), option_type, expiry


def get_open_position() -> Optional[dict]:
    """The currently open position, if any. An entry is never recorded while
    one is already open, so it's always the last row when unclosed."""
    rows = _read_all()
    if rows and rows[-1].get("exit_price", "") in ("", None):
        return rows[-1]
    return None


def current_running_capital() -> float:
    rows = _read_all()
    if not rows:
        return float(config.PORTFOLIO_CAPITAL)
    return float(rows[-1]["running_capital"])


def open_position(
    timestamp: str,
    instrument: str,
    qty: float,
    entry_price: float,
    stop_loss: float,
    target: float,
) -> dict:
    rows = _read_all()
    running_capital = (
        float(rows[-1]["running_capital"]) if rows else float(config.PORTFOLIO_CAPITAL)
    )
    row = {
        "timestamp": timestamp,
        "instrument": instrument,
        "action": "entry",
        "qty": qty,
        "entry_price": entry_price,
        "exit_price": "",
        "stop_loss": stop_loss,
        "target": target,
        "realized_pnl": "",
        "unrealized_pnl": 0,
        "running_capital": running_capital,
    }
    rows.append(row)
    _write_all(rows)
    return row


def mark_to_market(current_price: float) -> Optional[dict]:
    """Update the open position's unrealized_pnl in place. No-op if nothing
    is open."""
    rows = _read_all()
    if not rows or rows[-1].get("exit_price", "") not in ("", None):
        return None
    row = rows[-1]
    qty = float(row["qty"])
    entry_price = float(row["entry_price"])
    row["unrealized_pnl"] = round(
        (current_price - entry_price) * qty * config.NIFTY_LOT_SIZE, 2
    )
    _write_all(rows)
    return row


def close_position(exit_price: float) -> Optional[dict]:
    """Close the open position at `exit_price`, realize P&L, and roll
    running_capital forward. No-op if nothing is open."""
    rows = _read_all()
    if not rows or rows[-1].get("exit_price", "") not in ("", None):
        return None
    row = rows[-1]
    qty = float(row["qty"])
    entry_price = float(row["entry_price"])
    prev_capital = float(row["running_capital"])
    realized = round((exit_price - entry_price) * qty * config.NIFTY_LOT_SIZE, 2)

    row["action"] = "exit"
    row["exit_price"] = exit_price
    row["realized_pnl"] = realized
    row["unrealized_pnl"] = 0
    row["running_capital"] = round(prev_capital + realized, 2)
    _write_all(rows)
    return row


def read_recent(n: int = config.LEDGER_HISTORY_ROWS_IN_REPORT) -> list[dict]:
    return _read_all()[-n:]
