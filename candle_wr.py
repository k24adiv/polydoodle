import csv
from collections import defaultdict

rows = list(csv.DictReader(open("dry_run_data v3.csv")))
counts = defaultdict(lambda: {"W": 0, "L": 0})

for r in rows:
    outcome = r.get("target_outcome", "").upper()
    if outcome in ("WIN", "LOSS"):
        candle = r.get("candle_type", "UNKNOWN")
        counts[candle]["W" if outcome == "WIN" else "L"] += 1

print(f"\n{'Candle':<16} {'W':>4} {'L':>4} {'Total':>6} {'WR':>6}")
print("-" * 42)
for candle, v in sorted(counts.items()):
    t = v["W"] + v["L"]
    wr = v["W"] / t * 100
    flag = " ← BLOCKED" if candle == "BULL_MEDIUM" else ""
    print(f"{candle:<16} {v['W']:>4} {v['L']:>4} {t:>6} {wr:>5.0f}%{flag}")

total_w = sum(v["W"] for v in counts.values())
total_l = sum(v["L"] for v in counts.values())
total   = total_w + total_l
print("-" * 42)
print(f"{'ALL':<16} {total_w:>4} {total_l:>4} {total:>6} {total_w/total*100:>5.0f}%")
print()
