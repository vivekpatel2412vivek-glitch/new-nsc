"""Local web dashboard - the sole entrypoint for this tool.

Upload an NSE options CSV export (either "Most Active Contracts by Volume",
or the full Option Chain export) -> click Analyze -> results render on
screen and a matching PDF is generated for download. One upload = one
analysis = one PDF. No scheduler, no polling, no background loop -
/api/analyze runs the entire pipeline synchronously per request. Spot and
expiry are read directly from the CSV when the format provides them, or
derived (spot via put-call parity, expiry from the filename) when it
doesn't - see csv_parser.py. India VIX and Gamma are never available from
either format; ATM IV/IV Skew are only available from the full Option Chain
export. Anything unavailable is surfaced as N/A rather than a manual input
or a misleading number.

PAPER TRADING SIMULATION ONLY: this only ever reads the uploaded sheet and
writes to the local ledger.csv - no real order is ever placed, here or
anywhere else in this project.

No login is required - every route is open to anyone with the URL.

Run with `python dashboard.py` - opens at http://localhost:8000.
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import analysis
import config
import csv_parser
import gemini_client
import integrity
import ledger
import pdf_builder
import snapshot_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")

app = FastAPI(title="NIFTY Options Dashboard")

_DASHBOARD_HTML_PATH = config.STATIC_DIR / "dashboard.html"
_SAFE_REPORT_FILENAME = re.compile(r"^report_\d{8}_\d{6}\.pdf$")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")


def _mark_to_market_and_check_stops(
    raw: dict,
) -> tuple[Optional[dict], Optional[str], Optional[float], Optional[dict]]:
    """Mark the open paper position to market and auto-close it if its
    stop-loss or target has been breached by this upload's prices.

    Returns (open_position_after, auto_exit_note, current_price, closed_row).
    """
    open_pos = ledger.get_open_position()
    if not open_pos:
        return None, None, None, None

    strike, option_type, expiry = ledger.parse_instrument(open_pos["instrument"])
    current_price = analysis.lookup_current_price(raw, strike, option_type, expiry)
    if current_price is None:
        return open_pos, None, None, None

    stop_loss = float(open_pos["stop_loss"])
    target = float(open_pos["target"])

    if current_price <= stop_loss:
        closed = ledger.close_position(current_price)
        note = f"Stop-loss auto-triggered on {open_pos['instrument']} at {current_price:.2f}"
        return None, note, current_price, closed

    if current_price >= target:
        closed = ledger.close_position(current_price)
        note = f"Target auto-triggered on {open_pos['instrument']} at {current_price:.2f}"
        return None, note, current_price, closed

    updated = ledger.mark_to_market(current_price)
    return updated, None, current_price, None


def _apply_ai_decision(
    ai_analysis: dict,
    symbol: str,
    open_pos: Optional[dict],
    current_price: Optional[float],
    timestamp: str,
) -> Optional[dict]:
    """Apply Gemini's entry/exit/hold decision to the ledger. Returns the
    row opened or closed as a result, or None (hold / a no-op)."""
    action = ai_analysis.get("action", {}) or {}
    action_type = action.get("type")

    if action_type == "entry":
        if open_pos:
            return None
        qty = action.get("qty") or 0
        price = action.get("price") or 0
        strike = action.get("strike") or 0
        option_type = action.get("option_type") or ""
        expiry = action.get("expiry") or ""
        if not (qty and price and strike and option_type and expiry):
            return None
        instrument = ledger.build_instrument(symbol, float(strike), option_type, expiry)
        return ledger.open_position(
            timestamp=timestamp,
            instrument=instrument,
            qty=qty,
            entry_price=price,
            stop_loss=action.get("stop_loss", 0),
            target=action.get("target", 0),
        )

    if action_type == "exit":
        if not open_pos:
            return None
        exit_price = current_price if current_price is not None else (action.get("price") or 0)
        return ledger.close_position(exit_price)

    return None


def _pdf_url(pdf_path: Optional[Path]) -> Optional[str]:
    return f"/download/pdf/{pdf_path.name}" if pdf_path else None


def _build_error_result(reason: str, timestamp: str, run_time: datetime) -> dict:
    metrics = {
        "symbol": config.NSE_SYMBOL,
        "run_time": timestamp,
        "data_integrity_status": "Error",
        "verification_reason": reason,
    }
    output_path = config.REPORTS_DIR / f"report_{run_time.strftime('%Y%m%d_%H%M%S')}.pdf"
    pdf_builder.build_report(metrics, {}, [], output_path)
    return {
        "status": "Error",
        "reason": reason,
        "timestamp": timestamp,
        "pdf_url": _pdf_url(output_path),
    }


def _build_success_result(metrics: dict, ai_analysis: dict, pdf_path: Path) -> dict:
    running_capital = metrics["running_capital"]
    cumulative_pnl = running_capital - config.PORTFOLIO_CAPITAL
    progress_pct = (
        cumulative_pnl / config.PROFIT_TARGET * 100 if config.PROFIT_TARGET else 0
    )
    return {
        "status": "Verified",
        "reason": None,
        "timestamp": metrics["run_time"],
        "pdf_url": _pdf_url(pdf_path),
        "spot": metrics["spot"],
        "weekly_expiry": metrics["weekly_expiry"],
        "monthly_expiry": metrics["monthly_expiry"],
        "atm_strike": metrics["atm_strike"],
        "atm_straddle": metrics["atm_straddle"],
        "atm_iv_ce": metrics.get("atm_iv_ce"),
        "atm_iv_pe": metrics.get("atm_iv_pe"),
        "iv_skew": metrics.get("iv_skew"),
        "sentiment": ai_analysis.get("institutional_sentiment"),
        "global_money_bias_note": ai_analysis.get("global_money_bias_note"),
        "money_bias": metrics["money_bias"],
        "total_call_money_cr": metrics["total_call_money_cr"],
        "total_put_money_cr": metrics["total_put_money_cr"],
        "max_pain_weekly": metrics["max_pain_weekly"],
        "max_pain_monthly": metrics["max_pain_monthly"],
        "strike_metrics": metrics["strike_metrics"],
        "action": ai_analysis.get("action"),
        "evolution_suggestion": ai_analysis.get("evolution_suggestion"),
        "ledger": {
            "starting_capital": config.PORTFOLIO_CAPITAL,
            "profit_target": config.PROFIT_TARGET,
            "running_capital": running_capital,
            "cumulative_pnl": cumulative_pnl,
            "progress_pct": progress_pct,
            "open_position": metrics.get("open_position"),
            "open_position_ltp": metrics.get("open_position_ltp"),
            "realized_pnl_this_upload": (
                metrics["this_cycle_event"]["realized_pnl"]
                if metrics.get("this_cycle_event")
                and metrics["this_cycle_event"].get("action") == "exit"
                else None
            ),
            "recent_history": ledger.read_recent(),
        },
    }


@app.post("/api/analyze")
async def analyze(csv_file: UploadFile = File(...)) -> JSONResponse:
    run_time = datetime.now(config.IST)
    timestamp = run_time.isoformat(timespec="seconds")
    content = await csv_file.read()

    try:
        raw = csv_parser.parse_option_chain_csv(
            content, timestamp, symbol=config.NSE_SYMBOL, filename=csv_file.filename
        )
    except csv_parser.CSVParseError as exc:
        return JSONResponse(_build_error_result(str(exc), timestamp, run_time))

    try:
        spot, india_vix, sheet_expiry, _atm, current_rows, _all_rows = analysis.extract_analysis_rows(raw)
    except analysis.DataQualityError as exc:
        return JSONResponse(_build_error_result(str(exc), timestamp, run_time))

    previous_rows = snapshot_store.load_latest_snapshot()
    integrity_result = integrity.check(current_rows, previous_rows)
    if not integrity_result.ok:
        reason = "; ".join(integrity_result.reasons)
        return JSONResponse(_build_error_result(reason, timestamp, run_time))

    snapshot_store.save_snapshot(
        symbol=config.NSE_SYMBOL,
        nse_timestamp=timestamp,
        run_time=run_time,
        spot=spot,
        india_vix=india_vix,
        expiry=sheet_expiry,
        rows=current_rows,
    )

    metrics = analysis.build_metrics(raw, previous_rows)
    metrics["data_integrity_status"] = "Verified"

    open_pos, auto_exit_note, current_price, auto_closed_row = _mark_to_market_and_check_stops(raw)
    ai_analysis = gemini_client.get_ai_analysis(
        metrics, open_position=open_pos, auto_exit_note=auto_exit_note
    )
    ai_event_row = _apply_ai_decision(
        ai_analysis, metrics["symbol"], open_pos, current_price, metrics["run_time"]
    )

    final_open_pos = ledger.get_open_position()
    final_ltp = None
    if final_open_pos:
        fstrike, ftype, fexpiry = ledger.parse_instrument(final_open_pos["instrument"])
        final_ltp = analysis.lookup_current_price(raw, fstrike, ftype, fexpiry)

    metrics["running_capital"] = ledger.current_running_capital()
    metrics["open_position"] = final_open_pos
    metrics["open_position_ltp"] = final_ltp
    metrics["this_cycle_event"] = auto_closed_row or ai_event_row

    output_path = config.REPORTS_DIR / f"report_{run_time.strftime('%Y%m%d_%H%M%S')}.pdf"
    history = ledger.read_recent()
    pdf_builder.build_report(metrics, ai_analysis, history, output_path)

    return JSONResponse(_build_success_result(metrics, ai_analysis, output_path))


@app.post("/api/evolution/decision")
async def evolution_decision(request: Request) -> JSONResponse:
    body = await request.json()
    choice = body.get("choice")
    if choice not in ("approve", "reject"):
        return JSONResponse({"error": "choice must be 'approve' or 'reject'"}, status_code=400)

    suggestion = body.get("suggestion", "")
    ts = datetime.now(config.IST).isoformat(timespec="seconds")
    line = f"{ts} | {choice.upper()} | {suggestion}"
    with open(config.EVOLUTION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return JSONResponse({"ok": True, "logged": line})


@app.get("/download/pdf/{filename}")
def download_pdf(filename: str):
    if not _SAFE_REPORT_FILENAME.match(filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = config.REPORTS_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "report not found"}, status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=filename)


if __name__ == "__main__":
    uvicorn.run(app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT)
