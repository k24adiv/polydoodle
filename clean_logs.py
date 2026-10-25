import json
import os
import time

def clean_ptb_log(input_file="ptb_log_backup.json", output_file="ptb_log.json"):
    print("🧹 Starting targeted log sanitization and rescue...")
    
    if not os.path.exists(input_file):
        print(f"❌ Error: '{input_file}' not found.")
        print("Please make sure your backup is named exactly 'ptb_log_backup.json'.")
        return

    # Load the messy backup data
    with open(input_file, 'r') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            print("❌ Error: JSON is malformed. Check your backup file for missing brackets.")
            return

    cleaned_data = []
    stats = {"paper": 0, "skips": 0, "errors_and_old": 0, "rescued_fok": 0}

    # Calculate the timestamp for 48 hours ago
    # (This ensures we only grab yesterday and today's FOK trades)
    cutoff_timestamp = time.time() - (48 * 60 * 60)

    for entry in data:
        mode = entry.get("mode", "")
        result = entry.get("result", "")
        order_type = entry.get("order_type", "")
        timestamp = entry.get("timestamp", 0)

        # ── THE RESCUE OVERRIDE ───────────────────────────────────────
        # If it is a LIVE trade (WIN or LOSS), happened in the last 48 hours, 
        # and is NOT an old GTC order, we rescue it immediately!
        if mode == "LIVE" and result in ["WIN", "LOSS"]:
            if order_type != "GTC" and timestamp > cutoff_timestamp:
                cleaned_data.append(entry)
                stats["rescued_fok"] += 1
                continue  # Successfully rescued, skip the purge rules below

        # ── THE PURGE RULES (For everything else) ─────────────────────
        if mode == "PAPER":
            stats["paper"] += 1
            continue

        if result == "SKIP":
            stats["skips"] += 1
            continue

        # If it didn't get rescued above, and it's not a paper trade or skip,
        # it is either an old GTC trade, an error, or older than 48 hours.
        stats["errors_and_old"] += 1

    # Save the pristine, rescued data to the active bot file
    with open(output_file, 'w') as f:
        json.dump(cleaned_data, f, indent=4)

    print("\n✅ Sanitization & Rescue Complete!")
    print(f"🗑️  Purged Paper Trades: {stats['paper']}")
    print(f"🗑️  Purged Skips:        {stats['skips']}")
    print(f"🗑️  Purged Errors/Old:   {stats['errors_and_old']}")
    print(f"✨  RESCUED FOK TRADES:  {stats['rescued_fok']}")
    print(f"\n📂 Active AI dataset updated: {output_file}")

if __name__ == "__main__":
    clean_ptb_log()