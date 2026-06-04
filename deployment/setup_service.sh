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
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(dirname "${DEPLOY_DIR}")"

# 1. Copy the service template
echo -e "${GREEN}==> Copying service configuration to ${SERVICE_PATH}...${NC}"
cp "${DEPLOY_DIR}/${SERVICE_NAME}" "${SERVICE_PATH}"

# Update WorkingDirectory in case project root is not default /root/Scraper
if [ "${PROJECT_ROOT}" != "/root/Scraper" ]; then
    echo -e "${GREEN}==> Updating WorkDirectory in service file to ${PROJECT_ROOT}...${NC}"
    sed -i "s|WorkingDirectory=/root/Scraper|WorkingDirectory=${PROJECT_ROOT}|g" "${SERVICE_PATH}"
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
