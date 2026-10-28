#!/bin/bash
# Raspberry Pi Kiosk Setup Script for Pronto
# This script sets up Chromium to run in fullscreen kiosk mode

echo "🚀 Setting up Pronto Kiosk Mode on Raspberry Pi"
echo ""

# Install dependencies if needed
echo "📦 Installing dependencies..."
sudo apt-get update
# Chromium, cursor hiding, and fonts (Arial via MS Core Fonts + Noto Color Emoji)
# Note: DejaVu Sans is already included in base Raspberry Pi OS
sudo apt-get install -y chromium unclutter xdotool ttf-mscorefonts-installer fonts-noto-color-emoji

# Create autostart directory for labwc (Wayland - default on newer Pi OS)
mkdir -p ~/.config/labwc

# Create autostart file for labwc
echo "⚙️ Configuring autostart for labwc (Wayland)..."
cat > ~/.config/labwc/autostart << 'EOF'
# Disable screen blanking and power management
wlr-randr --output HDMI-A-1 --on &

# Hide mouse cursor when idle
unclutter -idle 0.5 &

# Start Chromium in kiosk mode (password-store=basic disables keyring prompts)
chromium --noerrdialogs --disable-infobars --password-store=basic --kiosk http://localhost:5001 --window-size=800,480 --window-position=0,0 &
EOF

chmod +x ~/.config/labwc/autostart
echo "✅ Autostart configured for labwc"

# Also create LXDE autostart for older Pi OS versions (X11)
mkdir -p ~/.config/lxsession/LXDE-pi
cat > ~/.config/lxsession/LXDE-pi/autostart << 'EOF'
@lxpanel --profile LXDE-pi
@pcmanfm --desktop --profile LXDE-pi
@xscreensaver -no-splash

# Disable screen blanking
@xset s off
@xset -dpms
@xset s noblank

# Hide mouse cursor when idle
@unclutter -idle 0.5 -root

# Start Chromium in kiosk mode (password-store=basic disables keyring prompts)
@chromium --noerrdialogs --disable-infobars --password-store=basic --kiosk http://localhost:5001 --window-size=800,480 --window-position=0,0
EOF

echo "✅ Autostart configured for LXDE (fallback)"
echo ""

# Create systemd service for Flask app
echo "🔧 Creating Flask service..."
sudo tee /etc/systemd/system/desk-display.service > /dev/null << EOF
[Unit]
Description=Pronto desk display (Flask)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
Environment="PATH=$(pwd)/venv/bin"
ExecStart=$(pwd)/venv/bin/python app.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "🐍 Creating Python virtual environment..."
    python3 -m venv venv
fi

# Install Python dependencies
echo "📚 Installing Python packages..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Enable and start the service
echo "🎯 Enabling Flask service..."
sudo systemctl daemon-reload
sudo systemctl enable desk-display.service
sudo systemctl start desk-display.service

echo ""
echo "✅ Setup complete!"
echo ""
echo "📋 Next steps:"
echo "   1. Make sure your .env file is configured (cp .env.example .env):"
echo "      - ICS_URL (required)"
echo "      - TIMEZONE, USER_NAME, ROOM_NUMBER (optional)"
echo "      - CF_ACCESS_* / OWNER_* for remote access (see docs/deployment.md)"
echo ""
echo "   2. Reboot your Raspberry Pi:"
echo "      sudo reboot"
echo ""
echo "   3. After reboot, Chromium will automatically open in kiosk mode"
echo ""
echo "📊 Useful commands:"
echo "   Check Flask app status:  sudo systemctl status desk-display"
echo "   View Flask logs:         sudo journalctl -u desk-display -f"
echo "   Restart Flask app:       sudo systemctl restart desk-display"
echo "   Test in browser:         http://localhost:5001"
echo ""
echo "ℹ️  This script configures both labwc (Wayland) and LXDE (X11) autostart"
echo "   Your Pi will use whichever desktop environment is active."
echo ""
