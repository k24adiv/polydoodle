"""
fix_swapped_columns.py
----------------------
Fixes a main.py bug where target_outcome and final_price were written
in swapped order, resulting in rows like:

  target_outcome = "1.450200"   (should be final_price)
  final_price    = "PENDING"    (should be target_outcome)

After correcting the swap, calls repair_csv.repair() to resolve any
remaining PENDING rows via Binance historical data.

Run:
    python fix_swapped_columns.py
    python fix_swapped_columns.py --csv path/to/other.csv
"""

import argparse
import csv
from pathlib import Path

from repair_csv import repair


def _looks_like_price(value: str) -> bool:
    """Return True if value is a float string (i.e. a price, not WIN/LOSS/PENDING)."""
    try:
        float(value.strip())
        return True
    except ValueError:
        return False


def fix_swapped(csv_path: str = "dry_run_data.csv") -> int:
    """
    Detect and fix rows where target_outcome contains a price and
    final_price contains 'PENDING' (columns were written in wrong order).

    Returns the number of rows corrected.
    """
    p = Path(csv_path)
    if not p.exists():
        print(f"ERROR: {csv_path} not found.")
        return 0

    with open(p, newline="") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)
        fields = list(reader.fieldnames or [])

    if "target_outcome" not in fields or "final_price" not in fields:
        print("ERROR: CSV missing expected columns.")
        return 0

    swapped = 0
    for row in rows:
        to_val = row.get("target_outcome", "").strip()
        fp_val = row.get("final_price",    "").strip()

        # Swapped: target_outcome holds a price, final_price holds "PENDING"
        if _looks_like_price(to_val) and fp_val.upper() == "PENDING":
            row["final_price"]    = to_val   # move price to the right column
            row["target_outcome"] = "PENDING" # restore PENDING so repair can find it
            swapped += 1
            print(f"  Fixed swap  {row['asset']} @ {str(row['timestamp'])[:19]}  "
                  f"final_price={to_val}")

    if swapped == 0:
        print("No swapped rows detected.")
    else:
        with open(p, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nCorrected {swapped} swapped row(s). Running repair...\n")

    return swapped


def main(csv_path: str = "dry_run_data.csv") -> None:
    print(f"=== Step 1: Fix swapped columns in {csv_path} ===\n")
    fixed = fix_swapped(csv_path)

    print(f"\n=== Step 2: Resolve PENDING outcomes via Binance ===\n")
    repair(csv_path, silent=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix column swap then resolve PENDING rows.")
    parser.add_argument("--csv", default="dry_run_data.csv")
    args = parser.parse_args()
    main(args.csv)
