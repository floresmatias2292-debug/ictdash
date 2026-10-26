#!/usr/bin/env bash
# Start the dashboard at http://127.0.0.1:8050
cd "$(dirname "$0")"
exec .venv/bin/python -W ignore -m uvicorn backend.server:app --host 127.0.0.1 --port "${PORT:-8050}"
