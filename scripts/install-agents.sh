#!/bin/sh
# Install the launchd agents for the current checkout:
#
#     ai.ruger.wispr    Wispr Flow -> inbox -> board -> Notion, every 300s
#     ai.ruger.board    serves the board at 127.0.0.1:8765, kept alive
#     ai.ruger.menubar  the menu bar app, if it has been built
#
#     sh scripts/install-agents.sh            # install (or reinstall)
#     sh scripts/install-agents.sh --remove   # stop and remove them
#
# The plists carry absolute paths, because launchd resolves nothing for you and
# hands the job a bare environment. That is why they are templates: the repo path
# is substituted here rather than committed as one person's home directory.
#
# Re-running is safe. Each agent is booted out before it is bootstrapped again,
# which is also how you pick up an edited plist -- launchd caches the old one
# otherwise, and you end up debugging a definition that is no longer on disk.

set -e

REPO=$(cd "$(dirname "$0")/.." && pwd)
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
# Every label this script has ever installed. Used for bootout and --remove, so a
# menu bar agent still gets cleaned up after its binary has been deleted.
ALL_LABELS="ai.ruger.wispr ai.ruger.board ai.ruger.menubar"

# What to install now. The menu bar app is optional — everything works without it
# — so it goes in only once it has actually been built.
LABELS="ai.ruger.wispr ai.ruger.board"
if [ -x "$REPO/build/RugerBar" ]; then
  LABELS="$LABELS ai.ruger.menubar"
elif [ "$1" != "--remove" ]; then
  echo "note: build/RugerBar is not built, so the menu bar app is being skipped"
  echo "      run: sh scripts/build-menubar.sh"
fi

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "! no interpreter at $REPO/.venv/bin/python"
  echo "  run: uv venv .venv && uv pip install --python .venv/bin/python anthropic openai"
  exit 1
fi

for label in $ALL_LABELS; do
  # `bootout` fails when the label was never loaded, which is not an error here.
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
done

if [ "$1" = "--remove" ]; then
  for label in $ALL_LABELS; do
    rm -f "$AGENTS/$label.plist"
  done
  echo "removed: $ALL_LABELS"
  exit 0
fi

mkdir -p "$AGENTS" "$HOME/.pkm/logs"

for label in $LABELS; do
  template="$REPO/scripts/$label.plist.template"
  [ -f "$template" ] || { echo "! missing $template"; exit 1; }
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$template" \
    > "$AGENTS/$label.plist"
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
  echo "installed $label"
done

echo
echo "repo:   $REPO"
echo "logs:   $HOME/.pkm/logs/{wispr,board}.log"
echo
echo "check:  launchctl print $DOMAIN/ai.ruger.wispr | grep -E 'runs|last exit'"
echo "board:  $("$REPO/.venv/bin/python" -c 'from pkm import status; print(status.url_for())')"
