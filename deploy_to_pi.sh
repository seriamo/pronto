#!/bin/bash
# Quick deployment script for Raspberry Pi
# Usage: ./deploy_to_pi.sh pi@raspberrypi.local

if [ -z "$1" ]; then
    echo "Usage: ./deploy_to_pi.sh pi@hostname"
    echo "Example: ./deploy_to_pi.sh pi@raspberrypi.local"
    exit 1
fi

PI_HOST=$1
REMOTE_DIR="~/desk_display"

echo "🚀 Deploying Pronto to $PI_HOST"
echo ""

# Files to transfer (exclude .git, cache, venv, etc.)
echo "📦 Transferring files..."
rsync -avz --exclude='.git' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='calendar_cache.ics' \
    ./ "$PI_HOST:$REMOTE_DIR/"

if [ $? -ne 0 ]; then
    echo "❌ Transfer failed. Check your connection to $PI_HOST"
    exit 1
fi

echo "✅ Files transferred successfully"
echo ""

# Install system dependencies (fonts for display.html: Arial and emoji support)
# Note: DejaVu Sans is already included in base Raspberry Pi OS
echo "📦 Installing system dependencies (fonts)..."
ssh "$PI_HOST" << 'ENDSSH'
# Check and install required fonts
FONTS_NEEDED=false
if ! dpkg -l | grep -q ttf-mscorefonts-installer; then
    FONTS_NEEDED=true
fi
if ! dpkg -l | grep -q fonts-noto-color-emoji; then
    FONTS_NEEDED=true
fi

if [ "$FONTS_NEEDED" = true ]; then
    echo "Installing Arial (via MS Core Fonts) and Noto Color Emoji..."
    echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true | sudo debconf-set-selections
    sudo apt-get update -qq
    sudo apt-get install -y ttf-mscorefonts-installer fonts-noto-color-emoji > /dev/null 2>&1
    sudo fc-cache -f
    echo "✅ Fonts installed (Arial + Noto Color Emoji)"
else
    echo "✅ Required fonts already installed"
fi
ENDSSH

if [ $? -ne 0 ]; then
    echo "⚠️ Warning: Failed to install system dependencies. Continuing anyway..."
fi

echo ""

# Install/update Python dependencies in venv
echo "📚 Installing Python dependencies on Pi..."
ssh "$PI_HOST" << 'ENDSSH'
cd ~/desk_display
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "✅ Dependencies installed"
ENDSSH

if [ $? -ne 0 ]; then
    echo "⚠️ Warning: Failed to install dependencies. You may need to SSH in and run manually."
else
    echo "✅ Dependencies updated"
fi

echo ""
echo "🔄 Restarting Flask service..."
ssh "$PI_HOST" "sudo systemctl restart desk-display"

if [ $? -ne 0 ]; then
    echo "⚠️ Warning: Failed to restart service. You may need to restart manually."
else
    echo "✅ Service restarted"
fi

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Useful commands:"
echo "   Check service status:    ssh $PI_HOST 'sudo systemctl status desk-display'"
echo "   View live logs:          ssh $PI_HOST 'sudo journalctl -u desk-display -f'"
echo "   Test API:                ssh $PI_HOST 'curl -s localhost:5001/api/status'"
echo ""
echo "🔄 Quick restart (no reboot needed):"
echo "   Restart Flask + Browser: ssh $PI_HOST 'sudo systemctl restart desk-display && pkill chromium && sleep 2 && DISPLAY=:0 chromium --kiosk --noerrdialogs --disable-infobars --password-store=basic --window-size=800,480 --window-position=0,0 http://localhost:5001 > /dev/null 2>&1 &'"
echo ""
