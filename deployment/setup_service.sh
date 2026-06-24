#!/bin/bash

# Setup systemd service for Reddit Stealth Scraper on Linux VPS.
# Run this script on your VPS as root or with sudo.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo -e "${GREEN}==> Setting up Reddit Stealth Scraper service...${NC}"

# Ensure running on Linux
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    echo -e "${RED}Error: This script is intended to be run on the Linux VPS, not on local Windows.${NC}"
    exit 1
fi

# Ensure running as root/sudo
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: Please run this script as root or with sudo:${NC}"
    echo "  sudo bash deployment/setup_service.sh"
    exit 1
fi

SERVICE_NAME="reddit_scraper.service"
MCP_SERVICE_NAME="reddit_scraper_mcp.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
MCP_SERVICE_PATH="/etc/systemd/system/${MCP_SERVICE_NAME}"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(dirname "${DEPLOY_DIR}")"

# 0. Install Xvfb (virtual X display) so the relogin browser can run HEADFUL on
#    a headless server. The service wraps uvicorn in `xvfb-run`, which needs the
#    xvfb + xauth packages. Idempotent — skips if already present.
if ! command -v xvfb-run >/dev/null 2>&1; then
    echo -e "${GREEN}==> Installing Xvfb (needed for headful browser on server)...${NC}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y && apt-get install -y xvfb xauth
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y xorg-x11-server-Xvfb xorg-x11-xauth
    elif command -v yum >/dev/null 2>&1; then
        yum install -y xorg-x11-server-Xvfb xorg-x11-xauth
    else
        echo -e "${RED}Could not find apt-get/dnf/yum. Install 'xvfb' + 'xauth' manually.${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}==> Xvfb already installed.${NC}"
fi

# 0b. Ensure Python deps (incl. fastmcp for the MCP server) are installed in the
#     venv the units use. Idempotent — pip skips what's already present.
PY=""
if [ -f "${PROJECT_ROOT}/venv/bin/python" ]; then PY="${PROJECT_ROOT}/venv/bin/python";
elif [ -f "${PROJECT_ROOT}/myenv312/bin/python" ]; then PY="${PROJECT_ROOT}/myenv312/bin/python"; fi
if [ -n "${PY}" ]; then
    echo -e "${GREEN}==> Installing/updating Python deps in venv (fastmcp, etc.)...${NC}"
    "${PY}" -m pip install -q -r "${PROJECT_ROOT}/requirements.txt" || \
        echo -e "${RED}pip install failed — install requirements.txt manually before starting.${NC}"
else
    echo -e "${RED}No venv found (venv/ or myenv312/). Create one and pip install -r requirements.txt, or the MCP unit will fail on 'No module named fastmcp'.${NC}"
fi

# 1. Copy the service templates (API + MCP)
echo -e "${GREEN}==> Copying service configuration to ${SERVICE_PATH}...${NC}"
cp "${DEPLOY_DIR}/${SERVICE_NAME}" "${SERVICE_PATH}"
echo -e "${GREEN}==> Copying MCP service configuration to ${MCP_SERVICE_PATH}...${NC}"
cp "${DEPLOY_DIR}/${MCP_SERVICE_NAME}" "${MCP_SERVICE_PATH}"

# Update WorkingDirectory in case project root is not default /root/Scraper
if [ "${PROJECT_ROOT}" != "/root/Scraper" ]; then
    echo -e "${GREEN}==> Updating WorkDirectory in service files to ${PROJECT_ROOT}...${NC}"
    sed -i "s|WorkingDirectory=/root/Scraper|WorkingDirectory=${PROJECT_ROOT}|g" "${SERVICE_PATH}"
    sed -i "s|WorkingDirectory=/root/Scraper|WorkingDirectory=${PROJECT_ROOT}|g" "${MCP_SERVICE_PATH}"
fi

# 2. Reload systemd to load the new service
echo -e "${GREEN}==> Reloading systemd daemon...${NC}"
systemctl daemon-reload

# 3. Enable the service to start on boot
echo -e "${GREEN}==> Enabling service ${SERVICE_NAME} to start on boot...${NC}"
systemctl enable "${SERVICE_NAME}"

# 4. Start the service
echo -e "${GREEN}==> Starting service ${SERVICE_NAME}...${NC}"
systemctl restart "${SERVICE_NAME}"

# 5. Check status
echo -e "${GREEN}==> Verifying service status...${NC}"
sleep 2
systemctl status "${SERVICE_NAME}" --no-pager || true

echo -e "\n${GREEN}============================================= ${NC}"
echo -e "${GREEN}Setup Completed Successfully! ${NC}"
echo -e "You can manage the service using:${NC}"
echo -e "  Start:   ${GREEN}sudo systemctl start ${SERVICE_NAME}${NC}"
echo -e "  Stop:    ${GREEN}sudo systemctl stop ${SERVICE_NAME}${NC}"
echo -e "  Restart: ${GREEN}sudo systemctl restart ${SERVICE_NAME}${NC}"
echo -e "  Status:  ${GREEN}sudo systemctl status ${SERVICE_NAME}${NC}"
echo -e "  Logs:    ${GREEN}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
echo -e "${GREEN}============================================= ${NC}"
