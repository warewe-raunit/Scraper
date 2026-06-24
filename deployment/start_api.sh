#!/bin/bash
# Foreground launcher for the API, called by reddit_scraper.service.
# Lives in a script (not inline ExecStart) so systemd never has to parse the
# shell quoting — the nested quotes in the worker-count grep broke when inlined.
set -e
cd "$(dirname "$0")/.."   # project root

if [ -f venv/bin/python ]; then PY=venv/bin/python
elif [ -f myenv312/bin/python ]; then PY=myenv312/bin/python
else PY=python3; fi

# Worker count from .env (the app's source of truth), falling back to env then 4.
W=$(grep -E '^API_WORKERS=' .env 2>/dev/null | tail -1 | cut -d= -f2 | cut -d'#' -f1 | tr -d ' "')
W=${W:-${API_WORKERS:-4}}

# HEADFUL relogin on a headless box needs a virtual display. Wrap in xvfb-run if
# present; otherwise launch directly (desktop with a real display).
if command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a --server-args="-screen 0 1920x1080x24 -ac -nolisten tcp" \
    "$PY" -m uvicorn api.main:app --host 0.0.0.0 --port 18080 --workers "$W"
else
  exec "$PY" -m uvicorn api.main:app --host 0.0.0.0 --port 18080 --workers "$W"
fi
