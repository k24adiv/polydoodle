"""
predictor.py — Multi-Asset Predictor
Normalizes all distance inputs to percentages for equal asset weighting.

Fixes applied:
  - Added upper-bound volatility guard (rejects distance_pct > MAX_DISTANCE_PCT)
  - Added input validation for distance_pct (None / NaN / negative)
  - Added volume guard (None / negative)
  - Unknown confidence/candle strings now emit a warning instead of silently
    encoding as 0
  - Separated raw feature list from scaler output (no variable re-use)
  - status() now reports model class and scaler presence
  - CONF_MAP / CANDLE_MAP made read-only via types.MappingProxyType
"""

import os
import math
import pickle
import types

MODEL_FILE = "model.pkl"

# ── Read-only lookup tables (prevents accidental mutation during long runs) ──
CONF_MAP: types.MappingProxyType = types.MappingProxyType(
    {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
)
CANDLE_MAP: types.MappingProxyType = types.MappingProxyType(
    {"DOJI": 0, "MEDIUM": 1, "STRONG": 2}
)

MIN_PROB_DEFAULT  = 0.55

# FIX 1 ── Upper-bound volatility filter (was described in spec but never coded)
# Moves larger than this are almost certainly a flash crash / data spike.
# 0.02 = 2 % move in 15 minutes.
MAX_DISTANCE_PCT  = 0.02


class Predictor:
    def __init__(self):
        self.model     = None
        self.scaler    = None
        self.min_prob  = MIN_PROB_DEFAULT
        self.enabled   = False
        self._load()

    # ── Private ────────────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(MODEL_FILE):
            print("[Predictor] No model.pkl found — running without ML filter.")
            return
        try:
            with open(MODEL_FILE, "rb") as f:
                bundle = pickle.load(f)
            if isinstance(bundle, dict):
                self.model    = bundle["model"]
                self.scaler   = bundle.get("scaler")
                self.min_prob = bundle.get("min_prob", MIN_PROB_DEFAULT)
            else:
                # Legacy: bare model object
                self.model    = bundle
                self.scaler   = None
                self.min_prob = MIN_PROB_DEFAULT
            self.enabled = True
            print(
                f"[Predictor] ✅ Model loaded ({type(self.model).__name__}). "
                f"Scaler: {'yes' if self.scaler else 'no'}. "
                f"Min-prob threshold: {self.min_prob:.0%}"
            )
        except Exception as e:
            print(f"[Predictor] ⚠  Failed to load model: {e}. Running without ML filter.")
            self.enabled = False

    @staticmethod
    def _validate_inputs(distance_pct, volume):
        """
        Returns (ok: bool, reason: str).
        Centralises all pre-prediction sanity checks so they run whether or
        not the ML model is enabled.
        """
        # FIX 3 ── Guard: distance_pct must be a real, positive number
        if distance_pct is None or (isinstance(distance_pct, float) and math.isnan(distance_pct)):
            return False, "distance_pct is None/NaN — SKIP"
        if distance_pct <= 0:
            return False, f"distance_pct={distance_pct:.4%} is non-positive — SKIP"

        # FIX 1 ── Upper-bound volatility filter
        if distance_pct > MAX_DISTANCE_PCT:
            return False, (
                f"distance_pct={distance_pct:.2%} exceeds max "
                f"{MAX_DISTANCE_PCT:.0%} — possible flash crash, SKIP"
            )

        # FIX 5 ── Guard: volume must be usable
        if volume is None or (isinstance(volume, float) and math.isnan(volume)):
            return False, "volume is None/NaN — SKIP"
        if volume < 0:
            return False, f"volume={volume} is negative — SKIP"

        return True, ""

    # ── Public API ─────────────────────────────────────────────────────────────

    def should_trade(
        self,
        distance_pct: float,
        hour: int,
        volume: float,
        confidence: str,
        candle_type: str = "STRONG",
    ):
        """
        Evaluate whether the current signal warrants a trade.

        Parameters
        ----------
        distance_pct : float
            Normalised distance from PTB as a fraction, e.g. 0.0018 = 0.18 %.
        hour : int
            UTC hour of the window (0–23).
        volume : float
            Market volume proxy (must be ≥ 0).
        confidence : str
            One of "LOW" | "MEDIUM" | "HIGH".
        candle_type : str
            One of "DOJI" | "MEDIUM" | "STRONG".

        Returns
        -------
        (allow: bool, prob: float | None, reason: str)
        """
        # ── Input validation (runs regardless of model availability) ──────────
        valid, reason = self._validate_inputs(distance_pct, volume)
        if not valid:
            return False, None, reason

        # ── FIX 4 — Warn on unknown categorical inputs ────────────────────────
        if confidence not in CONF_MAP:
            print(
                f"[Predictor] ⚠  Unknown confidence '{confidence}', "
                f"defaulting to LOW. Check signal engine."
            )
        if candle_type not in CANDLE_MAP:
            print(
                f"[Predictor] ⚠  Unknown candle_type '{candle_type}', "
                f"defaulting to DOJI. Check signal engine."
            )

        conf_encoded   = CONF_MAP.get(confidence, 0)
        candle_encoded = CANDLE_MAP.get(candle_type, 0)   # was 2 (STRONG) — now 0 (DOJI, pessimistic default)

        # No model — signal engine rules only
        if not self.enabled:
            return True, None, "No model — signal engine rules only"

        # ── FIX 2 — Use a separate variable for scaled features ───────────────
        raw_features = [[distance_pct, hour, volume, conf_encoded, candle_encoded]]
        try:
            if self.scaler is not None:
                X = self.scaler.transform(raw_features)
            else:
                X = raw_features

            prob = self.model.predict_proba(X)[0][1]

            if prob >= self.min_prob:
                return (
                    True,
                    prob,
                    f"Model confidence {prob:.0%} ≥ threshold {self.min_prob:.0%}",
                )
            else:
                return (
                    False,
                    prob,
                    f"Model confidence {prob:.0%} < threshold {self.min_prob:.0%} — SKIP",
                )

        except Exception as e:
            print(f"[Predictor] ⚠  Prediction error: {e}. Allowing trade (fallback).")
            return True, None, f"Prediction error — fallback allow"

    def status(self) -> str:
        # FIX 6 ── Report model class and scaler presence for easier debugging
        if self.enabled:
            model_name   = type(self.model).__name__
            scaler_info  = f", scaler={type(self.scaler).__name__}" if self.scaler else ", no scaler"
            return (
                f"ML filter ON — {model_name}{scaler_info} "
                f"(threshold {self.min_prob:.0%}, max_dist {MAX_DISTANCE_PCT:.0%})"
            )
        return "ML filter OFF (no model loaded)"