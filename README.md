# TLOPO Loot Tracker

A free Windows desktop companion for **The Legend of Pirates Online (TLOPO)** that watches your screen while you play, automatically reads loot popups when you open a chest, and keeps a running log of everything you've found.

## Which version should I download?

This repo has three branches/release tracks. They all share the same core loot-tracking engine — the difference is single- vs multi-window support and how thoroughly a feature has been tested.

| You are... | Get this |
|---|---|
| A normal player who plays one character at a time | **Main** — [`main` branch](../../tree/main), tagged releases like `v0.06`. Most tested, single-character/single-window only. |
| Running multiple TLOPO characters/windows at once | **Experimental (Multi-Character)** — [`experimental/multi-character` branch](../../tree/experimental/multi-character), tagged releases like `v0.04-experimental`. Alpha-tested and generally working, but still less proven than `main`. |
| Want to help test the newest features first | **This branch (`experimental-alpha`)** — tagged releases like `v0.03-alpha`. Newest multi-window features land here before being promoted to the Multi-Character branch — untested, may have bugs. **You're already looking at the right README.** |

All builds are on the [**Releases**](../../releases) page, distinguishable by their tag suffix (`vX.XX` = main, `vX.XX-experimental` = multi-character, `vX.XX-alpha` = experimental alpha).

## What it does

- **Auto-detects loot popups** ("Plundered Loot Pouch/Chest/Skull Chest!") the moment they appear on screen — no manual entry needed for loot.
- **Reads item rarity by text color** (Common, Uncommon, Rare, Famed, Legendary) using OCR and color analysis.
- **Tracks every Famed and Legendary item by name**, with a running count of how many of each you've gotten, visible at all times.
- **Per-target session stats**: kills (manual +1/+5/+10 buttons), pouch/chest/skull chest counts, and skull-chest drop rate — tracked separately for each boss/enemy you farm, plus combined session totals.
- **Auto-detects boss kills and your farming target** for a growing list of named bosses, by watching the boss's on-screen health bar and nameplate — no need to click +1 or change the target dropdown yourself. Auto-detected kills are shown alongside your manual count (e.g. `12 (9 auto)`) so you can compare the two. **Caveats:** this only works for named bosses on the tracker's known list one-on-one — it does **not** yet work for common/regular enemies, and does **not** work well when killing a whole group of enemies at once (e.g. farming the Bridge or the Hornets), where you should keep using the manual kill buttons. Support for all enemy types is planned for a future release.
- **Exports your session** to a formatted Excel workbook (3 sheets: summary, named item log, full loot log) or a plain text file, saved straight to your Desktop.
- **Runs alongside the game**, always-on-top, and never touches game files or the network — it only reads what's on your screen. On Windows, detection is scoped to just the TLOPO game window itself while it's focused, so other things on your screen (Discord, a browser, etc.) can't be mistaken for the game.

## Download

Grab the latest release from the [**Releases**](../../releases) page — download the zip, extract it, and run `install.bat` once followed by `START_TRACKER.bat` every time you play. No coding knowledge needed.

Full step-by-step setup and usage instructions are in [`TLOPO_Tracker/README.txt`](TLOPO_Tracker/README.txt).

## Found a bug or have a suggestion?

Please open an [Issue](../../issues) — bug reports and feature requests are welcome.

## Requirements

Windows 11, Python 3.10+ (the installer will tell you if it's missing and where to get it).

**macOS**: an experimental `install.sh` / `start_tracker.sh` is included and should work in theory (everything the app is built on — `mss`, `tkinter`, `easyocr`/`torch` — supports macOS), but **this has not actually been tested on a real Mac yet**. You will very likely need to grant your Terminal app **Screen Recording** permission in System Settings → Privacy & Security before the tracker can see anything on screen. Full details are in [`TLOPO_Tracker/README.txt`](TLOPO_Tracker/README.txt) under "MAC USERS." If you try it, please [open an issue](../../issues) with what you found.

**Linux**: not tested and not currently documented — screen capture is unreliable under Wayland depending on your desktop environment, so results may vary even though the same shell scripts would likely work under X11.

**Known limitation (Mac/Linux only)**: the window-scoping described above (only ever looking inside the actual game window) is currently Windows-only. On Mac/Linux, the tracker still scans the whole screen, so other on-screen tan/parchment-colored content (like a loot screenshot open in a Discord window) could be misread as the game. Keep other windows with loot screenshots closed while farming until this is addressed for those platforms.

**Known limitation (all platforms, including Windows)**: window-scoping only helps once the game window is *focused*. Something drawn on top of the game *without* stealing focus — most notably Discord's own in-game overlay feature — can still be misread, since the tracker can't currently distinguish "the game is focused" from "something is visually on top of the focused game." Avoid bringing up overlay content with loot screenshots while farming.

**Known issue**: item names that wrap across two lines in the loot popup don't always get merged back into a single item — occasionally you'll see one item logged as two separate half-name entries. Less commonly, a green (Rare) item can be logged as untagged instead of "Rare" near the edge of the expected color range. Gold amounts and chest counts are unaffected either way. Tracked in [Issue #5](../../issues/5).

## If detection doesn't work on your setup

The tracker finds the loot popup by its background color and reads item rarity by text color, both tuned from a screenshot. If your game renders with different colors (different OS, monitor, or color profile — most likely on Mac, since it's untested there), detection can fail or misclassify rarity. Every one of those colors is adjustable from the in-app **Settings** panel (gear icon) — no code editing required — and a bundled tool (`tools/run_color_sampler.bat` on Windows, `tools/run_color_sampler.sh` on Mac/Linux) reads the exact colors out of a screenshot you take, so you can plug the right numbers in. Full walkthrough: [`TLOPO_Tracker/README.txt`](TLOPO_Tracker/README.txt), section "8B. FIXING COLOR DETECTION."
