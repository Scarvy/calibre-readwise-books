#!/bin/bash
# Scripted reading scenarios: each runs the logger (one log per scenario) while
# driving the Calibre viewer with keypresses. Books open through the Calibre
# app (calibre://view-book), the same way double-clicking does.
#
# Takes over the screen during page turns. Needs macOS Accessibility +
# Automation (System Events) permission for the terminal app.
#
# Usage:  logger/scenarios.sh [scenario ...]
# Scenarios: two-books normal-read long-dwell away-and-back (default: all)
#
# Moves the saved reading position of the books used below.
set -u
cd "$(dirname "$0")/.."

LIBRARY_ID="Calibre_Library"
BOOK_A_ID=4 BOOK_A_TITLE="JavaScript"  # JavaScript: The Good Parts
BOOK_B_ID=1 BOOK_B_TITLE="Quick Start" # Quick Start Guide
NEXT_PAGE=124 PREVIOUS_PAGE=123        # right / left arrow key codes

log() { echo "[driver $(date +%H:%M:%S)] $*"; }
wait_seconds() { for _ in $(seq 1 "$1"); do sleep 1; done; }
press() { osascript -e "tell application \"System Events\" to key code $1"; }

start_logger() {
  uv run logger/watch_viewer.py --label "$1" &
  wait_seconds 4
  LOGGER=$(pgrep -nf 'python logger/watch_viewer.py')
}

stop_logger() {
  wait_seconds 5  # let the logger see the final save and window close
  kill -INT "$LOGGER"
  wait
}

open_book() {  # book_id -> sets OPENED_PID to the viewer process
  open "calibre://view-book/$LIBRARY_ID/$1/EPUB"
  wait_seconds 10
  OPENED_PID=$(lsappinfo info -only pid "$(lsappinfo front)" | grep -o '[0-9]\+')
  case "$(ps -o comm= -p "$OPENED_PID")" in
    *ebook-viewer*) log "book $1 open in viewer pid $OPENED_PID" ;;
    *) log "book $1 did not open in a viewer"; return 1 ;;
  esac
}

focus_book() {  # pid title-fragment: bring that book's window to the front
  osascript \
    -e "tell application \"System Events\" to tell (first process whose unix id is $1)" \
    -e "set frontmost to true" \
    -e "try" \
    -e "perform action \"AXRaise\" of (first window whose name contains \"$2\")" \
    -e "end try" \
    -e "end tell"
}

turn_pages() {  # pid title count gap-seconds [key]; gap "random" = 20-75s
  for i in $(seq 1 "$3"); do
    focus_book "$1" "$2"
    press "${5:-$NEXT_PAGE}"
    log "page turn $i ($2)"
    if [ "$4" = random ]; then wait_seconds $((RANDOM % 56 + 20)); else wait_seconds "$4"; fi
  done
}

close_viewer() {  # pid
  focus_book "$1" ""
  osascript -e 'tell application "System Events" to keystroke "q" using command down'
  wait_seconds 3
}

go_away() {  # seconds with Finder in front
  log "away for $1s"
  osascript -e 'tell application "Finder" to activate'
  wait_seconds "$1"
}

scenario_two_books() {
  start_logger "two books"
  open_book $BOOK_A_ID || return
  local a=$OPENED_PID
  open_book $BOOK_B_ID || return
  local b=$OPENED_PID
  for round in 1 2 3 4; do
    log "round $round"
    turn_pages "$a" "$BOOK_A_TITLE" 1 15
    turn_pages "$b" "$BOOK_B_TITLE" 1 15 $PREVIOUS_PAGE  # near its end, so page back
  done
  close_viewer "$a"
  [ "$b" != "$a" ] && close_viewer "$b"
  stop_logger
}

scenario_normal_read() {
  start_logger "normal read"
  open_book $BOOK_A_ID || return
  turn_pages "$OPENED_PID" "$BOOK_A_TITLE" 10 random
  close_viewer "$OPENED_PID"
  stop_logger
}

scenario_long_dwell() {
  start_logger "long dwell"
  open_book $BOOK_A_ID || return
  turn_pages "$OPENED_PID" "$BOOK_A_TITLE" 3 20
  log "dwelling on one page for 400s"
  wait_seconds 400
  turn_pages "$OPENED_PID" "$BOOK_A_TITLE" 3 20
  close_viewer "$OPENED_PID"
  stop_logger
}

scenario_away_and_back() {
  start_logger "away and back"
  open_book $BOOK_A_ID || return
  turn_pages "$OPENED_PID" "$BOOK_A_TITLE" 3 20
  go_away 900
  turn_pages "$OPENED_PID" "$BOOK_A_TITLE" 3 20
  close_viewer "$OPENED_PID"
  stop_logger
}

if ! osascript -e 'tell application "System Events" to get name of every window of process "Finder"' >/dev/null 2>&1; then
  echo "No Accessibility permission for this terminal; see System Settings > Privacy & Security." >&2
  exit 1
fi

for scenario in "${@:-two-books normal-read long-dwell away-and-back}"; do
  for name in $scenario; do
    log "=== $name ==="
    "scenario_${name//-/_}"
  done
done
log "all done"
