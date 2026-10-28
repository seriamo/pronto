#!/bin/bash
# Auto-update script for Raspberry Pi desk display
# Runs weekly (Saturday 3am) via cron to install OS updates and reboot
# Logs results to update_log.json next to this script (shown at /api/update-log)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/update_log.json"
TEMP_LOG=$(mktemp)

echo "🔄 Starting auto-update at $(date -Iseconds)"

# Capture update details
STARTED_AT=$(date -Iseconds)
PACKAGES_BEFORE=$(dpkg -l | grep '^ii' | wc -l)

# Update package lists
apt-get update -qq > "$TEMP_LOG" 2>&1
UPDATE_EXIT=$?

# Get list of upgradable packages
UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -v "^Listing" | head -50)
UPGRADE_COUNT=$(echo "$UPGRADABLE" | grep -c '/' 2>/dev/null || echo "0")

# Perform upgrade
apt-get upgrade -y -qq >> "$TEMP_LOG" 2>&1
UPGRADE_EXIT=$?

# Autoremove old packages
apt-get autoremove -y -qq >> "$TEMP_LOG" 2>&1

PACKAGES_AFTER=$(dpkg -l | grep '^ii' | wc -l)
FINISHED_AT=$(date -Iseconds)

# Check if reboot is required
REBOOT_REQUIRED=false
if [ -f /var/run/reboot-required ]; then
    REBOOT_REQUIRED=true
fi

# Build JSON log entry (values passed via env, not interpolated into code — this runs as root)
STARTED_AT="$STARTED_AT" FINISHED_AT="$FINISHED_AT" UPGRADE_COUNT="$UPGRADE_COUNT" \
PACKAGES_BEFORE="$PACKAGES_BEFORE" PACKAGES_AFTER="$PACKAGES_AFTER" \
UPDATE_EXIT="$UPDATE_EXIT" UPGRADE_EXIT="$UPGRADE_EXIT" REBOOT_REQUIRED="$REBOOT_REQUIRED" \
UPGRADABLE="$UPGRADABLE" LOG_FILE="$LOG_FILE" python3 -c "
import json, os

env = os.environ
upgradable = env['UPGRADABLE'].strip()
entry = {
    'started_at': env['STARTED_AT'],
    'finished_at': env['FINISHED_AT'],
    'packages_upgraded': int((env['UPGRADE_COUNT'].split() or ['0'])[0]),
    'packages_before': int(env['PACKAGES_BEFORE']),
    'packages_after': int(env['PACKAGES_AFTER']),
    'update_exit_code': int(env['UPDATE_EXIT']),
    'upgrade_exit_code': int(env['UPGRADE_EXIT']),
    'reboot_required': env['REBOOT_REQUIRED'] == 'true',
    'upgradable_list': upgradable.split('\n') if upgradable else [],
}

log_file = env['LOG_FILE']
log = []
if os.path.exists(log_file):
    try:
        with open(log_file, 'r') as f:
            log = json.load(f)
    except Exception:
        pass

# Keep last 52 entries (1 year of weekly updates)
log.append(entry)
log = log[-52:]

with open(log_file, 'w') as f:
    json.dump(log, f, indent=2)

print(f'✅ Update complete: {entry[\"packages_upgraded\"]} packages upgraded, reboot={entry[\"reboot_required\"]}')
"

rm -f "$TEMP_LOG"

# Reboot if required
if [ "$REBOOT_REQUIRED" = true ]; then
    echo "🔁 Reboot required, rebooting in 10 seconds..."
    sleep 10
    reboot
else
    # Restart desk-display service to pick up any Python/lib changes
    systemctl restart desk-display.service
    echo "♻️ Restarted desk-display.service (no reboot needed)"
fi
