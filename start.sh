#!/usr/bin/env bash
# Start StockDeck (used by Replit and handy locally).
set -e
cd "$(dirname "$0")"
pip install -q -r requirements.txt
exec python -m uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
