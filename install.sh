#!/usr/bin/env bash
# Install the Herdr rich-sidebar config and the session-usage reporter.
#
# Usage:
#   ./install.sh                     copy the config files only
#   ./install.sh claude copilot      also install those Herdr integrations
#
# Idempotent. Existing config files are backed up with a date suffix.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HERDR_CONFIG_DIR:-$HOME/.config/herdr}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$CONFIG_DIR"

backup_and_copy() {
    src="$1"
    dst="$2"
    if [ -e "$dst" ] && ! cmp -s "$src" "$dst"; then
        cp "$dst" "$dst.bak-$STAMP"
        echo "backed up $dst -> $dst.bak-$STAMP"
    fi
    cp "$src" "$dst"
    echo "installed $dst"
}

backup_and_copy "$REPO_DIR/config.toml" "$CONFIG_DIR/config.toml"
backup_and_copy "$REPO_DIR/session-usage.py" "$CONFIG_DIR/session-usage.py"
backup_and_copy "$REPO_DIR/status.sh" "$CONFIG_DIR/status.sh"
chmod +x "$CONFIG_DIR/status.sh" "$CONFIG_DIR/session-usage.py"

# Herdr agent integrations. The command refuses when the agent's config
# directory does not exist, so install the agent itself first.
if [ "$#" -gt 0 ]; then
    for agent in "$@"; do
        if herdr integration install "$agent"; then
            echo "installed Herdr integration for $agent"
        else
            echo "WARNING: could not install the $agent integration (is $agent installed?)" >&2
        fi
    done
fi

# Reporter daemon. macOS uses a launchd agent; other systems are told how
# to run the script by hand or through a systemd user unit (see README).
if [ "$(uname -s)" = "Darwin" ]; then
    LABEL="com.$(id -un).herdr-session-usage"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$CONFIG_DIR/session-usage.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>/dev/null</string>
  <key>StandardErrorPath</key>
  <string>$CONFIG_DIR/session-usage.log</string>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "loaded $LABEL"
else
    echo "Not macOS: start $CONFIG_DIR/session-usage.py yourself, or use the"
    echo "systemd user unit from the README."
fi

if command -v herdr >/dev/null 2>&1; then
    herdr server reload-config >/dev/null 2>&1 || true
fi

echo
echo "Done. Verify with: python3 $CONFIG_DIR/session-usage.py --once"
