"""
train_model.py — retrain the logistic regression ML filter.

Run manually: python train_model.py

Features used:
    distance       — how far BTC was from PTB at entry ($)
    hour           — UTC hour of the window
    volume         — 24h BTC volume at entry
    ptb_confidence — encoded: HIGH=2, MEDIUM=1, LOW=0
    candle_type    — encoded: STRONG=2, MEDIUM=1, DOJI=0
                     (DOJI windows never reach a trade, but kept for completeness)

Target:
    result == "WIN"
"""

import json
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import pickle

LOG_FILE  = "ptb_log.json"
MODEL_OUT = "model.pkl"
MIN_PROB  = 0.55   # threshold used by predictor.py at runtime

CONFIDENCE_MAP = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}
CANDLE_MAP     = {"STRONG": 2, "MEDIUM": 1, "DOJI": 0}


def load_data():
    with open(LOG_FILE, "r") as f:
        windows = json.load(f)

    X, y = [], []

    for w in windows:
        # Only use windows that were actually traded
        if w.get("result") not in ("WIN", "LOSS"):
            continue
        if w.get("signal_price") is None:
            continue

        distance   = abs(w.get("signal_price", 0) - w.get("ptb", 0))
        hour       = w.get("hour", 0)
        volume     = w.get("volume") or 0
        confidence = CONFIDENCE_MAP.get(w.get("ptb_confidence", "LOW"), 0)
        candle     = CANDLE_MAP.get(w.get("candle_type", "STRONG"), 2)
        result     = 1 if w["result"] == "WIN" else 0

        X.append([distance, hour, volume, confidence, candle])
        y.append(result)

    return np.array(X), np.array(y)


def main():
    print("📂 Loading data from ptb_log.json...")
    X, y = load_data()
    print(f"   {len(X)} tradeable windows loaded")
    print(f"   Wins: {y.sum()} | Losses: {len(y) - y.sum()}")

    if len(X) < 50:
        print("⚠️  Less than 50 tradeable windows — model may not be reliable.")
        print("   Keep collecting data before relying on ML filter.")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )

    # class_weight='balanced' prevents model predicting every trade as WIN
    # when wins outnumber losses 2:1 — forces model to learn loss patterns
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            max_iter=1000,
            random_state=42,
            class_weight="balanced"
        )
    )
    model.fit(X_train, y_train)

    print("\n📊 Classification report (test set):")
    print(classification_report(y_test, model.predict(X_test),
                                 target_names=["LOSS", "WIN"]))

    # Threshold analysis — show win rate at different probability cutoffs
    probs = model.predict_proba(X_test)[:, 1]
    print("\n📈 Threshold analysis:")
    print(f"  {'Threshold':<12} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'Skipped%':>10}")
    print(f"  {'-'*50}")
    for thresh in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]:
        mask   = probs >= thresh
        trades = mask.sum()
        if trades == 0:
            continue
        wins     = y_test[mask].sum()
        win_rate = wins / trades * 100
        skipped  = (1 - trades / len(y_test)) * 100
        marker   = "  ← current" if thresh == MIN_PROB else ""
        print(f"  {thresh:<12.0%} {trades:>7} {wins:>6} {win_rate:>6.1f}% {skipped:>9.1f}%{marker}")

    print(f"\n  Tip: Pick the threshold where win_rate is highest")
    print(f"  without skipping too many trades (aim to keep 40%+ of trades).")

    with open(MODEL_OUT, "wb") as f:
        pickle.dump(model, f)

    print(f"\n{'='*50}")
    print(f"  SAVED")
    print(f"{'='*50}")
    print(f"  Model saved to {MODEL_OUT}")
    print(f"  Predictor will use MIN_PROB = {MIN_PROB:.0%}")
    print(f"\n  ✅ Training complete. Now run your bot — predictor.py will load this model.")


if __name__ == "__main__":
    main()