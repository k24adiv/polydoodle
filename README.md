# Polybot

Polybot is a Python-based trading automation project for multi-asset market-window analysis and execution on Polymarket. The code is organized as a set of operational scripts rather than a web application, and it includes data collection, signal generation, optional ML filtering, live execution, logging, stats, and redemption helpers.

## What this project does

From the inspected code, the workflow is:

1. Collect live market baseline data from Binance.
2. Measure price deviation against a PTB (price-to-beat) baseline.
3. Classify the current candle state and generate a directional signal.
4. Optionally apply an ML prediction check before allowing a trade.
5. Execute live Polymarket CLOB orders when conditions are met.
6. Log trades, track outcomes, and reconcile redeemed or pending positions.

The code is built around the following primary modules:

- `main.py` — orchestrates the main bot loop, timing, monitor logic, and trade attempts.
- `ptb_scanner.py` — scans asset prices, calculates PTB values, and returns confidence data.
- `signal_engine.py` — decides whether the current market condition warrants a trade.
- `live_executor.py` — performs live CLOB order execution and market validation.
- `predictor.py` — loads and applies an optional ML model.
- `logger.py` — creates structured logs and JSON trade records.
- `auto_redeem.py` — performs redemption and reconciliation tasks after market resolution.
- `notifier.py` — sends notifications through Discord and Telegram.
- `stats.py` and `stats_analyser.py` — summarize trading results and win-rate analysis.
- `train_model.py` — trains the predictive model from stored trade history.

## Repository layout

The top-level project contains both source modules and operational artifacts. The genuine source files to keep are the main Python modules such as:

- `main.py`
- `ptb_scanner.py`
- `signal_engine.py`
- `live_executor.py`
- `predictor.py`
- `logger.py`
- `stats.py`
- `notifier.py`
- `auto_redeem.py`
- `train_model.py`
- `asset_score.py`
- `outcome_tracker.py`
- `check_trades.py`
- `check_positions.py`
- `setup_allowances.py`
- `approve_v2.py`
- `wraptopusd.py`
- `manual_redeem.py`

There are also historical or duplicate files with names such as `main copy.py`, `live_executor copy.py`, and similar variants. These were not ignored by this setup and should be reviewed manually before deciding whether to keep, remove, or archive them.

## Local setup

This project is designed to run as a local Python script-based bot. The repository does not currently include a standard Python packaging file such as `pyproject.toml` or a generated dependency lock file; installation is expected to be done with a local virtual environment and a package list.

Suggested local workflow:

1. Create a virtual environment.
2. Install the project dependencies from `requirements.txt`.
3. Create a local `.env` file with the required credentials.
4. Run the bot from the repository root.

## Configuration and environment variables

The code reads runtime credentials from environment variables using `python-dotenv` and `os.getenv(...)`.

The inspected project references the following environment values:

- `DISCORD_WEBHOOK` — Discord alert webhook.
- `TELEGRAM_TOKEN` — Telegram bot token.
- `TELEGRAM_CHAT_ID` — Telegram chat identifier.
- `POLYGON_PRIVATE_KEY` — wallet private key for Polygon actions.
- `CLOB_API_KEY` — Polymarket CLOB API key.
- `CLOB_API_SECRET` — Polymarket CLOB API secret.
- `CLOB_API_PASSPHRASE` — Polymarket CLOB passphrase.
- `ANKR_KEY` — optional Polygon RPC key.
- `POLYGONSCAN_API_KEY` — optional Polygonscan key.
- `RPC_URL` — optional custom Polygon RPC endpoint.
- `LIVE_ENABLED` — toggles live execution mode.
- `LIVE_STAKE_USD` — trade stake in USD for live execution.
- `TARGET_LIMIT_PRICE` and `MAX_LIMIT_PRICE` — execution-price thresholds used by the live executor.
- `LIMIT_TIMEOUT_SECS` — order timeout window.
- `TRADING_MODE` — runtime mode used by analysis scripts (`paper` or `live`).

These values should be stored only in a local `.env` file and must never be committed to Git.

## Security notes

This project interacts with live market APIs, blockchain RPC endpoints, and wallet private keys. That creates real security and operational risk:

- private keys can grant access to wallet funds,
- API credentials can expose account access,
- market automation can trigger live transactions,
- log files may contain account activity, trade state, or operational history.

This repository is not a neutral demo or static analyzer; it is an operational trading bot. It should be treated as sensitive tooling that requires careful local isolation and deliberate secret management.

## Important runtime files to keep out of Git

The repository includes private or generated files that should be excluded from source control. Examples from the inspected project include:

- `.env`
- `.env1.txt`
- `ptb_log.json`
- `ptb_log_backup.json`
- `live_trades.log`
- `bot_audit.log`
- `server.log`
- `dry_run_data.csv`
- `dry_run_data v3.csv`
- `dry_run_data v4.csv`
- `dry_run_data v5.csv`
- `model.pkl.bak`
- `window_counter.txt`
- `venv/`
- `__pycache__/`

This repository also contains generated logs and local state artifacts that are useful for local debugging but are not suitable for public publication.

## Reliability and scope

This README is based strictly on the code present in the repository. It does not claim verified profitability, production readiness, or a proven win rate. The project is a local trading automation effort that relies on external APIs, live execution logic, and market assumptions.

## Disclaimer

Use at your own risk. This code interacts with live market data and blockchain-based trading infrastructure. There is no guarantee that any strategy or execution path will be profitable, stable, or safe under all conditions.
