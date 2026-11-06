#!/usr/bin/env bash
# Reset the demo database, then start the server. Open http://127.0.0.1:8000
set -e
cd "$(dirname "$0")"
python seed.py
exec python -m uvicorn app.main:app --port "${PORT:-8000}"
