#!/bin/sh
cd /app
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "${IPTV_PORT:-8300}"
