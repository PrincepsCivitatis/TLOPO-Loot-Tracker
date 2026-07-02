#!/usr/bin/env bash
# install.sh
# First-time setup for macOS/Linux. Run once with: bash install.sh
#
# NOTE: This script is UNTESTED on macOS. It mirrors install.bat's steps
# (Python check, venv creation, CPU-only torch, remaining dependencies).
# See README.txt for macOS-specific settings you will likely need to
# enable before the tracker can actually detect anything on screen.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "  TLOPO Loot Tracker - First Time Setup"
echo "============================================================"
echo
echo "This will set up everything the tracker needs to run."
echo "This only needs to be done once. It may take several minutes"
echo "the first time, especially the OCR/AI library install."
echo

# ---------------------------------------------------------------
# Step 1: Check for Python 3.10+
# ---------------------------------------------------------------
echo "[1/5] Checking for Python..."
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo
    echo "============================================================"
    echo "  Python was not found on this computer."
    echo
    echo "  Please install Python 3.10 or newer from:"
    echo "      https://www.python.org/downloads/"
    echo "  (or, on macOS, via Homebrew: brew install python@3.12)"
    echo
    echo "  After installing Python, run this script again."
    echo "============================================================"
    exit 1
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    Found Python $PY_VERSION"

if ! "$PYTHON_BIN" -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo
    echo "  Your Python version ($PY_VERSION) is older than 3.10."
    echo "  Please install Python 3.10 or newer and run this script again."
    exit 1
fi
echo "    Python version OK."
echo

# ---------------------------------------------------------------
# Step 2: Create virtual environment
# ---------------------------------------------------------------
echo "[2/5] Setting up a private Python environment for the tracker..."
if [ -f "venv/bin/activate" ]; then
    echo "    Environment already exists, skipping creation."
else
    "$PYTHON_BIN" -m venv venv
    echo "    Environment created."
fi
echo

# ---------------------------------------------------------------
# Step 3: Activate environment and upgrade pip
# ---------------------------------------------------------------
echo "[3/5] Activating environment and preparing installer..."
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip >/dev/null
echo "    Ready."
echo

# ---------------------------------------------------------------
# Step 4: Install CPU-only torch (required by easyocr)
# ---------------------------------------------------------------
echo "[4/5] Installing the OCR engine's AI backend (CPU-only, this is the"
echo "      biggest download and may take a few minutes)..."
if [ "$(uname -s)" = "Darwin" ]; then
    # macOS wheels are universal/CPU by default -- no special index needed.
    pip install torch
else
    pip install torch --index-url https://download.pytorch.org/whl/cpu
fi
echo "    AI backend installed."
echo

# ---------------------------------------------------------------
# Step 5: Install remaining dependencies
# ---------------------------------------------------------------
echo "[5/5] Installing remaining tracker dependencies..."
pip install -r requirements.txt

echo
echo "============================================================"
echo "  Installation complete! Run start_tracker.sh to launch."
echo "============================================================"
echo
echo "  IMPORTANT (macOS): before it can see anything on screen, you"
echo "  will need to grant your Terminal app Screen Recording"
echo "  permission in System Settings -> Privacy & Security ->"
echo "  Screen Recording. See README.txt for details."
echo
