#!/bin/sh
# macOS: double-click this file to start the jukebox (it opens in Terminal).
# The browser opens by itself: the setup page the first time, then the admin page.
# Close the Terminal window (or press Ctrl+C) to stop the jukebox.
cd "$(dirname "$0")" || exit 1
sh ./run.sh
status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then  # 130 = stopped with Ctrl+C
    echo
    echo "The jukebox stopped. If that wasn't you, see the message above."
    printf "Press Enter to close this window. "
    read -r _
fi
