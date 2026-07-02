# TLOPO Loot Tracker

A free Windows desktop companion for **The Legend of Pirates Online (TLOPO)** that watches your screen while you play, automatically reads loot popups when you open a chest, and keeps a running log of everything you've found.

## What it does

- **Auto-detects loot popups** ("Plundered Loot Pouch/Chest/Skull Chest!") the moment they appear on screen — no manual entry needed for loot.
- **Reads item rarity by text color** (Common, Uncommon, Rare, Famed, Legendary) using OCR and color analysis.
- **Tracks every Famed and Legendary item by name**, with a running count of how many of each you've gotten, visible at all times.
- **Per-target session stats**: kills (manual +1/+5/+10 buttons), pouch/chest/skull chest counts, and skull-chest drop rate — tracked separately for each boss/enemy you farm, plus combined session totals.
- **Exports your session** to a formatted Excel workbook (3 sheets: summary, named item log, full loot log) or a plain text file, saved straight to your Desktop.
- **Runs alongside the game**, always-on-top, and never touches game files or the network — it only reads what's on your screen.

## Download

Grab the latest release from the [**Releases**](../../releases) page — download the zip, extract it, and run `install.bat` once followed by `START_TRACKER.bat` every time you play. No coding knowledge needed.

Full step-by-step setup and usage instructions are in [`TLOPO_Tracker/README.txt`](TLOPO_Tracker/README.txt).

## Found a bug or have a suggestion?

Please open an [Issue](../../issues) — bug reports and feature requests are welcome.

## Requirements

Windows 11, Python 3.10+ (the installer will tell you if it's missing and where to get it).
