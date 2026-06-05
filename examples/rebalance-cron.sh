#!/usr/bin/env bash
#
# Unattended daily rebalance via cron.
#
# Setup:
#   1. pip install msts-trader            (in a venv or system Python)
#   2. Put your credentials in ~/.msts-trader/creds.json  (chmod 600)
#      — see creds.example.json
#   3. Edit FEED_URL / BROKER below
#   4. chmod +x rebalance-cron.sh
#   5. Add to crontab (runs 15:50 ET on weekdays — adjust for your TZ):
#        50 15 * * 1-5  /path/to/rebalance-cron.sh >> ~/.msts-trader/cron.log 2>&1
#
# msts-trader refuses to trade outside US regular hours and only acts on
# tickers that drift past the threshold, so a daily run is safe.

set -euo pipefail

BROKER="${BROKER:-tastytrade}"
CREDS_FILE="${CREDS_FILE:-$HOME/.msts-trader/creds.json}"
# A reachable static CSV (ticker,weight). If you only have a "Copy CSV"
# button, save the CSV to a local file and use --csv-file below instead.
FEED_URL="${FEED_URL:-https://example.com/your-weights.csv}"
THRESHOLD="${THRESHOLD:-0.04}"

# Option A — pull the latest weights from a URL:
exec msts-trader rebalance \
  --broker "$BROKER" \
  --creds-file "$CREDS_FILE" \
  --csv-url "$FEED_URL" \
  --threshold "$THRESHOLD" \
  --yes

# Option B — use a local CSV you maintain (comment out Option A above):
# exec msts-trader rebalance \
#   --broker "$BROKER" \
#   --creds-file "$CREDS_FILE" \
#   --csv-file "$HOME/.msts-trader/targets.csv" \
#   --threshold "$THRESHOLD" \
#   --yes
