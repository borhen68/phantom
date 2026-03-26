#!/bin/bash
# Setup a quiet, comfortable rest environment on macOS
# Reduce screen brightness, lower system volume, enable Do Not Disturb, and optionally start a soothing audio.

# Helper: log
log() {
  echo "[setup_rest_env] $1"
}

# 1. Dim screen brightness (requires 'brightness' utility: https://github.com/nriley/brightness)
if command -v brightness >/dev/null 2>&1; then
  TARGET_BRIGHTNESS=0.3  # 0.0 (dark) to 1.0 (full)
  log "Setting screen brightness to $TARGET_BRIGHTNESS"
  brightness $TARGET_BRIGHTNESS
else
  log "'brightness' utility not found. Skipping brightness adjustment."
  log "You can install it via Homebrew: brew install brightness"
fi

# 2. Lower system volume to a quiet level (0-100)
TARGET_VOLUME=20
log "Setting system volume to $TARGET_VOLUME%"
osascript -e "set volume output volume $TARGET_VOLUME"

# 3. Enable Do Not Disturb (macOS 12+ uses Focus)
log "Enabling Do Not Disturb (Focus)"
# Using AppleScript to toggle Do Not Disturb for 1 hour
osascript -e "tell application \"System Events\"
    tell appearance preferences
        set dark mode to true
    end tell
end tell"
# For macOS Monterey and later, we can use defaults + kill
/usr/bin/defaults write ~/Library/Preferences/com.apple.ncprefs.plist dndEnabled -bool true
killall NotificationCenter 2>/dev/null || true

# 4. Optionally start a soothing audio (e.g., from iTunes/Music)
# Uncomment and set your preferred audio file or playlist.
# AUDIO_FILE="/System/Library/Sounds/Submarine.aiff"
# if [[ -f "$AUDIO_FILE" ]]; then
#   afplay "$AUDIO_FILE" &
#   log "Playing soothing sound $AUDIO_FILE"
# else
#   log "Audio file not found, skipping sound playback."
# fi

log "Rest environment setup complete."
