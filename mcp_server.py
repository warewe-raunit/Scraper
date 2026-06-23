"""
mcp_server.py — Model Context Protocol (stdio) front-end for the Scraper API.

Exposes every REST endpoint of the running FastAPI app (Reddit, YouTube, X,
LinkedIn) as MCP tools so Claude Code, Codex, and other MCP-capable agents can
call them directly. It is a THIN client: it mirrors the live OpenAPI spec, so
new endpoints become tools automatically with no code changes here.

Run:
    python mcp_server.py                      # talks to $SCRAPER_BASE_URL

Env:
    SCRAPER_BASE_URL  Base URL of the running API. Default http://127.0.0.1:8000
    API_KEY           Same key the API validates (sent as X-API-Key). Required
                      unless the server has no key configured.

Wire it into an agent — see mcp_server config snippets in API.md / README.
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv(override=True)

BASE_URL = (os.getenv("SCRAPER_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
API_KEY = (os.getenv("API_KEY") or "").strip()

# API key rides on every request as a header; the server also accepts ?api_key=
# but the header is what its docs prefer. openapi.json itself is unauthenticated.
headers = {"X-API-Key": API_KEY} if API_KEY else {}
client = httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=120.0)


def _build() -> FastMCP:
    try:
        spec = httpx.get(f"{BASE_URL}/openapi.json", timeout=15.0).json()
    except Exception as e:
        # The MCP host swallows stdout; emit the real reason to stderr so the
        # user sees "is the API running / is SCRAPER_BASE_URL right" instead of
        # a silent dead server.
        print(f"[mcp_server] cannot reach {BASE_URL}/openapi.json: {e}", file=sys.stderr)
        raise SystemExit(1)
    return FastMCP.from_openapi(openapi_spec=spec, client=client, name="scraper-api")


mcp = _build()

if __name__ == "__main__":
    mcp.run()  # stdio transport — what Claude Code / Codex spawn
