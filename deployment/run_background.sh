#!/bin/bash

# Run Reddit Stealth Scraper FastAPI Gateway in background using nohup.
# Useful if you don't have root/sudo privileges to register a systemd service.

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(dirname "${DEPLOY_DIR}")"
PID_FILE="${PROJECT_ROOT}/logs/api_server.pid"
LOG_FILE="${PROJECT_ROOT}/logs/api_server_background.log"

cd "${PROJECT_ROOT}"

# Ensure logs directory exists
mkdir -p logs

# Check if already running
if [ -f "${PID_FILE}" ]; then
    PID=$(cat "${PID_FILE}")
    if ps -p $PID > /dev/null; then
        echo -e "${RED}Warning: API server is already running in background with PID $PID.${NC}"
        echo "If you want to restart it, run: bash deployment/stop_background.sh first."
        exit 0
    else
        # Stale PID file
        rm -f "${PID_FILE}"
    fi
fi

# Detect Python interpreter
PYTHON_CMD="python3"
if [ -f "venv/bin/python" ]; then
    PYTHON_CMD="venv/bin/python"
elif [ -f "myenv312/bin/python" ]; then
    PYTHON_CMD="myenv312/bin/python"
fi

echo -e "${GREEN}==> Starting API server in background using ${PYTHON_CMD}...${NC}"

# Run HEADFUL relogin on a headless server: the LinkedIn relogin browser needs an
# X display. If xvfb-run is available, launch the whole app under a virtual
# display so headful Chromium has somewhere to render. Falls back to a direct
# launch (works on a desktop with a real display) if xvfb-run is missing.
export LINKEDIN_RELOGIN_HEADLESS=${LINKEDIN_RELOGIN_HEADLESS:-false}
if command -v xvfb-run >/dev/null 2>&1; then
    echo -e "${GREEN}==> Wrapping in xvfb-run (virtual display for headful browser)...${NC}"
    nohup xvfb-run -a --server-args="-screen 0 1920x1080x24 -ac -nolisten tcp" \
        ${PYTHON_CMD} -m uvicorn api.main:app --host 0.0.0.0 --port 18080 > "${LOG_FILE}" 2>&1 &
else
    echo -e "${RED}==> xvfb-run not found; launching directly. Install it for headful on a server: sudo apt-get install -y xvfb xauth${NC}"
    nohup ${PYTHON_CMD} -m uvicorn api.main:app --host 0.0.0.0 --port 18080 > "${LOG_FILE}" 2>&1 &
fi

# Save PID
PID=$!
echo $PID > "${PID_FILE}"

echo -e "${GREEN}==> Started API server successfully!${NC}"
echo -e "  PID:  ${GREEN}${PID}${NC}"
echo -e "  Logs: ${GREEN}tail -f logs/api_server_background.log${NC}"
echo -e "  Stop: ${GREEN}bash deployment/stop_background.sh${NC}"
