"""Central configuration: constants and environment-loaded secrets.

No API keys are ever hardcoded here - they are read from the environment.
Create a `.env` file (see .env.example) or set real environment variables
before running dashboard.py.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# --- Paths -------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
REPORTS_DIR = BASE_DIR / "reports"
LEDGER_PATH = BASE_DIR / "ledger.csv"
LOG_DIR = BASE_DIR / "logs"
EVOLUTION_LOG_PATH = LOG_DIR / "evolution_decisions.txt"
GEMINI_RAW_ERRORS_LOG_PATH = LOG_DIR / "gemini_raw_errors.log"
STATIC_DIR = BASE_DIR / "static"

SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

NSE_SYMBOL = os.environ.get("NSE_SYMBOL", "NIFTY")

# --- Instrument specifics ------------------------------------------------
STRIKE_STEP = int(os.environ.get("STRIKE_STEP", "50"))  # NIFTY strike spacing
STRIKES_AROUND_ATM = int(os.environ.get("STRIKES_AROUND_ATM", "10"))
TOP_N_LEVELS = int(os.environ.get("TOP_N_LEVELS", "3"))  # support/resistance count
TOP_N_MOVERS = int(os.environ.get("TOP_N_MOVERS", "8"))  # OI buildup rows in report

# NIFTY lot size for Premium Money (Cr) = LTP x OI x lot size / 1,00,00,000.
# NSE revises this periodically - override via env var when it changes.
NIFTY_LOT_SIZE = int(os.environ.get("NIFTY_LOT_SIZE", "65"))

# Conviction tag: strikes in the top/bottom (1 - CONVICTION_PERCENTILE) split
# of volume and |OI interval delta| within the current sheet are tagged
# "high"/"low" - adapts to whatever regime the market is in, no fixed numbers.
CONVICTION_PERCENTILE = float(os.environ.get("CONVICTION_PERCENTILE", "0.25"))

# --- Gemini API ------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

# --- Paper portfolio framing --------------------------------------------
# Starting capital and profit target for the paper-trading ledger (ledger.py)
# and the Gemini strategist persona (gemini_client.py). Position/P&L state
# itself IS tracked across uploads in ledger.csv - see ledger.py.
PORTFOLIO_CAPITAL = int(os.environ.get("PORTFOLIO_CAPITAL", "2500000"))  # Rs 25,00,000
PROFIT_TARGET = int(os.environ.get("PROFIT_TARGET", "200000"))  # Rs 2,00,000

IST = ZoneInfo("Asia/Kolkata")

# --- Dashboard (dashboard.py - the sole entrypoint) ------------------------
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
# Render/Railway/Heroku-style platforms inject PORT - prefer it when present.
DASHBOARD_PORT = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", "8000")))

# --- Misc --------------------------------------------------------------
LEDGER_HISTORY_ROWS_IN_REPORT = 10

DISCLAIMER = (
    "PAPER TRADING SIMULATION ONLY. This report is generated for research and "
    "educational purposes. No real orders are placed. Nothing in this document "
    "constitutes investment advice or a recommendation to buy or sell any "
    "security."
)
