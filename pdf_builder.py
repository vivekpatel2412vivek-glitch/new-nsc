"""Builds the final institutional-style PDF report with reportlab platypus.

Section order is fixed: 1. Sheet Verification, 2. Institutional Sentiment,
3. Market Ranges, 4. Strike Data Table, 5. Action & Ledger, 6. Evolution
Suggestion. A PDF is only ever built after the data-integrity check passes
(main.py stops before this module is called otherwise), so Section 1 always
reads "Verified" in practice.
"""
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import config

_styles = getSampleStyleSheet()
_styles.add(
    ParagraphStyle(
        name="ReportTitle",
        parent=_styles["Title"],
        fontSize=20,
        spaceAfter=4,
    )
)
_styles.add(
    ParagraphStyle(
        name="Disclaimer",
        parent=_styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#8a1f1f"),
        borderColor=colors.HexColor("#8a1f1f"),
        borderWidth=0.5,
        borderPadding=6,
        backColor=colors.HexColor("#fdecea"),
    )
)
_styles.add(
    ParagraphStyle(
        name="SectionHeading",
        parent=_styles["Heading2"],
        spaceBefore=14,
        spaceAfter=6,
        textColor=colors.HexColor("#1a2b4c"),
    )
)
_styles.add(
    ParagraphStyle(
        name="ActionBadge",
        parent=_styles["Normal"],
        fontSize=8,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#7a4a00"),
        borderColor=colors.HexColor("#7a4a00"),
        borderWidth=0.5,
        borderPadding=6,
        backColor=colors.HexColor("#fff3e0"),
    )
)
_styles.add(
    ParagraphStyle(
        name="VerifiedStatus",
        parent=_styles["Normal"],
        fontSize=13,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#1e6b2f"),
    )
)
_styles.add(
    ParagraphStyle(
        name="ErrorStatus",
        parent=_styles["Normal"],
        fontSize=13,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#8a1f1f"),
    )
)

_TABLE_STYLE = TableStyle(
    [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b4c")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6fa")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
)

_WIDE_TABLE_STYLE = TableStyle(
    [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b4c")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6fa")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
)

# Same as _WIDE_TABLE_STYLE plus a bold "TOTAL" footer band for the strike table.
_STRIKE_TABLE_STYLE = TableStyle(
    _WIDE_TABLE_STYLE.getCommands()
    + [
        ("FONTNAME", (0, -2), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -2), (-1, -1), colors.HexColor("#dfe6f0")),
    ]
)


def _fmt_delta(v) -> str:
    return f"{v:+,}" if v is not None else "n/a"


def _fmt_money(v) -> str:
    return f"Rs {v:,.2f}" if v is not None else "n/a"


def _fmt_num(v, digits: int = 2) -> str:
    return f"{v:,.{digits}f}" if v is not None else "N/A"


def _strike_data_table(m: dict) -> Table:
    rows = [["Strike", "Type", "Absolute OI", "ΔOI Interval", "Premium (Cr)", "Conviction"]]
    entries = sorted(m["strike_metrics"], key=lambda e: (e["strike"], e["option_type"]))
    for e in entries:
        rows.append(
            [
                f"{e['strike']:.0f}",
                e["option_type"],
                f"{e['absolute_oi']:,}",
                _fmt_delta(e["oi_interval_delta"]),
                f"{e['premium_money_cr']:,.2f}",
                e["conviction"],
            ]
        )

    for side in ("CE", "PE"):
        side_entries = [e for e in entries if e["option_type"] == side]
        rows.append(
            [
                f"TOTAL ({'Calls' if side == 'CE' else 'Puts'})",
                "",
                f"{sum(e['absolute_oi'] for e in side_entries):,}",
                "n/a" if any(e['oi_interval_delta'] is None for e in side_entries)
                else _fmt_delta(sum(e['oi_interval_delta'] for e in side_entries)),
                f"{sum(e['premium_money_cr'] for e in side_entries):,.2f}",
                "",
            ]
        )

    t = Table(
        rows,
        colWidths=[20 * mm, 15 * mm, 28 * mm, 28 * mm, 28 * mm, 46 * mm],
        repeatRows=1,
    )
    t.setStyle(_STRIKE_TABLE_STYLE)
    return t


def _position_ledger_table(history: list[dict]) -> Table:
    rows = [
        [
            "Timestamp", "Instrument", "Action", "Qty", "Entry", "Exit",
            "SL", "Target", "Realized", "Unrealized", "Capital",
        ]
    ]
    for h in history:
        rows.append(
            [
                h.get("timestamp", ""),
                h.get("instrument", ""),
                h.get("action", ""),
                h.get("qty", ""),
                h.get("entry_price", ""),
                h.get("exit_price", "") or "-",
                h.get("stop_loss", ""),
                h.get("target", ""),
                h.get("realized_pnl", "") or "-",
                h.get("unrealized_pnl", ""),
                h.get("running_capital", ""),
            ]
        )
    t = Table(
        rows,
        colWidths=[
            24 * mm, 34 * mm, 10 * mm, 8 * mm, 13 * mm, 13 * mm,
            11 * mm, 11 * mm, 15 * mm, 15 * mm, 18 * mm,
        ],
    )
    t.setStyle(_WIDE_TABLE_STYLE)
    return t


def _action_table(action: dict) -> Table:
    rows = [
        ["Type", "Instrument", "Qty", "Price", "Stop Loss", "Target"],
        [
            str(action.get("type", "")).upper(),
            action.get("instrument", "") or "-",
            str(action.get("qty", 0)),
            str(action.get("price", 0)),
            str(action.get("stop_loss", 0)),
            str(action.get("target", 0)),
        ],
    ]
    t = Table(rows, colWidths=[18 * mm, 60 * mm, 16 * mm, 22 * mm, 22 * mm, 22 * mm])
    t.setStyle(_TABLE_STYLE)
    return t


def build_report(
    metrics: dict,
    ai_analysis: dict,
    ledger_history: list[dict],
    output_path,
) -> None:
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=16 * mm,
        bottomMargin=14 * mm,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        title=f"{metrics.get('symbol', config.NSE_SYMBOL)} Options Analysis Report",
    )

    status = metrics.get("data_integrity_status", "n/a")

    story = []
    story.append(
        Paragraph(
            f"{metrics.get('symbol', config.NSE_SYMBOL)} Options Analysis Report",
            _styles["ReportTitle"],
        )
    )
    if status == "Verified":
        story.append(
            Paragraph(
                f"Generated {metrics['run_time']} | Spot: {metrics['spot']:.2f} "
                f"| Weekly Expiry: {metrics['weekly_expiry']} | Monthly Expiry: "
                f"{metrics['monthly_expiry']}",
                _styles["Normal"],
            )
        )
    else:
        story.append(Paragraph(f"Generated {metrics.get('run_time', 'n/a')}", _styles["Normal"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(config.DISCLAIMER, _styles["Disclaimer"]))

    # 1. Sheet Verification
    story.append(Paragraph("1. Sheet Verification", _styles["SectionHeading"]))
    status_style = _styles["VerifiedStatus"] if status == "Verified" else _styles["ErrorStatus"]
    story.append(Paragraph(status.upper(), status_style))
    if status != "Verified":
        story.append(
            Paragraph(
                metrics.get("verification_reason", "Sheet failed the data-integrity protocol."),
                _styles["Normal"],
            )
        )
        story.append(
            Paragraph(
                "Analysis was not performed on this upload - the sections below "
                "are not applicable. Fix the issue above and re-upload.",
                _styles["Normal"],
            )
        )
        story.append(Spacer(1, 10))
        story.append(Paragraph(config.DISCLAIMER, _styles["Disclaimer"]))
        doc.build(story)
        return
    story.append(
        Paragraph(
            "No null fields, no truncation, and no structural mismatch vs. "
            "the previous uploaded sheet.",
            _styles["Normal"],
        )
    )

    # 2. Institutional Sentiment
    story.append(Paragraph("2. Institutional Sentiment", _styles["SectionHeading"]))
    sentiment = ai_analysis.get("institutional_sentiment", {}) or {}
    story.append(
        Paragraph(f"<b>Sentiment:</b> {sentiment.get('label', 'n/a')}", _styles["Normal"])
    )
    story.append(Spacer(1, 3))
    story.append(Paragraph(sentiment.get("reasoning", ""), _styles["Normal"]))
    story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            f"<b>PCR:</b> OI {metrics['pcr_oi']:.3f} &nbsp;&nbsp; Volume "
            f"{metrics['pcr_volume']:.3f}",
            _styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            "<b>IV Skew / ATM IV / India VIX:</b> N/A - not available from this "
            "data source (Most Active Contracts export has no implied volatility)",
            _styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"<b>Global Money Value:</b> Call Rs {metrics['total_call_money_cr']:,.2f} Cr "
            f"&nbsp;&nbsp; Put Rs {metrics['total_put_money_cr']:,.2f} Cr &nbsp;&nbsp; "
            f"({metrics['money_bias']}) - {ai_analysis.get('global_money_bias_note', '')}",
            _styles["Normal"],
        )
    )

    # 3. Market Ranges
    story.append(Paragraph("3. Market Ranges", _styles["SectionHeading"]))
    story.append(
        Paragraph(
            "Based on the most-active-by-volume contracts in this upload, not "
            "the full option chain - directional estimates, not exact full-chain figures.",
            _styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"<b>Max Pain:</b> Weekly {metrics['max_pain_weekly']:.0f} &nbsp;&nbsp; "
            f"Monthly {metrics['max_pain_monthly']:.0f}",
            _styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            f"<b>ATM Straddle:</b> {_fmt_num(metrics['atm_straddle'])} at strike "
            f"{metrics['atm_strike']:.0f} (N/A if either ATM leg wasn't among "
            "the most-active contracts this upload)",
            _styles["Normal"],
        )
    )

    # 4. Strike Data Table
    story.append(Paragraph("4. Strike Data Table", _styles["SectionHeading"]))
    if not metrics.get("has_previous_snapshot"):
        story.append(
            Paragraph(
                "No prior snapshot found - Interval Delta reads \"n/a\" and "
                "Conviction tags read \"Insufficient History\" until a "
                "run-over-run comparison is available.",
                _styles["Normal"],
            )
        )
    story.append(_strike_data_table(metrics))

    # 5. Action & Ledger
    story.append(Paragraph("5. Action &amp; Ledger", _styles["SectionHeading"]))
    story.append(
        Paragraph(
            "SIMULATED PAPER TRADE IDEA - NOT A REAL ORDER. Stop-loss/target "
            "are enforced automatically by the system, not by Gemini.",
            _styles["ActionBadge"],
        )
    )
    story.append(Spacer(1, 4))
    action = ai_analysis.get("action", {}) or {}
    story.append(_action_table(action))
    story.append(Spacer(1, 4))
    story.append(Paragraph(action.get("reasoning", ""), _styles["Normal"]))
    story.append(Spacer(1, 8))

    open_pos = metrics.get("open_position")
    ltp = metrics.get("open_position_ltp")
    if open_pos and ltp is not None:
        net_spread = ltp - float(open_pos["entry_price"])
        story.append(
            Paragraph(
                f"<b>LTP:</b> {ltp:.2f} &nbsp;&nbsp; <b>Net Spread vs Entry:</b> "
                f"{net_spread:+.2f}",
                _styles["Normal"],
            )
        )
    else:
        story.append(Paragraph("<b>LTP / Net Spread:</b> n/a - no open position", _styles["Normal"]))

    cycle_event = metrics.get("this_cycle_event")
    if cycle_event and cycle_event.get("action") == "exit":
        story.append(
            Paragraph(
                f"<b>Realized P&amp;L (this cycle):</b> "
                f"{_fmt_money(float(cycle_event['realized_pnl']))}",
                _styles["Normal"],
            )
        )
    else:
        story.append(
            Paragraph("<b>Realized P&amp;L (this cycle):</b> n/a - no trade closed this cycle", _styles["Normal"])
        )

    if open_pos:
        story.append(
            Paragraph(
                f"<b>Unrealized P&amp;L (current):</b> "
                f"{_fmt_money(float(open_pos['unrealized_pnl']))}",
                _styles["Normal"],
            )
        )
    else:
        story.append(
            Paragraph("<b>Unrealized P&amp;L (current):</b> n/a - no open position", _styles["Normal"])
        )

    running_capital = metrics.get("running_capital", config.PORTFOLIO_CAPITAL)
    cumulative_pnl = running_capital - config.PORTFOLIO_CAPITAL
    progress_pct = (cumulative_pnl / config.PROFIT_TARGET * 100) if config.PROFIT_TARGET else 0
    story.append(
        Paragraph(
            f"<b>Running Capital:</b> Rs {running_capital:,.2f} vs starting Rs "
            f"{config.PORTFOLIO_CAPITAL:,} &nbsp;&nbsp; <b>Cumulative P&amp;L:</b> "
            f"{_fmt_money(cumulative_pnl)} &nbsp;&nbsp; <b>Progress to Rs "
            f"{config.PROFIT_TARGET:,} Target:</b> {progress_pct:.1f}%",
            _styles["Normal"],
        )
    )

    if ledger_history:
        story.append(Spacer(1, 8))
        story.append(Paragraph("<b>Recent Ledger</b>", _styles["Normal"]))
        story.append(_position_ledger_table(ledger_history))

    # 6. Evolution Suggestion
    story.append(Paragraph("6. Evolution Suggestion", _styles["SectionHeading"]))
    story.append(Paragraph(ai_analysis.get("evolution_suggestion", ""), _styles["Normal"]))

    story.append(Spacer(1, 10))
    story.append(Paragraph(config.DISCLAIMER, _styles["Disclaimer"]))

    doc.build(story)
