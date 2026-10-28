# Deployment

How to run Pronto on a Raspberry Pi (tested on Pi 4/5, Raspberry Pi OS Bookworm) and reach it securely from anywhere.

## 1. First-time Pi setup

```bash
# From your computer
./deploy_to_pi.sh pi@raspberrypi.local

# On the Pi
ssh pi@raspberrypi.local
cd ~/desk_display
cp .env.example .env && nano .env   # set ICS_URL, TIMEZONE, USER_NAME…
./kiosk_setup.sh                    # venv + deps, systemd service, Chromium kiosk autostart
sudo reboot
```

After the reboot, Chromium opens `http://localhost:5001` full-screen at 800×480.

Personal images (your logo, QR code, easter-egg photos) go in `static/custom/` and `static/easter-eggs/` on the Pi. Both are gitignored.

## 2. Deploying updates

```bash
./deploy_to_pi.sh pi@raspberrypi.local   # rsync, pip install -r requirements.txt, restart service
```

To deploy manually instead:

```bash
rsync -avz --exclude='.git' --exclude='venv' --exclude='__pycache__' ./ pi@raspberrypi.local:~/desk_display/
ssh pi@raspberrypi.local 'cd ~/desk_display && venv/bin/pip install -r requirements.txt && sudo systemctl restart desk-display'
```

## 3. Remote access (Cloudflare Tunnel + Access)

The app listens on `127.0.0.1:5001` only. To use `/msg`, the admin pages or iOS Shortcuts from outside the Pi:

1. **Tunnel.** Install `cloudflared` on the Pi and route a hostname to the local app: `desk.example.com → http://localhost:5001`.
2. **Access application(s)**, in Zero Trust → Access → Applications → Self-hosted:
   - `desk.example.com`: allow your own email. Add a **Service Auth** policy for an iOS service token.
   - *(optional)* `desk.example.com/msg`: allow your coworkers, e.g. by email domain.
   - *(optional)* To show the display publicly, add a **Bypass** policy for `/`, `/api/status`, `/api/version`, `/api/message`, `/api/easter-eggs`, `/static` and `/qr.svg`, and set `PUBLIC_DISPLAY=true`.
3. **Tell the app to verify Access tokens.** Add to `.env`:

   ```bash
   PUBLIC_URL=https://desk.example.com
   CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
   CF_ACCESS_AUD=<AUD tag of app 1>,<AUD tag of app 2>   # Application → Overview → "Application Audience (AUD) Tag"
   OWNER_EMAILS=you@example.com
   OWNER_SERVICE_TOKENS=<service token Client ID>          # e.g. abc123.access
   ```

4. Restart the service with `sudo systemctl restart desk-display`.

Every non-local request must carry a valid Access JWT. The app checks the signature against your team's public keys, plus the audience, issuer and expiry. Without `CF_ACCESS_*` set, remote requests get `403`.

**LAN or Tailscale access without Cloudflare (optional):** set `HOST=0.0.0.0` and `TRUSTED_NETWORKS=192.168.1.0/24` (or `100.64.0.0/10` for Tailscale). Browse by IP address, not hostname. Anyone on those networks gets owner access.

## 4. Weekly OS updates (optional)

`auto_update.sh` runs `apt upgrade`, logs to `update_log.json` (visible at `/api/update-log`), and reboots if needed:

```bash
sudo crontab -e
0 3 * * 6 /home/pi/desk_display/auto_update.sh >> /var/log/desk-display-update.log 2>&1
```

## Everyday commands

```bash
sudo systemctl status desk-display        # service status
sudo journalctl -u desk-display -f        # live logs
sudo systemctl restart desk-display       # restart the app
curl -s localhost:5001/api/status | jq    # current status (run on the Pi)
sudo systemctl status cloudflared         # tunnel status

# Restart the Chromium kiosk over SSH (Wayland)
XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 \
  nohup chromium --ozone-platform=wayland --noerrdialogs --disable-infobars \
  --password-store=basic --kiosk http://localhost:5001 \
  --window-size=800,480 --window-position=0,0 >/dev/null 2>&1 &
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Service won't start | `sudo journalctl -u desk-display -n 50 --no-pager`; run `venv/bin/python app.py` by hand |
| `403` through the tunnel | `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD` match the Access app; your email or token is in `OWNER_*`; check the logs for `🔒 Rejected Cloudflare Access token` |
| Calendar not updating | `/admin` → health panel shows the last fetch error; refresh with `/api/refresh` |
| Recurring events missing | `journalctl -u desk-display \| grep RRULE`; `grep -c RRULE: calendar_caches/*.ics` |
| Wrong times | `TIMEZONE` in `.env`; `grep "DTSTART;TZID" calendar_caches/*.ics \| head` |
| Port already in use | `sudo ss -tulpn \| grep 5001` |
