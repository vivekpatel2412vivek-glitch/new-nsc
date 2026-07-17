"""Data integrity protocol.

Before any analysis runs, the freshly fetched strike sheet is compared
against the last saved snapshot:
  - null/missing fields anywhere in the current sheet -> fail
  - the current strike list truncated vs. the previous sheet -> fail
  - a CE/PE leg present in the previous sheet but missing now -> fail
Anything else is marked "Verified" and analysis proceeds.

changeinOpenInterest and impliedVolatility are deliberately NOT in the
required-fields list - some data sources (e.g. the Most Active Contracts
CSV) never provide them at all, which is a normal, expected state, not a
sign of corrupted data.
"""
from dataclasses import dataclass, field
from typing import Optional

_REQUIRED_LEG_FIELDS = (
    "lastPrice",
    "openInterest",
    "totalTradedVolume",
)


@dataclass
class IntegrityResult:
    status: str  # "Verified" | "Failed"
    reasons: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "Verified"


def _leg_null_fields(leg: dict) -> list:
    return [f for f in _REQUIRED_LEG_FIELDS if leg.get(f) is None]


def check(current_rows: list, previous_rows: Optional[list]) -> IntegrityResult:
    reasons = []

    for row in current_rows:
        strike = row.get("strikePrice")
        if "CE" not in row and "PE" not in row:
            reasons.append(f"Strike {strike}: both CE and PE legs missing")
            continue
        for side in ("CE", "PE"):
            leg = row.get(side)
            if leg is None:
                continue
            missing = _leg_null_fields(leg)
            if missing:
                reasons.append(f"Strike {strike} {side}: null field(s) {missing}")

    if previous_rows is not None:
        if len(current_rows) < len(previous_rows):
            reasons.append(
                f"Strike list truncated: {len(current_rows)} strikes now vs "
                f"{len(previous_rows)} in previous snapshot"
            )

        prev_by_strike = {r["strikePrice"]: r for r in previous_rows}
        for row in current_rows:
            prev = prev_by_strike.get(row["strikePrice"])
            if prev is None:
                continue
            for side in ("CE", "PE"):
                if side in prev and side not in row:
                    reasons.append(
                        f"Strike {row['strikePrice']}: {side} leg present in "
                        f"previous snapshot but missing now"
                    )

    status = "Failed" if reasons else "Verified"
    return IntegrityResult(status=status, reasons=reasons)
