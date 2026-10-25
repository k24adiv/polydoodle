"""
logger.py — Multi-Asset Audit Trail
====================================
Provides:
  • get_logger(asset)   → contextual logger with [ASSET] prefix
  • log_window()        → append structured trade record to ptb_log.json
  • print_stats()       → full breakdown table (candle / distance / stake / asset)
  • log_raw_api()       → write raw API payloads to trades.log at DEBUG level

Logging levels
  Console  → INFO   (human-readable, coloured by level)
  File     → DEBUG  (trades.log — every raw API call, the "Black Box")
"""

import json
import logging
import os
import sys
from datetime import datetime

# ─── Constants ────────────────────────────────────────────────────────────────

LOG_FILE   = "ptb_log.json"      # structured trade records (JSON)
TRADE_LOG  = "trades.log"        # raw black-box log (text, DEBUG)
ASSETS     = ["BTC", "ETH", "SOL", "XRP"]

# ─── ANSI colour helpers (console only) ───────────────────────────────────────

_COLOURS = {
    "DEBUG":    "\033[37m",    # grey
    "INFO":     "\033[36m",    # cyan
    "WARNING":  "\033[33m",    # yellow
    "ERROR":    "\033[31m",    # red
    "CRITICAL": "\033[35m",    # magenta
    "RESET":    "\033[0m",
}

# Asset badge colours so each asset is visually distinct in the terminal
_ASSET_COLOURS = {
    "BTC": "\033[93m",   # bright yellow
    "ETH": "\033[94m",   # bright blue
    "SOL": "\033[92m",   # bright green
    "XRP": "\033[96m",   # bright cyan
    "SYS": "\033[97m",   # white  (used for system/general messages)
}


class _AssetFilter(logging.Filter):
    """Injects `asset` into every LogRecord so the formatter can use %(asset)s."""
    def __init__(self, asset: str):
        super().__init__()
        self.asset = asset

    def filter(self, record):
        record.asset = self.asset
        return True


class _ColouredConsoleFormatter(logging.Formatter):
    """Coloured formatter for the console handler."""

    FMT = "%(asctime)s | [%(asset)-3s] | %(levelname)-8s | %(message)s"
    DATEFMT = "%H:%M:%S"

    def format(self, record):
        level_colour  = _COLOURS.get(record.levelname, "")
        asset_colour  = _ASSET_COLOURS.get(getattr(record, "asset", "SYS"), _ASSET_COLOURS["SYS"])
        reset         = _COLOURS["RESET"]

        # Reconstruct the format with inline colour codes
        coloured_fmt = (
            f"%(asctime)s | "
            f"{asset_colour}[%(asset)-3s]{reset} | "
            f"{level_colour}%(levelname)-8s{reset} | "
            f"%(message)s"
        )
        formatter = logging.Formatter(coloured_fmt, datefmt=self.DATEFMT)
        return formatter.format(record)


class _PlainFileFormatter(logging.Formatter):
    """Plain formatter for the file handler — no ANSI codes."""

    FMT     = "%(asctime)s | [%(asset)-3s] | %(levelname)-8s | %(message)s"
    DATEFMT = "%Y-%m-%d %H:%M:%S"

    def format(self, record):
        formatter = logging.Formatter(self.FMT, datefmt=self.DATEFMT)
        return formatter.format(record)


# ─── Logger factory ───────────────────────────────────────────────────────────

# One shared file handler so all assets write to the same trades.log
_file_handler = logging.FileHandler(TRADE_LOG, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_PlainFileFormatter())

# Track already-configured loggers to avoid duplicate handlers on re-import
_logger_registry: dict[str, logging.Logger] = {}


def get_logger(asset: str = "SYS") -> logging.Logger:
    """
    Return a Logger tagged with [asset].
    Safe to call multiple times — returns the same instance.

    Usage:
        log = get_logger("BTC")
        log.info("Signal fired → STRONG UP")
        log.debug("Raw API payload: %s", response_dict)
    """
    asset = asset.upper()
    if asset in _logger_registry:
        return _logger_registry[asset]

    logger = logging.getLogger(f"polybot.{asset}")
    logger.setLevel(logging.DEBUG)          # capture everything; handlers filter

    if not logger.handlers:
        # Console handler — INFO and above
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(_ColouredConsoleFormatter())

        logger.addHandler(console)
        logger.addHandler(_file_handler)

    logger.addFilter(_AssetFilter(asset))
    logger.propagate = False

    _logger_registry[asset] = logger
    return logger


def log_raw_api(asset: str, label: str, payload: dict | list | str) -> None:
    """
    Write a raw API payload to trades.log at DEBUG level.
    Call this around every external API call so you have a full black box.

    Usage:
        log_raw_api("BTC", "ORDER_RESPONSE", response_dict)
        log_raw_api("ETH", "GAMMA_POSITIONS", positions_list)
    """
    log = get_logger(asset)
    try:
        payload_str = json.dumps(payload, indent=2, default=str)
    except Exception:
        payload_str = str(payload)
    log.debug("── %s ──\n%s", label, payload_str)


# ─── JSON trade log (ptb_log.json) ────────────────────────────────────────────

def load_log() -> list:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(log: list) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


def get_next_window_number() -> int:
    """Global window counter — never resets between restarts."""
    log = load_log()
    if not log:
        return 1
    return max(w.get("window", 0) for w in log) + 1


def get_last_results(asset: str | None = None, n: int = 3) -> list[str]:
    """
    Return the last n traded results as ['WIN','LOSS',...].
    Pass asset='BTC' to filter to a specific asset; omit for all assets.
    """
    log = load_log()
    traded = [
        w for w in log
        if w.get("result") not in ("SKIP", None)
        and (asset is None or w.get("asset", "").upper() == asset.upper())
    ]
    return [w["result"] for w in traded[-n:]]


def log_window(window_data: dict) -> None:
    """
    Append one window record to ptb_log.json.
    Expected keys (all optional except those marked *):
      window*  asset*  result*  mode  candle_type  distance
      bet_size  stake  pnl  signal  timestamp
    """
    # Stamp with ISO timestamp if caller didn't
    window_data.setdefault("timestamp", datetime.utcnow().isoformat())

    log = load_log()
    log.append(window_data)
    save_log(log)

    asset = window_data.get("asset", "SYS")
    logger = get_logger(asset)
    logger.info(
        "Window #%s logged | result=%s | pnl=%s | total_records=%d",
        window_data.get("window", "?"),
        window_data.get("result", "?"),
        f"${window_data.get('pnl', 0):.2f}",
        len(log),
    )


# ─── Stats printer ────────────────────────────────────────────────────────────

def print_stats(asset_filter: str | None = None) -> None:
    """
    Print a full breakdown table.
    Pass asset_filter='BTC' to scope to one asset, or omit for all assets.
    """
    log = load_log()
    if not log:
        print("No data yet.")
        return

    # Optionally scope to one asset
    if asset_filter:
        log = [w for w in log if w.get("asset", "").upper() == asset_filter.upper()]
        if not log:
            print(f"No data for asset: {asset_filter}")
            return

    # ── Core counts ──────────────────────────────────────────────────────────
    trades = [w for w in log if w.get("signal", w.get("result")) != "SKIP"]
    wins   = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]
    skips  = [w for w in log  if w.get("signal", w.get("result")) == "SKIP"]

    total_pnl = sum(t.get("pnl", 0) for t in trades)
    winrate   = len(wins) / len(trades) * 100 if trades else 0

    recent_mode  = log[-1].get("mode", "PAPER") if log else "PAPER"
    header_title = "🔴 LIVE STATS" if recent_mode == "LIVE" else "📝 PAPER STATS"
    if asset_filter:
        header_title += f" [{asset_filter}]"

    # ── Candle breakdown ─────────────────────────────────────────────────────
    candle_lines = []
    for ct in ("STRONG", "MEDIUM"):
        ct_trades = [t for t in trades if t.get("candle_type") == ct]
        if ct_trades:
            ct_wins = sum(1 for t in ct_trades if t.get("result") == "WIN")
            ct_wr   = ct_wins / len(ct_trades) * 100
            ct_pnl  = sum(t.get("pnl", 0) for t in ct_trades)
            candle_lines.append(
                f"║  {ct:<7} {len(ct_trades):>3} trades | {ct_wr:>5.1f}% WR | ${ct_pnl:>+.2f}{'':4}║"
            )

    # ── Distance bucket breakdown ─────────────────────────────────────────
    dist_buckets = [
        (0.0000, 0.0018, "<0.18%"),
        (0.0018, 0.0036, "0.18-0.36%"),
        (0.0036, 9999.0, ">0.36%"),
    ]
    dist_lines = []
    for lo, hi, label in dist_buckets:
        b = [
            t for t in trades
            if t.get("distance_pct") is not None
            and lo <= abs(t["distance_pct"]) < hi
        ]
        # Fallback: raw USD distance (legacy records)
        if not b:
            usd_ranges = {"<0.18%": (0, 55), "0.18-0.36%": (55, 120), ">0.36%": (120, 9999)}
            lo_usd, hi_usd = usd_ranges.get(label, (0, 0))
            b = [
                t for t in trades
                if t.get("distance") is not None
                and t.get("distance_pct") is None
                and lo_usd <= abs(t["distance"]) < hi_usd
            ]
        if b:
            bw   = sum(1 for t in b if t.get("result") == "WIN")
            bwr  = bw / len(b) * 100
            bpnl = sum(t.get("pnl", 0) for t in b)
            dist_lines.append(
                f"║  {label:<11} {len(b):>3} trades | {bwr:>5.1f}% WR | ${bpnl:>+.2f}{'':2}║"
            )

    # ── Stake breakdown ───────────────────────────────────────────────────────
    stakes_used = sorted(set(str(t.get("bet_size", t.get("stake", "?"))) for t in trades))
    stake_lines = []
    for s in stakes_used:
        s_trades = [t for t in trades if str(t.get("bet_size", t.get("stake", "?"))) == s]
        s_wins   = sum(1 for t in s_trades if t.get("result") == "WIN")
        s_wr     = s_wins / len(s_trades) * 100 if s_trades else 0
        s_pnl    = sum(t.get("pnl", 0) for t in s_trades)
        stake_lines.append(
            f"║  ${s:<6} {len(s_trades):>3} trades | {s_wr:>5.1f}% WR | ${s_pnl:>+.2f}{'':4}║"
        )

    # ── Per-asset breakdown ───────────────────────────────────────────────────
    asset_lines = []
    all_assets = sorted(set(t.get("asset", "???") for t in trades if t.get("asset")))
    for a in all_assets:
        a_trades = [t for t in trades if t.get("asset") == a]
        a_wins   = sum(1 for t in a_trades if t.get("result") == "WIN")
        a_wr     = a_wins / len(a_trades) * 100 if a_trades else 0
        a_pnl    = sum(t.get("pnl", 0) for t in a_trades)
        asset_lines.append(
            f"║  {a:<5} {len(a_trades):>3} trades | {a_wr:>5.1f}% WR | ${a_pnl:>+.2f}{'':4}║"
        )

    # ── Print ─────────────────────────────────────────────────────────────────
    W = 40  # box inner width

    def _row(label: str, value: str) -> str:
        content = f" {label:<12}{value}"
        return f"║{content:<{W}}║"

    print(f"""
╔{'═'*W}╗
║{'':^{W-len(header_title)//2}}{header_title:^{W}}║
╠{'═'*W}╣
{_row('Windows:', str(len(log)))}
{_row('Trades:', str(len(trades)))}
{_row('Wins:', str(len(wins)))}
{_row('Losses:', str(len(losses)))}
{_row('Skips:', str(len(skips)))}
{_row('Win Rate:', f"{winrate:.1f}%")}
{_row('Total PnL:', f"${total_pnl:+.2f}")}
╠{'═'*W}╣
║{'BY ASSET':<{W}}║""")
    for line in asset_lines:
        print(line)
    if not asset_lines:
        print(f"║{'  No asset data yet':<{W}}║")

    print(f"╠{'═'*W}╣")
    print(f"║{'BY CANDLE TYPE':<{W}}║")
    for line in candle_lines:
        print(line)
    if not candle_lines:
        print(f"║{'  No candle data yet':<{W}}║")

    print(f"╠{'═'*W}╣")
    print(f"║{'BY DISTANCE':<{W}}║")
    for line in dist_lines:
        print(line)
    if not dist_lines:
        print(f"║{'  No distance data yet':<{W}}║")

    print(f"╠{'═'*W}╣")
    print(f"║{'BY STAKE SIZE':<{W}}║")
    for line in stake_lines:
        print(line)
    if not stake_lines:
        print(f"║{'  No stake data yet':<{W}}║")

    print(f"╚{'═'*W}╝")
    
    