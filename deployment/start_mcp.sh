#!/bin/bash
# Foreground launcher for the HTTP MCP, called by reddit_scraper_mcp.service.
# Prefers an ISOLATED mcp-venv so installing fastmcp can't disturb the API's
# dependency tree (fastmcp pins starlette/anyio versions that conflict with it).
set -e
cd "$(dirname "$0")/.."   # project root

if [ -f mcp-venv/bin/python ]; then PY=mcp-venv/bin/python
elif [ -f venv/bin/python ]; then PY=venv/bin/python
elif [ -f myenv312/bin/python ]; then PY=myenv312/bin/python
else PY=python3; fi

exec "$PY" mcp_server.py
