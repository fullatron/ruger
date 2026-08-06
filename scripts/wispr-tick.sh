#!/bin/sh
# One tick of the unattended pipeline: Wispr Flow -> inbox -> board -> Notion.
# Driven by ~/Library/LaunchAgents/ai.ruger.wispr.plist every 5 minutes.
#
# Deliberately no `set -e`. Each stage is independent and a failure in one must
# not skip the rest: a Featherless outage should not stop the push of what is
# already extracted, and a Notion outage should not undo an extraction.
#
# Push and pull are skipped when nothing changed. They are O(board) rather than
# O(new work) -- push re-sends every row so local edits reach Notion -- so an
# idle wake would otherwise cost a few seconds of API traffic every 5 minutes
# for nothing. With the gate, an idle tick is two SQLite reads.

# Derived from this script's own location, so a clone works anywhere. launchd
# invokes it by absolute path, which is what makes $0 usable here.
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY="$REPO/.venv/bin/python"

cd "$REPO" || exit 1

printf '\n=== %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# 1. Wispr's SQLite + ndjson -> markdown in ~/.pkm/inbox.
imported=$("$PY" -m pkm wispr 2>&1)
status=$?
printf '%s\n' "$imported"
[ $status -eq 0 ] || printf '! wispr import exited %s\n' "$status"

# 2. inbox -> events -> episodes -> extraction -> commitments.
synced=$("$PY" -m pkm sync 2>&1)
status=$?
printf '%s\n' "$synced"
[ $status -eq 0 ] || printf '! sync exited %s\n' "$status"

# Did anything actually move? "wrote 0" from the import and a sync that neither
# extracted nor refreshed means the board is identical to five minutes ago.
changed=1
case "$imported" in *"wrote 0"*)
  case "$synced" in
    *"0 episodes extracted"*)
      case "$synced" in *"episode(s) refreshed"*) ;; *) changed=0 ;; esac
      ;;
  esac
  ;;
esac

# 3. commitments -> Notion. Creates the pages that are missing and touches
#    nothing else: Notion owns a card once it exists.
if [ "$changed" -eq 0 ]; then
  echo "nothing new to push"
else
  "$PY" -m pkm push 2>&1 || printf '! push exited %s\n' "$?"
fi

# 4. Notion status -> local, on EVERY tick, including the idle ones.
#    This used to sit behind the same gate as push, which meant the local view
#    only refreshed when new work arrived -- stale exactly when you are working
#    in Notion and not recording meetings. It is one read-only query.
#    Still no --prune: an automated sync must not archive anything.
"$PY" -m pkm pull 2>&1 || printf '! pull exited %s\n' "$?"

# 5. Cards that have sat in Done long enough get filed under Archive (section 18).
#    Ungated on purpose, like the pull: what makes a card old enough is the
#    calendar, not whether a meeting happened. It costs one indexed SQLite read
#    when nothing is due, and reaches Notion only when something is.
"$PY" -m pkm archive 2>&1 || printf '! archive exited %s\n' "$?"
