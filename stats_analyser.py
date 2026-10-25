"""
Spidey Bot — Stats Analyser
Run this from your polybot folder in VS Code terminal:
    python stats_analyser.py
"""

import json
import os
from collections import defaultdict

# ── CONFIG ──────────────────────────────────────────────────────────────────
LOG_FILE         = "ptb_log.json"
BANKROLL_START   = 1000.0
BET_SIZE         = 5.0
# ────────────────────────────────────────────────────────────────────────────

def load_data(path):
    if not os.path.exists(path):
        print(f"[ERROR] Cannot find {path}. Run this script from your polybot folder.")
        exit(1)
    with open(path) as f:
        return json.load(f)

def colour(text, code):
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36", "bold": "1", "reset": "0"}
    return f"\033[{codes[code]}m{text}\033[0m"

def bar(wins, total, width=20):
    if total == 0:
        return "[" + "-" * width + "]"
    filled = int((wins / total) * width)
    pct    = wins / total * 100
    colour_code = "green" if pct >= 65 else ("yellow" if pct >= 50 else "red")
    return colour("[" + "█" * filled + "░" * (width - filled) + f"] {wins}/{total} = {pct:.1f}%", colour_code)

def kelly(win_rate, b=1.0):
    p = win_rate / 100
    q = 1 - p
    f = (b * p - q) / b
    return max(f, 0), max(f / 2, 0)

def section(title):
    width = 55
    print("\n" + colour("═" * width, "cyan"))
    print(colour(f"  {title}", "bold"))
    print(colour("═" * width, "cyan"))

def analyse_set(data, traded, label="ALL"):
    """Core analysis block — reused for both paper and live."""

    wins    = [w for w in traded if w["result"] == "WIN"]
    losses  = [w for w in traded if w["result"] == "LOSS"]
    skipped = [w for w in data   if w["result"] == "SKIP"]

    if not traded:
        print(colour(f"  No traded windows yet in {label} set.", "yellow"))
        return

    win_rate = len(wins) / len(traded) * 100
    net_pnl  = len(wins) * BET_SIZE - len(losses) * BET_SIZE
    roi      = (net_pnl / BANKROLL_START) * 100

    print(f"  Total windows logged : {len(data)}")
    print(f"  Traded               : {len(traded)}   Skipped: {len(skipped)} ({len(skipped)/max(len(data),1)*100:.1f}%)")
    print(f"  Wins / Losses        : {len(wins)} / {len(losses)}")
    print(f"  Win rate             : {colour(f'{win_rate:.1f}%', 'green' if win_rate >= 65 else 'yellow')}")
    print(f"  Net PnL              : {colour(f'${net_pnl:+.2f}', 'green' if net_pnl >= 0 else 'red')}")
    print(f"  ROI on bankroll      : {roi:.1f}%")

    return win_rate

def main():
    data = load_data(LOG_FILE)

    # Split into paper and live
    paper_data = [w for w in data if w.get("trading_mode", "paper") == "paper"]
    live_data  = [w for w in data if w.get("trading_mode", "paper") == "live"]

    all_traded    = [w for w in data       if w["result"] != "SKIP"]
    paper_traded  = [w for w in paper_data if w["result"] != "SKIP"]
    live_traded   = [w for w in live_data  if w["result"] != "SKIP"]

    if not all_traded:
        print("No traded windows found yet.")
        return

    # ── OVERVIEW — ALL ────────────────────────────────────────────────────────
    section("OVERVIEW — ALL TIME")
    analyse_set(data, all_traded, "ALL")

    # ── LIVE PERFORMANCE ──────────────────────────────────────────────────────
    section("LIVE TRADING PERFORMANCE")
    if not live_traded:
        print(colour("  No live windows yet — bot is still in paper mode or", "yellow"))
        print(colour("  TRADING_MODE=live not set in .env", "yellow"))
    else:
        live_wins   = [w for w in live_traded if w["result"] == "WIN"]
        live_losses = [w for w in live_traded if w["result"] == "LOSS"]
        live_wr     = len(live_wins) / len(live_traded) * 100
        live_pnl    = len(live_wins) * BET_SIZE - len(live_losses) * BET_SIZE

        print(f"  Live windows traded  : {len(live_traded)}")
        print(f"  Wins / Losses        : {len(live_wins)} / {len(live_losses)}")
        print(f"  Live win rate        : {colour(f'{live_wr:.1f}%', 'green' if live_wr >= 65 else ('yellow' if live_wr >= 55 else 'red'))}")
        print(f"  Live PnL             : {colour(f'${live_pnl:+.2f}', 'green' if live_pnl >= 0 else 'red')}")
        print()

        # Phase guidance
        if len(live_traded) < 50:
            remaining = 50 - len(live_traded)
            print(colour(f"  📍 PHASE 1 — Validation ({len(live_traded)}/50 windows)", "cyan"))
            print(colour(f"     {remaining} more windows needed before stake increase", "yellow"))
            print(colour(f"     Keep flat $5 — do not change stakes yet", "yellow"))
        elif live_wr >= 65:
            print(colour(f"  ✅ PHASE 1 COMPLETE — {len(live_traded)} windows at {live_wr:.1f}%", "green"))
            print(colour(f"     Ready to increase to flat $10", "green"))
        else:
            print(colour(f"  ⚠️  PHASE 1 — Win rate {live_wr:.1f}% below 65% target", "red"))
            print(colour(f"     Stay at $5 flat — diagnose before increasing stakes", "red"))

        # Compare to paper
        if paper_traded:
            paper_wr = len([w for w in paper_traded if w["result"] == "WIN"]) / len(paper_traded) * 100
            drop     = paper_wr - live_wr
            print()
            print(f"  Paper win rate       : {paper_wr:.1f}%")
            print(f"  Live win rate        : {live_wr:.1f}%")
            if drop <= 8:
                print(colour(f"  ✅ Drop of {drop:.1f}% — within expected 5-10% range", "green"))
            else:
                print(colour(f"  ⚠️  Drop of {drop:.1f}% — larger than expected, investigate", "yellow"))

    # ── PAPER PERFORMANCE ─────────────────────────────────────────────────────
    section("PAPER TRADING PERFORMANCE")
    if not paper_traded:
        print(colour("  No paper windows found.", "yellow"))
    else:
        analyse_set(paper_data, paper_traded, "PAPER")

    # ── PTB CONFIDENCE ───────────────────────────────────────────────────────
    section("WIN RATE BY PTB CONFIDENCE")
    for conf in ["HIGH", "MEDIUM", "LOW"]:
        group = [w for w in all_traded if w.get("ptb_confidence") == conf]
        w     = [x for x in group if x["result"] == "WIN"]
        label = colour(f"  {conf:<8}", "bold")
        print(f"{label} {bar(len(w), len(group))}")

    # ── CANDLE TYPE ──────────────────────────────────────────────────────────
    section("WIN RATE BY CANDLE TYPE")
    candle_windows = [w for w in all_traded if w.get("candle_type") is not None]
    if not candle_windows:
        print(colour("  ⏳ No candle_type data yet", "yellow"))
    else:
        for ctype in ["STRONG", "MEDIUM", "DOJI"]:
            group = [w for w in candle_windows if w["candle_type"] == ctype]
            w     = [x for x in group if x["result"] == "WIN"]
            label = colour(f"  {ctype:<8}", "bold")
            note  = ""
            if group:
                pct = len(w) / len(group) * 100
                if ctype == "STRONG" and pct >= 75:
                    note = colour("  ✅ Strong candles performing well", "green")
                elif ctype == "DOJI" and len(group) > 0:
                    note = colour("  ⚠️  DOJI trades should be skipped", "red")
            print(f"{label} {bar(len(w), len(group))}{note}")

        strong_group = [w for w in candle_windows if w["candle_type"] == "STRONG"]
        medium_group = [w for w in candle_windows if w["candle_type"] == "MEDIUM"]
        if strong_group and medium_group:
            s_wr = len([w for w in strong_group if w["result"] == "WIN"]) / len(strong_group) * 100
            m_wr = len([w for w in medium_group if w["result"] == "WIN"]) / len(medium_group) * 100
            if s_wr > m_wr + 5:
                print(colour(f"\n  ✅ STRONG candles win {s_wr-m_wr:.1f}% more than MEDIUM", "green"))
            else:
                print(colour(f"\n  ⏳ STRONG vs MEDIUM edge not clear yet", "yellow"))

    # ── HOUR OF DAY ──────────────────────────────────────────────────────────
    section("WIN RATE BY HOUR (UTC)")
    hour_data = defaultdict(lambda: {"wins": 0, "total": 0})
    for w in all_traded:
        h = w["hour"]
        hour_data[h]["total"] += 1
        if w["result"] == "WIN":
            hour_data[h]["wins"] += 1

    print(f"  {'Hour':<8} {'Bar':<45} {'Note'}")
    print(f"  {'-'*8} {'-'*45} {'-'*10}")
    for h in sorted(hour_data):
        d   = hour_data[h]
        pct = d["wins"] / d["total"] * 100 if d["total"] else 0
        note = ""
        if pct >= 80:  note = colour("🟢 BEST", "green")
        elif pct < 45: note = colour("🔴 AVOID", "red")
        elif pct < 55: note = colour("🟡 MARGINAL", "yellow")
        print(f"  {h:02d}:00    {bar(d['wins'], d['total'])}  {note}")

    # ── ENTRY TIMING ─────────────────────────────────────────────────────────
    section("WIN RATE BY ENTRY TIMING (mins left when signal triggered)")
    timing_windows = [w for w in all_traded if w.get("entry_time_remaining") is not None]
    if not timing_windows:
        print(colour("  ⏳ No entry_time_remaining data yet", "yellow"))
    else:
        timing_buckets = [
            ("8–11 mins",  480, 660),
            ("5–8 mins",   300, 480),
            ("3–5 mins",   180, 300),
            ("1–3 mins",   60,  180),
            ("<1 min",     0,   60),
        ]
        for label, lo, hi in timing_buckets:
            group = [w for w in timing_windows if lo <= w["entry_time_remaining"] < hi]
            w     = [x for x in group if x["result"] == "WIN"]
            note  = "  ← Abel's sweet spot" if label == "5–8 mins" else ""
            if group:
                print(f"  {label:<12} {bar(len(w), len(group))}{colour(note, 'cyan')}")
            else:
                print(f"  {label:<12} {colour('no trades yet', 'yellow')}")

    # ── DISTANCE AT ENTRY ────────────────────────────────────────────────────
    section("WIN RATE BY ENTRY DISTANCE FROM PTB")
    buckets = {
        "$10–30":  {"min": 10,  "max": 30,  "w": 0, "t": 0},
        "$30–60":  {"min": 30,  "max": 60,  "w": 0, "t": 0},
        "$60–100": {"min": 60,  "max": 100, "w": 0, "t": 0},
        "$100+":   {"min": 100, "max": 9e9, "w": 0, "t": 0},
    }
    for w in all_traded:
        if w.get("signal_price") and w.get("ptb"):
            dist = abs(w["signal_price"] - w["ptb"])
            for k, b_ in buckets.items():
                if b_["min"] <= dist < b_["max"]:
                    b_["t"] += 1
                    if w["result"] == "WIN":
                        b_["w"] += 1
                    break
    for label, b_ in buckets.items():
        print(f"  {label:<10} {bar(b_['w'], b_['t'])}")

    # ── POWER TRADE ───────────────────────────────────────────────────────────
    section("POWER TRADE PERFORMANCE")
    power_windows  = [w for w in all_traded if w.get("power_trade") is True]
    normal_windows = [w for w in all_traded if w.get("power_trade") is False
                      or (w.get("candle_type") is not None and w.get("power_trade") is None)]
    if not power_windows:
        print(colour("  ⏳ No power trade data yet", "yellow"))
    else:
        pw = [w for w in power_windows  if w["result"] == "WIN"]
        nw = [w for w in normal_windows if w["result"] == "WIN"]
        print(f"  {'Power trades':<20} {bar(len(pw), len(power_windows))}")
        print(f"  {'Normal trades':<20} {bar(len(nw), len(normal_windows))}")
        if power_windows and normal_windows:
            p_wr = len(pw) / len(power_windows)  * 100
            n_wr = len(nw) / len(normal_windows) * 100
            if p_wr > n_wr + 5:
                print(colour(f"\n  ✅ Power trade rule is working — {p_wr-n_wr:.1f}% higher win rate", "green"))
            elif p_wr < n_wr - 5:
                print(colour(f"\n  ⚠️  Power trades underperforming by {n_wr-p_wr:.1f}%", "yellow"))
            else:
                print(colour(f"\n  ⏳ Power trade edge not yet clear", "yellow"))

    # ── BANKROLL CURVE ───────────────────────────────────────────────────────
    section("BANKROLL CURVE (last 30 traded windows)")
    running = BANKROLL_START
    curve   = []
    for w in data:
        running += w.get("pnl", 0)
        curve.append(running)

    recent_curve = curve[-30:]
    if recent_curve:
        lo  = min(recent_curve)
        hi  = max(recent_curve)
        rng = hi - lo if hi != lo else 1
        rows = 6
        print()
        for row in range(rows, -1, -1):
            threshold = lo + (row / rows) * rng
            line = f"  ${threshold:>7.0f} |"
            for val in recent_curve:
                line += "█" if val >= threshold else " "
            print(line)
        print(f"  {'':>9}+" + "─" * len(recent_curve))
        print(f"  {'':>9} Last {len(recent_curve)} windows →")

    # ── STREAK ANALYSIS ──────────────────────────────────────────────────────
    section("STREAK ANALYSIS")
    max_consec_loss = 0
    cur_loss = 0
    max_consec_win = 0
    cur_win = 0
    for w in all_traded:
        if w["result"] == "LOSS":
            cur_loss += 1
            cur_win  = 0
            max_consec_loss = max(max_consec_loss, cur_loss)
        else:
            cur_win  += 1
            cur_loss  = 0
            max_consec_win = max(max_consec_win, cur_win)

    cur_streak_type = all_traded[-1]["result"] if all_traded else "N/A"
    cur_streak = 0
    for w in reversed(all_traded):
        if w["result"] == cur_streak_type:
            cur_streak += 1
        else:
            break

    print(f"  Max consecutive wins   : {colour(str(max_consec_win), 'green')}")
    print(f"  Max consecutive losses : {colour(str(max_consec_loss), 'red')}")
    print(f"  Current streak         : {colour(f'{cur_streak}x {cur_streak_type}', 'green' if cur_streak_type == 'WIN' else 'red')}")

    # ── KELLY CRITERION ──────────────────────────────────────────────────────
    section("KELLY CRITERION & BET SIZING")
    win_rate         = len([w for w in all_traded if w["result"] == "WIN"]) / len(all_traded) * 100
    full_k, half_k   = kelly(win_rate)
    current_bankroll = data[-1]["bankroll"] if data else BANKROLL_START

    print(f"  Win rate used          : {win_rate:.1f}%")
    print(f"  Current bankroll       : ${current_bankroll:.2f}")
    print()
    print(f"  Full Kelly bet         : {full_k*100:.1f}% of bankroll = ${current_bankroll * full_k:.2f}")
    print(f"  Half Kelly bet         : {half_k*100:.1f}% of bankroll = ${current_bankroll * half_k:.2f}")
    print(f"  Current flat bet       : ${BET_SIZE:.2f} ({BET_SIZE/current_bankroll*100:.2f}% of bankroll)")
    print()
    if win_rate >= 65:
        print(colour("  ✅ Win rate healthy enough to consider Kelly sizing.", "green"))
        if live_traded and len(live_traded) < 50:
            print(colour("  ⚠️  Stay flat $5 — still in Phase 1 live validation.", "yellow"))
        elif live_traded and len([w for w in live_traded if w["result"] == "WIN"]) / len(live_traded) * 100 >= 65:
            print(colour("  ✅ Phase 1 complete — ready to increase to $10 flat.", "green"))
    else:
        print(colour("  ⚠️  Win rate below 65% — stay flat for now.", "yellow"))

    # ── ML MODEL PERFORMANCE ─────────────────────────────────────────────────
    section("ML MODEL PERFORMANCE")
    ml_windows = [w for w in all_traded if w.get("model_prob") is not None]

    if not ml_windows:
        print(colour("  ⏳ No model_prob data yet.", "yellow"))
    else:
        ml_wins = [w for w in ml_windows if w["result"] == "WIN"]
        ml_wr   = len(ml_wins) / len(ml_windows) * 100

        allowed          = [w for w in ml_windows if w["model_prob"] >= 0.55]
        blocked_would_be = [w for w in ml_windows if w["model_prob"] < 0.55]
        allowed_wr       = len([w for w in allowed if w["result"] == "WIN"]) / len(allowed) * 100 if allowed else 0
        blocked_wr       = len([w for w in blocked_would_be if w["result"] == "WIN"]) / len(blocked_would_be) * 100 if blocked_would_be else 0

        print(f"  Windows with model data : {len(ml_windows)}")
        print(f"  Win rate (model active) : {colour(f'{ml_wr:.1f}%', 'green' if ml_wr >= 65 else 'yellow')}")
        print()
        print(f"  Trades model ALLOWED  (prob ≥ 55%) : {bar(len([w for w in allowed if w['result']=='WIN']), len(allowed))}")
        if blocked_would_be:
            bw = len([w for w in blocked_would_be if w["result"] == "WIN"])
            print(f"  Trades model BLOCKED  (prob < 55%) : {bar(bw, len(blocked_would_be))}")
            if allowed_wr > blocked_wr:
                print(colour(f"\n  ✅ Filter working — allowed trades win {allowed_wr-blocked_wr:.1f}% more", "green"))
            else:
                print(colour(f"\n  ⚠️  Filter may not be helping yet", "yellow"))
        else:
            print("     (No blocked trade outcomes to compare yet)")

        print(f"\n  {'Prob range':<14} {'Performance'}")
        print(f"  {'-'*14} {'-'*45}")
        for label, lo, hi in [("55–65%",0.55,0.65),("65–70%",0.65,0.70),("70–75%",0.70,0.75),("75–80%",0.75,0.80),("80%+",0.80,1.01)]:
            group = [w for w in ml_windows if lo <= w["model_prob"] < hi]
            w_    = [x for x in group if x["result"] == "WIN"]
            if group:
                print(f"  {label:<14} {bar(len(w_), len(group))}")
            else:
                print(f"  {label:<14} {colour('no trades in this range yet', 'yellow')}")

    # ── PRE-ENTRY DATA ────────────────────────────────────────────────────────
    pre_windows = [w for w in all_traded if w.get("pre_entry_move") is not None]
    if len(pre_windows) >= 20:
        section("PRE-ENTRY DATA (Spike & Volatility Filter Preview)")
        import statistics
        moves  = [abs(w["pre_entry_move"]) for w in pre_windows]
        ranges = [w["pre_entry_range"]     for w in pre_windows if w.get("pre_entry_range")]

        print(f"  Windows with pre-entry data : {len(pre_windows)}")
        print(f"  Avg abs move (2min before)  : ${statistics.mean(moves):.2f}")
        print(f"  Max abs move seen           : ${max(moves):.2f}")
        if ranges:
            print(f"  Avg range (2min before)     : ${statistics.mean(ranges):.2f}")
            print(f"  Max range seen              : ${max(ranges):.2f}")

        high_move_thresh = statistics.mean(moves) + statistics.stdev(moves)
        high_move = [w for w in pre_windows if abs(w["pre_entry_move"]) > high_move_thresh]
        low_move  = [w for w in pre_windows if abs(w["pre_entry_move"]) <= high_move_thresh]
        if high_move and low_move:
            hm_wr = len([w for w in high_move if w["result"] == "WIN"]) / len(high_move) * 100
            lm_wr = len([w for w in low_move  if w["result"] == "WIN"]) / len(low_move)  * 100
            print(f"\n  High pre-entry move (>${high_move_thresh:.0f}) : {bar(len([w for w in high_move if w['result']=='WIN']), len(high_move))}")
            print(f"  Low  pre-entry move (<=${high_move_thresh:.0f}) : {bar(len([w for w in low_move  if w['result']=='WIN']), len(low_move))}")
            if lm_wr > hm_wr + 5:
                print(colour(f"\n  ✅ Spike filter will help — calm windows win {lm_wr-hm_wr:.1f}% more", "green"))
                print(colour(f"     Suggested threshold: ${high_move_thresh:.0f}", "cyan"))
            else:
                print(colour(f"\n  ⏳ No clear spike pattern yet — keep collecting data", "yellow"))

    # ── RECOMMENDATIONS ───────────────────────────────────────────────────────
    section("RECOMMENDATIONS")
    worst_hours = [h for h, d in hour_data.items() if d["total"] >= 4 and d["wins"]/d["total"] < 0.50]
    best_hours  = [h for h, d in hour_data.items() if d["total"] >= 4 and d["wins"]/d["total"] >= 0.80]

    recs = []

    # Phase-aware stake recommendation
    if live_traded:
        live_wr = len([w for w in live_traded if w["result"] == "WIN"]) / len(live_traded) * 100
        if len(live_traded) < 50:
            recs.append(colour(f"📍 PHASE 1: Keep flat $5 — {50-len(live_traded)} more live windows needed", "cyan"))
        elif live_wr >= 65:
            recs.append(colour(f"✅ PHASE 1 DONE: Increase to flat $10", "green"))
        else:
            recs.append(colour(f"⚠️  Live win rate {live_wr:.1f}% — stay at $5, diagnose first", "red"))
    else:
        recs.append(f"Keep flat ${BET_SIZE} bets until live with 50+ real windows")

    if worst_hours:
        hrs = ", ".join([f"{h:02d}:00" for h in sorted(worst_hours)])
        recs.append(f"Skip these UTC hours: {colour(hrs, 'red')}")
    if best_hours:
        hrs = ", ".join([f"{h:02d}:00" for h in sorted(best_hours)])
        recs.append(f"Prioritise these UTC hours: {colour(hrs, 'green')}")

    for i, rec in enumerate(recs, 1):
        print(f"  {i}. {rec}")

    print("\n" + colour("═" * 55, "cyan") + "\n")

if __name__ == "__main__":
    main()