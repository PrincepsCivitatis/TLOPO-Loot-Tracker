#!/usr/bin/env bash
# run_color_sampler.sh
# macOS/Linux equivalent of run_color_sampler.bat.
#
# Usage: bash tools/run_color_sampler.sh /path/to/screenshot.png

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "$1" ]; then
    echo "Usage: bash run_color_sampler.sh /path/to/screenshot.png"
    echo
    echo "Take a screenshot while the loot popup is open on screen, then"
    echo "pass its file path to this script to see the exact colors it contains."
    exit 1
fi

VENV_PY="$SCRIPT_DIR/../venv/bin/python"
if [ ! -f "$VENV_PY" ]; then
    echo "Could not find the tracker's Python environment."
    echo "Please run install.sh in the TLOPO_Tracker folder first."
    exit 1
fi

"$VENV_PY" "$SCRIPT_DIR/color_sampler.py" "$1"
