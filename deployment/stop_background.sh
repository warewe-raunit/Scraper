#!/bin/bash

# Stop Reddit Stealth Scraper FastAPI Gateway running in background.

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(dirname "${DEPLOY_DIR}")"
PID_FILE="${PROJECT_ROOT}/logs/api_server.pid"

if [ ! -f "${PID_FILE}" ]; then
    echo -e "${RED}Error: PID file not found at ${PID_FILE}${NC}"
    echo "Is the server running in background?"
    exit 1
fi

PID=$(cat "${PID_FILE}")

if ps -p $PID > /dev/null; then
    echo -e "${GREEN}==> Stopping background API server with PID ${PID}...${NC}"
    kill $PID
    
    # Wait for up to 10 seconds for it to terminate
    for i in {1..10}; do
        if ps -p $PID > /dev/null; then
            sleep 1
        else
            break
        fi
    done
    
    # Force kill if still running
    if ps -p $PID > /dev/null; then
        echo -e "${RED}==> Process did not terminate. Force killing PID ${PID}...${NC}"
        kill -9 $PID
    fi
    
    echo -e "${GREEN}==> Stopped successfully.${NC}"
else
    echo -e "${RED}Warning: Process with PID ${PID} is not running.${NC}"
fi

rm -f "${PID_FILE}"
echo -e "${GREEN}==> Cleaned up PID file.${NC}"
