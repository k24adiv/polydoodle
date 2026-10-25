"""
backtest.py — Polybot backtester
Replays ptb_log.json with configurable settings to find best parameters.
Usage: python backtest.py
"""

import json
import argparse
from collections import defaultdict

# ── CONFIG — tweak these to test different settings ───────────────────────────
TRIGGER_DISTANCE  = 60      # minimum $ move from PTB to enter
BET_SIZE          = 5.0     # flat stake per trade
WIN_PAYOUT        = 0.92    # Polymarket pays ~92c per $1 staked on win
AVOID_HOURS       = {1, 2, 5, 16, 17, 23}
CIRCUIT_BREAKER   = 4       # stop after N consecutive losses (0 = disabled)
SKIP_DOJI         = True    # skip DOJI candles
SKIP_LOW_CONF     = False   # skip LOW confidence PTBs
ONLY_STRONG       = False   # only trade STRONG candles (skip MEDIUM)

LOG_PATH = "ptb_log.json"


def run_backtest(
    trigger=TRIGGER_DISTANCE,
    bet=BET_SIZE,
    payout=WIN_PAYOUT,
    avoid_hours=AVOID_HOURS,
    circuit_breaker=CIRCUIT_BREAKER,
    skip_doji=SKIP_DOJI,
    skip_low_conf=SKIP_LOW_CONF,
    only_strong=ONLY_STRONG,
    verbose=False,
):
    with open(LOG_PATH) as f:
        data = json.load(f)

    bankroll       = 1000.0
    trades         = 0
    wins           = 0
    losses         = 0
    skips          = 0
    consec_losses  = 0
    triggered_cb   = False
    pnl_curve      = []
    hour_stats     = defaultdict(lambda: {"w": 0, "l": 0})
    dist_buckets   = defaultdict(lambda: {"w": 0, "l": 0})

    for w in data:
        if triggered_cb:
            break

        hour      = w.get("hour", 0)
        signal    = w.get("signal", "SKIP")
        result    = w.get("result", "SKIP")
        candle    = w.get("candle_type") or "UNKNOWN"
        conf      = w.get("ptb_confidence") or "LOW"
        ptb       = w.get("ptb")
        sig_price = w.get("signal_price")
        fin_price = w.get("final_price")

        # Apply filters
        if signal == "SKIP" or result == "SKIP":
            skips += 1
            continue
        if hour in avoid_hours:
            skips += 1
            continue
        if skip_doji and candle == "DOJI":
            skips += 1
            continue
        if skip_low_conf and conf == "LOW":
            skips += 1
            continue
        if only_strong and candle != "STRONG":
            skips += 1
            continue

        # Check trigger distance
        if ptb is not None and sig_price is not None:
            dist = abs(sig_price - ptb)
        else:
            skips += 1
            continue

        if dist < trigger:
            skips += 1
            continue

        # Recalculate result from raw prices (more accurate than log result)
        if fin_price is not None and ptb is not None:
            if signal == "YES":
                actual_result = "WIN" if fin_price > ptb else "LOSS"
            else:
                actual_result = "WIN" if fin_price < ptb else "LOSS"
        else:
            actual_result = result  # fallback to logged result

        trades += 1
        dist_bucket = f"${int(dist//25)*25}-${int(dist//25)*25+25}"

        if actual_result == "WIN":
            pnl = bet * payout
            wins += 1
            consec_losses = 0
            hour_stats[hour]["w"] += 1
            dist_buckets[dist_bucket]["w"] += 1
        else:
            pnl = -bet
            losses += 1
            consec_losses += 1
            hour_stats[hour]["l"] += 1
            dist_buckets[dist_bucket]["l"] += 1

            if circuit_breaker > 0 and consec_losses >= circuit_breaker:
                bankroll += pnl
                pnl_curve.append(bankroll)
                if verbose:
                    print(f"🔴 Circuit breaker at window {w['window']} — {consec_losses} consecutive losses")
                triggered_cb = True
                break

        bankroll += pnl
        pnl_curve.append(bankroll)

        if verbose:
            emoji = "✅" if actual_result == "WIN" else "❌"
            print(f"  W{w['window']:03d} | {emoji} {signal} | dist ${dist:.0f} | "
                  f"hour {hour:02d} | {candle} | bank ${bankroll:.2f}")

    # ── RESULTS ───────────────────────────────────────────────────────────────
    total_pnl  = bankroll - 1000.0
    win_rate   = wins / trades * 100 if trades > 0 else 0
    per_trade  = total_pnl / trades if trades > 0 else 0

    print(f"\n{'='*55}")
    print(f"  BACKTEST RESULTS")
    print(f"{'='*55}")
    print(f"  Trigger:        ${trigger}")
    print(f"  Avoid hours:    {sorted(avoid_hours)}")
    print(f"  Only STRONG:    {only_strong}")
    print(f"  Circuit break:  {circuit_breaker if circuit_breaker > 0 else 'off'}")
    print(f"{'─'*55}")
    print(f"  Trades:         {trades}  (skipped {skips})")
    print(f"  Wins / Losses:  {wins} / {losses}")
    print(f"  Win rate:       {win_rate:.1f}%")
    print(f"  Total PnL:      ${total_pnl:+.2f}")
    print(f"  Per trade avg:  ${per_trade:+.2f}")
    print(f"  Final bankroll: ${bankroll:.2f}")
    if triggered_cb:
        print(f"  ⚠️  Circuit breaker triggered — bot would have stopped")
    print(f"{'─'*55}")

    # Best and worst hours
    hour_rates = {
        h: (v["w"] / (v["w"] + v["l"]) * 100, v["w"] + v["l"])
        for h, v in hour_stats.items() if v["w"] + v["l"] >= 3
    }
    if hour_rates:
        best  = sorted(hour_rates.items(), key=lambda x: x[1][0], reverse=True)[:3]
        worst = sorted(hour_rates.items(), key=lambda x: x[1][0])[:3]
        print(f"  Best hours:  " + " | ".join(f"{h:02d}h {r:.0f}% ({n}t)" for h,(r,n) in best))
        print(f"  Worst hours: " + " | ".join(f"{h:02d}h {r:.0f}% ({n}t)" for h,(r,n) in worst))

    # Distance breakdown
    print(f"\n  Win rate by trigger distance:")
    for bucket, v in sorted(dist_buckets.items()):
        t = v["w"] + v["l"]
        r = v["w"] / t * 100
        bar = "█" * int(r / 5)
        print(f"    {bucket:>12}: {r:5.1f}%  {bar}  ({t} trades)")

    print(f"{'='*55}\n")
    return {"win_rate": win_rate, "total_pnl": total_pnl, "trades": trades}


def sweep_triggers():
    """Test multiple trigger distances to find the sweet spot."""
    print("\n📊 TRIGGER DISTANCE SWEEP")
    print(f"{'─'*50}")
    print(f"  {'Trigger':>8} | {'Trades':>6} | {'Win%':>6} | {'PnL':>8} | {'$/trade':>7}")
    print(f"{'─'*50}")
    for t in [30, 40, 50, 60, 75, 100, 125]:
        r = run_backtest(trigger=t, circuit_breaker=0, verbose=False)
        # suppress the full output — just print summary row
    print(f"{'─'*50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polybot backtester")
    parser.add_argument("--trigger",  type=float, default=TRIGGER_DISTANCE)
    parser.add_argument("--avoid",    type=int,   nargs="+", default=list(AVOID_HOURS))
    parser.add_argument("--strong",   action="store_true", help="STRONG candles only")
    parser.add_argument("--cb",       type=int,   default=CIRCUIT_BREAKER, help="Circuit breaker limit")
    parser.add_argument("--verbose",  action="store_true")
    parser.add_argument("--sweep",    action="store_true", help="Sweep trigger distances")
    args = parser.parse_args()

    if args.sweep:
        # Clean sweep — print one line per trigger
        print(f"\n📊 TRIGGER DISTANCE SWEEP (avoid={sorted(args.avoid)}, STRONG={args.strong})")
        print(f"{'─'*55}")
        print(f"  {'Trigger':>8} | {'Trades':>6} | {'Win%':>6} | {'Total PnL':>10} | {'$/trade':>7}")
        print(f"{'─'*55}")
        for t in [30, 40, 50, 60, 75, 100, 125]:
            # redirect run_backtest output temporarily
            import io, sys
            old = sys.stdout; sys.stdout = io.StringIO()
            r = run_backtest(trigger=t, avoid_hours=set(args.avoid),
                             only_strong=args.strong, circuit_breaker=0)
            sys.stdout = old
            ppw = r["total_pnl"] / r["trades"] if r["trades"] else 0
            print(f"  ${t:>7} | {r['trades']:>6} | {r['win_rate']:>5.1f}% | "
                  f"${r['total_pnl']:>+9.2f} | ${ppw:>+6.2f}")
        print(f"{'─'*55}\n")
    else:
        run_backtest(
            trigger=args.trigger,
            avoid_hours=set(args.avoid),
            only_strong=args.strong,
            circuit_breaker=args.cb,
            verbose=args.verbose,
        )
