#!/bin/sh
# The capture dialog (§10). Opens a focused text box, sends what you typed or
# dictated into `pkm capture`, and reports through a notification.
#
#     sh scripts/ruger-capture.sh
#
# Clicked from the menu bar item, or bound to a hotkey with the stock Shortcuts
# app ("Run Shell Script" → this path) so you never have to reach for the menu.
#
# Ruger records no audio (D12). This is a text field: dictate into it with Wispr
# Flow or macOS dictation, and the operating system does the transcription.

SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")
REPO=$(cd "$(dirname "$SELF")/.." && pwd)
PY="$REPO/.venv/bin/python"

[ -x "$PY" ] || { osascript -e 'display notification "No interpreter in .venv" with title "Ruger"'; exit 1; }

# A real multi-line box, not AppleScript's one-line `display dialog` field — see
# capture-dialog.js. Cancel prints nothing, which is not an error.
TEXT=$(osascript -l JavaScript "$REPO/scripts/capture-dialog.js" 2>/dev/null) || exit 0

# Trim, so a stray space is treated as a cancel rather than an empty capture.
case "$(printf '%s' "$TEXT" | tr -d '[:space:]')" in
  "") exit 0 ;;
esac

cd "$REPO" || exit 1
printf '%s' "$TEXT" | "$PY" -m pkm capture --notify >/dev/null 2>&1 &
exit 0
