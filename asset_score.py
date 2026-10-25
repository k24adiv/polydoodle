"""
asset_score.py — Per-Asset Adaptive Confidence Scorer
------------------------------------------------------
Replaces the hard "2 losses = 6hr ban" idea with a continuous score
that decays on losses and recovers naturally over time.

How it works
------------
Each asset starts at score 1.0 (full confidence).

  Loss  -> score *= LOSS_DECAY       (halved: 1.0 -> 0.5 -> 0.25)
  Win   -> score  = min(score * WIN_BOOST, MAX_SCORE)
  Time  -> every window, score += TIME_RECOVERY  (passive drift back up)
  Skip  -> score += SKIP_RECOVERY                (half recovery on no-trade)

Entry is blocked when score < SCORE_THRESHOLD (0.4).

Practical behaviour
-------------------
  2 consecutive losses:  1.0 -> 0.5 -> 0.25  -> BLOCKED  (below 0.4)
  Recovery without trade: needs ~7 windows (~105 min) to cross 0.4 again
  1 win after a loss:    0.5 -> min(0.65, 1.0) -> back to tradeable
  This mirrors the "6-8hr ban after 2 losses" intent but is continuous
  and self-healing — no hardcoded timers needed.

Thread safety
-------------
All state is guarded by _lock.  Safe to call from any thread.

Public API
----------
  score_manager.on_win(asset)
  score_manager.on_loss(asset)
  score_manager.on_skip(asset)        # call each window even when no trade
  score_manager.on_window_end(asset)  # passive time recovery, call last
  score_manager.is_tradeable(asset)   # returns bool
  score_manager.get_score(asset)      # returns float 0.0-1.0
  score_manager.summary()             # returns dict for logging/display
"""

import threading
import time

# ── Tuning constants ───────────────────────────────────────────────────────────
SCORE_THRESHOLD = 0.40   # below this -> asset suspended
MAX_SCORE       = 1.00   # cap so wins don't over-inflate

LOSS_DECAY      = 0.50   # score halved on every loss
WIN_BOOST       = 1.30   # 30% recovery boost on a win
TIME_RECOVERY   = 0.07   # passive recovery per window (~14 windows to full)
SKIP_RECOVERY   = 0.035  # half recovery when window skipped (no trade taken)


class AssetScoreManager:
    """
    Singleton-style manager.
    Import and use the module-level `score_manager` instance directly.
    """

    def __init__(self, assets: list):
        self._lock        = threading.Lock()
        self._scores      = {a: MAX_SCORE for a in assets}
        self._last_event  = {a: "init"    for a in assets}
        self._event_ts    = {a: time.time() for a in assets}

    # ── Mutators ───────────────────────────────────────────────────────────────

    def on_win(self, asset: str) -> None:
        with self._lock:
            old = self._scores.get(asset, MAX_SCORE)
            self._scores[asset]     = min(old * WIN_BOOST, MAX_SCORE)
            self._last_event[asset] = "WIN"
            self._event_ts[asset]   = time.time()

    def on_loss(self, asset: str) -> None:
        with self._lock:
            old = self._scores.get(asset, MAX_SCORE)
            self._scores[asset]     = old * LOSS_DECAY
            self._last_event[asset] = "LOSS"
            self._event_ts[asset]   = time.time()

    def on_skip(self, asset: str) -> None:
        """Call each window for any asset that had no trade outcome."""
        with self._lock:
            old = self._scores.get(asset, MAX_SCORE)
            self._scores[asset]     = min(old + SKIP_RECOVERY, MAX_SCORE)
            self._last_event[asset] = "SKIP"

    def on_window_end(self, asset: str) -> None:
        """
        Passive time-based recovery. Call once per window for every asset,
        AFTER on_win / on_loss / on_skip, so recovery compounds correctly.
        """
        with self._lock:
            old = self._scores.get(asset, MAX_SCORE)
            self._scores[asset] = min(old + TIME_RECOVERY, MAX_SCORE)

    # ── Queries ────────────────────────────────────────────────────────────────

    def is_tradeable(self, asset: str) -> bool:
        with self._lock:
            return self._scores.get(asset, MAX_SCORE) >= SCORE_THRESHOLD

    def get_score(self, asset: str) -> float:
        with self._lock:
            return round(self._scores.get(asset, MAX_SCORE), 3)

    def summary(self) -> dict:
        """Returns a snapshot dict — use for logging/display."""
        with self._lock:
            return {
                a: {
                    "score":      round(s, 3),
                    "tradeable":  s >= SCORE_THRESHOLD,
                    "last_event": self._last_event.get(a, "?"),
                }
                for a, s in self._scores.items()
            }


# ── Module-level singleton ─────────────────────────────────────────────────────
# Initialised with a placeholder list; main.py calls _init_score_manager(ASSETS)
# after importing ASSETS from ptb_scanner, so the singleton is populated once
# with the real asset list.
score_manager = AssetScoreManager([])


def init_score_manager(assets: list) -> None:
    """
    Call once at bot startup (after importing ASSETS) to populate the
    singleton with the correct asset list.  Idempotent.
    """
    global score_manager
    score_manager = AssetScoreManager(assets)
