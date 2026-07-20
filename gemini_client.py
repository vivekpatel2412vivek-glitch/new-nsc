"""Sends computed option-chain metrics to Gemini and gets back a structured
JSON trading read from a "Lead Institutional Derivative Strategist" persona.

Never blocks the pipeline: if the Gemini call fails or returns unparsable
JSON for any reason, a fallback analysis dict is returned so the PDF and
dashboard can still render - the malformed raw response text is logged to
logs/gemini_raw_errors.log so the failure is debuggable.

All the NUMBERS (sheet verification, Premium Money, Max Pain, ATM Straddle,
the full per-strike table) are computed deterministically by analysis.py and
never asked of Gemini - only the interpretive fields (sentiment, a trading
decision, an evolution suggestion) come from the model. This avoids an LLM
re-deriving or transcribing financial figures that code already computes
exactly.

Implied volatility (ATM IV / IV Skew) is only sometimes available, depending
on which NSE export was uploaded - the Most Active Contracts CSV never has
it, the full Option Chain export does. Gemini is told explicitly when the
atm_iv_ce/atm_iv_pe/iv_skew fields are null so it never guesses or
hallucinates volatility context that wasn't actually provided. India VIX and
Gamma are never available from any supported data source.

Everything Gemini returns here is a SIMULATED, PAPER-TRADING recommendation.
No real orders are ever placed from this output. ledger.py tracks the open
position across uploads and it's passed in here each call, so Gemini is told
explicitly whether one is open rather than reasoning statelessly.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import types

import config

logger = logging.getLogger(__name__)


def _build_system_instruction(monthly_expiry: str) -> str:
    return (
        "You are the Lead Institutional Derivative Strategist. Your mandate "
        f"is to manage a paper trading portfolio of Rs {config.PORTFOLIO_CAPITAL:,} "
        f"with a profit target of Rs {config.PROFIT_TARGET:,} by the monthly "
        f"expiry on {monthly_expiry}. You operate with the discipline of a "
        "hedge fund, using margin to capture institutional microstructure "
        "shifts while prioritizing capital preservation.\n\n"
        "This is a PAPER-TRADING SIMULATION ONLY - no real orders are ever "
        "placed from your output, and nothing you return is financial advice.\n\n"
        "Data Analysis & Integrity Protocol: the system has already compared "
        "this sheet against the previous one, verified it (no corrupted, "
        "mismatched, or truncated data), and computed - for every strike - "
        "Absolute OI, Interval Delta since the last sheet, Premium Money in "
        "Cr, and a Conviction tag (high volume + low OI change = intraday "
        "churn/scalping; high volume + high OI change = institutional "
        "conviction). It has also computed Total Call vs Total Put Money, "
        "weekly and monthly Max Pain, and ATM Straddle. You are given all of "
        "this as verified input - do not recompute or second-guess the "
        "arithmetic, just interpret it.\n\n"
        "atm_iv_ce/atm_iv_pe/iv_skew below are null whenever this upload's "
        "data source doesn't provide implied volatility (e.g. the Most Active "
        "Contracts export) - do not mention or guess at IV in that case, base "
        "your sentiment purely on OI positioning, Volume, and Premium Money "
        "signals. When they ARE present (the full Option Chain export), you "
        "may factor them into your read. India VIX and Gamma are never "
        "available from any supported data source - never mention or guess "
        "at them either way. Note also that a Most Active Contracts upload "
        "lists only the most-active-by-volume contracts, not the full option "
        "chain, so Max Pain/PCR computed from it are directional estimates "
        "rather than exact full-chain figures - factor that uncertainty into "
        "your confidence, not into fabricated precision.\n\n"
        "If the current (weekly) expiry looks too risky to act on, or a "
        "calendar spread across expiries would be more accurate, you may say "
        "so in your reasoning - this is informational only: no re-fetch of a "
        "different expiry happens automatically as a result, so make your "
        "best call on the data you were actually given.\n\n"
        "Trading & Margin Mandate: Nifty lot size is 65 units. You decide "
        "where, when, and how much margin to deploy - you are not forced to "
        "trade every session. Every trade needs a clear logic and exit "
        "strategy (stop-loss/target). Update the ledger directly through your "
        "'action' below rather than asking for entry/exit permission each "
        "time. Maintain a capital buffer for adjustments rather than sizing "
        "to the limit.\n\n"
        "The system tracks your paper portfolio's open position across "
        "uploads and tells you below whether one is currently open (with "
        "instrument, qty, entry price, stop-loss, target, and current "
        "unrealized P&L) - if one is open, your only sensible actions are "
        "'exit' or 'hold'; if none is open, 'entry' or 'hold'. Stop-loss and "
        "target are enforced automatically by the system the instant they're "
        "breached by the live price, before you're even consulted - if "
        "'auto_exit_this_run' is non-null, that breach already happened this "
        "upload and closed the position for you; do not issue your own "
        "'exit' for a stop-loss/target you see was already auto-triggered.\n\n"
        "Respond with JSON only - no markdown fences, no preamble or "
        "explanation text, the entire response must be a single valid JSON "
        "object matching this shape exactly:\n"
        "{\n"
        '  "institutional_sentiment": {\n'
        '    "label": "Bullish" | "Bearish" | "Neutral",\n'
        '    "reasoning": "2-4 sentence reasoning based on OI/Volume/Premium Money only"\n'
        "  },\n"
        '  "global_money_bias_note": "short read on the call vs put Premium Money split",\n'
        '  "action": {\n'
        '    "type": "entry" | "exit" | "hold",\n'
        '    "instrument": "human-readable label, e.g. NIFTY 25000 CE 24-Jul-2026",\n'
        '    "strike": <strike price as a number, required if type is entry, else 0>,\n'
        '    "option_type": "CE" | "PE" | "",\n'
        '    "expiry": "one of the expiry dates you were given, or empty if not entry",\n'
        '    "qty": <lots, 0 if hold>,\n'
        '    "price": <entry/exit price, 0 if hold>,\n'
        '    "stop_loss": <stop-loss price, 0 if hold>,\n'
        '    "target": <target price, 0 if hold>,\n'
        '    "reasoning": "why this action, sized for Rs '
        f"{config.PORTFOLIO_CAPITAL:,} capital and Rs {config.PROFIT_TARGET:,} "
        'target with a capital buffer maintained"\n'
        "  },\n"
        '  "evolution_suggestion": "one concrete new metric or accuracy improvement, for approval"\n'
        "}"
    )


_REQUIRED_KEYS = (
    "institutional_sentiment",
    "global_money_bias_note",
    "action",
    "evolution_suggestion",
)


def _fallback(reason: str) -> dict:
    return {
        "institutional_sentiment": {
            "label": "Unavailable",
            "reasoning": f"AI analysis unavailable: {reason}",
        },
        "global_money_bias_note": "",
        "action": {
            "type": "hold",
            "instrument": "",
            "strike": 0,
            "option_type": "",
            "expiry": "",
            "qty": 0,
            "price": 0,
            "stop_loss": 0,
            "target": 0,
            "reasoning": f"AI analysis unavailable: {reason}",
        },
        "evolution_suggestion": "",
    }


def _extract_json_object(text: str) -> str:
    """Strip markdown fences and any stray prose the model added despite
    JSON-mode instructions (e.g. a leaked "let me fix that..." preamble) by
    taking the substring between the first '{' and the last '}'."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.lower().startswith("json"):
                text = text[4:]
    text = text.strip()

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text


def _short_error(exc: Exception) -> str:
    """A compact, human-readable error message - the SDK's exceptions
    stringify to a huge raw JSON blob otherwise, unreadable when it ends up
    dumped into a report."""
    if exc.args and isinstance(exc.args[0], dict):
        err = exc.args[0].get("error", {})
        message = err.get("message")
        status = err.get("status") or getattr(exc, "status", None)
        if message:
            first_line = message.split("\n")[0].split(". For more information")[0].strip()
            return f"{status}: {first_line}" if status else first_line
    text = str(exc)
    return text if len(text) <= 200 else text[:200] + "..."


def _log_raw_response(raw_text: str, error: str) -> None:
    ts = datetime.now(config.IST).isoformat(timespec="seconds")
    with open(config.GEMINI_RAW_ERRORS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"=== {ts} | {error} ===\n{raw_text}\n\n")


def _call_once(prompt_payload: dict, monthly_expiry: str) -> tuple[Optional[dict], Optional[str], bool]:
    """One Gemini attempt. Returns (parsed, error_reason, retryable).
    retryable is True only for a bad-JSON response - this specific model
    occasionally truncates its own output despite thinking being disabled;
    a fresh attempt usually comes back clean. Hard API errors (auth, quota)
    are not retried - a second call would just hit the same wall."""
    try:
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=json.dumps(prompt_payload, default=str),
            config=types.GenerateContentConfig(
                system_instruction=_build_system_instruction(monthly_expiry),
                response_mime_type="application/json",
                temperature=0.3,
                max_output_tokens=4096,
                # This model has an extended-thinking mode that consumes part
                # of max_output_tokens on hidden reasoning before the visible
                # JSON - budgeting it to 0 was observed to substantially
                # reduce (though not fully eliminate) the visible JSON
                # getting cut off mid-object.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any Gemini/network failure is non-fatal
        logger.error("Gemini analysis failed: %s", exc)
        return None, _short_error(exc), False

    raw_text = response.text or ""
    try:
        parsed = json.loads(_extract_json_object(raw_text))
    except json.JSONDecodeError as exc:
        logger.error("Gemini response was not valid JSON: %s", exc)
        _log_raw_response(raw_text, str(exc))
        return None, f"invalid JSON response: {exc}", True

    if not isinstance(parsed, dict) or not all(k in parsed for k in _REQUIRED_KEYS):
        logger.error("Gemini response missing expected keys: %s", parsed)
        _log_raw_response(raw_text, "missing expected keys")
        return None, "malformed response from model", True

    return parsed, None, False


def get_ai_analysis(
    metrics: dict,
    open_position: Optional[dict] = None,
    auto_exit_note: Optional[str] = None,
) -> dict:
    if not config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set; skipping AI analysis")
        return _fallback("GEMINI_API_KEY is not configured")

    prompt_payload = {
        "symbol": metrics["symbol"],
        "spot": metrics["spot"],
        "weekly_expiry": metrics["weekly_expiry"],
        "monthly_expiry": metrics["monthly_expiry"],
        "atm_strike": metrics["atm_strike"],
        "atm_straddle": metrics["atm_straddle"],
        "atm_iv_ce": metrics.get("atm_iv_ce"),
        "atm_iv_pe": metrics.get("atm_iv_pe"),
        "iv_skew": metrics.get("iv_skew"),
        "pcr_oi": metrics["pcr_oi"],
        "pcr_volume": metrics["pcr_volume"],
        "max_pain_weekly": metrics["max_pain_weekly"],
        "max_pain_monthly": metrics["max_pain_monthly"],
        "total_call_oi": metrics["total_ce_oi"],
        "total_put_oi": metrics["total_pe_oi"],
        "total_call_money_cr": metrics["total_call_money_cr"],
        "total_put_money_cr": metrics["total_put_money_cr"],
        "money_bias": metrics["money_bias"],
        "sheet_verification": metrics.get("data_integrity_status", "Verified"),
        "oi_buildup_top_movers": metrics["oi_buildup"],
        "open_position": open_position,
        "auto_exit_this_run": auto_exit_note,
    }

    parsed, error, retryable = _call_once(prompt_payload, metrics["monthly_expiry"])
    if parsed is None and retryable:
        logger.info("Retrying Gemini call once after a bad-JSON response...")
        parsed, error, _retryable = _call_once(prompt_payload, metrics["monthly_expiry"])

    if parsed is None:
        return _fallback(error or "unknown error")
    return parsed
