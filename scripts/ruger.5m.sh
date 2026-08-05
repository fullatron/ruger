#!/bin/sh
# Ruger's menu bar item, for SwiftBar (or xbar).
#
# <bitbar.title>Ruger</bitbar.title>
# <bitbar.version>1.0</bitbar.version>
# <bitbar.author.github>fullatron</bitbar.author.github>
# <bitbar.desc>Commitments on the board, and whether the import timer is alive.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
#
# Install by symlinking it into your SwiftBar plugin folder. The `5m` in the
# filename is SwiftBar's refresh interval and matches the timer's own 300s, so
# the menu is never more than one tick out of date:
#
#     ln -s "$PWD/scripts/ruger.5m.sh" "$PLUGIN_DIR/ruger.5m.sh"
#
# All of the formatting lives in `pkm status --swiftbar`, so this file stays a
# wrapper: no jq, and the menu is testable without a menu bar.

# Resolve through the symlink, so the repo is found from wherever SwiftBar keeps
# its plugins rather than assumed to be at some fixed path.
SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")
REPO=$(cd "$(dirname "$SELF")/.." && pwd)
PY="$REPO/.venv/bin/python"

[ -x "$PY" ] || PY=$(command -v python3)
[ -n "$PY" ] || { echo "⚠︎ Ruger"; echo "---"; echo "no python found"; exit 0; }

# A crash must still render a menu: SwiftBar shows stderr as the title, which
# would replace the icon with a stack trace.
cd "$REPO" || exit 0
"$PY" -m pkm status --swiftbar 2>/dev/null || {
  echo "⚠︎ Ruger | color=#eb5757"
  echo "---"
  echo "pkm status failed — run it in $REPO to see why"
  echo "Open log | href=file://$HOME/.pkm/logs/wispr.log"
}
