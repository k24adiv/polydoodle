"""
notifier.py — Multi-Asset Notification Engine
Supports Discord Webhooks and Telegram Bot API.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG FIXES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG 1 [CRITICAL] — Env vars read at module import time, before load_dotenv()
  Original code ran os.getenv() at the top level when the module was first
  imported. Since notifier is imported before load_dotenv() can fully
  propagate in some environments, WEBHOOK / TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
  were always None — so all notifications silently did nothing.
  FIX: read env vars inside each send function at call time, not at import.

BUG 2 [MEDIUM] — Discord silently skipped if "discord.com" not in URL
  The guard `if "discord.com" not in WEBHOOK` would silently swallow
  notifications if the URL was slightly malformed or a test URL was used.
  FIX: only check that the webhook string is non-empty. Let the request
  fail loudly with the actual error if the URL is wrong.

BUG 3 [LOW] — No diagnostic on silent failures
  When both Discord and Telegram were unconfigured, notify() printed to
  console but gave no indication that external notifications were skipped.
  FIX: added a one-time config check at startup via notify_startup_check().
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("notifier")


def _get_discord_webhook() -> str:
    """FIX (Bug 1): read at call time, not module import time."""
    return os.getenv("DISCORD_WEBHOOK", "").strip()


def _get_telegram_creds() -> tuple[str, str]:
    """FIX (Bug 1): read at call time, not module import time."""
    token   = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def send_discord(message: str) -> bool:
    """
    Send a message to Discord via webhook.
    Returns True on success, False on failure.
    FIX (Bug 1+2): webhook read at call time; guard is non-empty check only.
    """
    webhook = _get_discord_webhook()
    if not webhook:
        return False
    try:
        r = requests.post(webhook, json={"content": message}, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  ⚠️ [Discord Error] {e}")
        log.warning(f"Discord notification failed: {e}")
        return False


def send_telegram(message: str) -> bool:
    """
    Send a message via Telegram Bot API.
    Returns True on success, False on failure.
    FIX (Bug 1): credentials read at call time.
    """
    token, chat_id = _get_telegram_creds()
    if not token or not chat_id:
        return False
    try:
        url     = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  ⚠️ [Telegram Error] {e}")
        log.warning(f"Telegram notification failed: {e}")
        return False


def notify(message: str, asset: str = None, prefix: str = "🔍") -> None:
    """
    Central notification hub. Always prints to console.
    Sends to Discord and/or Telegram if configured.

    Usage: notify("Trade Executed", asset="BTC", prefix="🚀")
    """
    asset_tag    = f"[{asset}] " if asset else ""
    full_message = f"{prefix} {asset_tag}{message}"

    print(f"\n> NOTIFY: {full_message}")

    discord_ok  = send_discord(full_message)
    telegram_ok = send_telegram(full_message)

    if not discord_ok and not telegram_ok:
        log.debug("No external notification channels configured or both failed.")


def notify_error(error_msg: str, asset: str = None) -> None:
    """Specific helper for urgent errors."""
    notify(error_msg, asset=asset, prefix="❌ ALERT")


def notify_startup_check() -> None:
    """
    FIX (Bug 3): Call this once at bot startup to show which notification
    channels are active. Helps immediately identify misconfiguration.
    """
    webhook         = _get_discord_webhook()
    token, chat_id  = _get_telegram_creds()

    discord_status  = "✅ configured" if webhook  else "❌ not set (DISCORD_WEBHOOK missing)"
    telegram_status = "✅ configured" if (token and chat_id) else "❌ not set (TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing)"

    print(f"\n  📣 Notification Channels:")
    print(f"     Discord  : {discord_status}")
    print(f"     Telegram : {telegram_status}")

    if not webhook and not (token and chat_id):
        print("     ⚠️  No external notifications — console only.\n")
    else:
        print()