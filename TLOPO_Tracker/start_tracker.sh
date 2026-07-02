#!/usr/bin/env bash
# start_tracker.sh
# Launches the tracker on macOS/Linux. Run every time you want to use it:
#   bash start_tracker.sh
#
# NOTE: This script is UNTESTED on macOS. See README.txt for
# macOS-specific settings (Screen Recording permission) you will
# likely need to enable first.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "  Starting TLOPO Loot Tracker..."
echo "============================================================"
echo

if [ ! -f "venv/bin/activate" ]; then
    echo "  It looks like setup has not been run yet."
    echo "  Please run install.sh first, then try again."
    echo
    exit 1
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "  If this is the very first time launching, the OCR engine will"
echo "  download a small language model (about 100MB). This only"
echo "  happens once and may take a minute depending on your internet."
echo

python tlopo_tracker.py
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo
    echo "============================================================"
    echo "  The tracker closed unexpectedly (exit code $STATUS)."
    echo "  If nothing is being detected, double check that your"
    echo "  terminal app has Screen Recording permission -- see"
    echo "  README.txt for details."
    echo "============================================================"
    echo
    read -rp "Press Enter to close..."
fi
