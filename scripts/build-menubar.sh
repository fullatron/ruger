#!/bin/sh
# Compile the menu bar app to build/RugerBar.
#
#     sh scripts/build-menubar.sh
#
# The only build step in the repo, and it stays optional: everything works without
# it, through `pkm status` and `pkm capture` on the command line. Needs the Xcode
# command line tools, which is where swiftc comes from.
#
# No app bundle. A plain executable with an .accessory activation policy is a menu
# bar app already, and a bundle would add signing and Info.plist upkeep for
# nothing — this one is launched by launchd, not double-clicked from Finder.

set -e

REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT="$REPO/build"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "! swiftc not found. Install the Xcode command line tools:"
  echo "    xcode-select --install"
  exit 1
fi

mkdir -p "$OUT"

# -O because it runs all day; the compile is a couple of seconds either way.
swiftc -O \
  -target "$(uname -m)-apple-macos13.0" \
  -o "$OUT/RugerBar" \
  "$REPO/menubar/RugerBar.swift"

echo "built $OUT/RugerBar"
echo
echo "run it now:       $OUT/RugerBar &"
echo "or at every login: sh scripts/install-agents.sh"
