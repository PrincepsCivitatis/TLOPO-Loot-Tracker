TLOPO LOOT TRACKER - HOW TO USE THIS
=====================================

Note: this README file (only this file, not the application itself)
was written with AI assistance.

*** THIS BRANCH IS EXPERIMENTAL ***
This branch ("experimental/multi-character") adds support for running
multiple TLOPO windows at once (e.g. multiple characters logged in
simultaneously) and detecting loot on each independently, instead of
only the one window that currently has Windows' focus. This is newer
and less tested than the main branch - if you just want the stable,
single-character version, use the main branch / latest numbered
release instead.


This little program sits on your screen while you play The Legend of
Pirates Online. It watches for the loot popup window that appears when
you open a chest, reads what's inside it, and keeps track of everything
for you automatically - kills, gold, item rarity, and especially rare
"Famed" and "Legendary" items by name.

It only looks at what's on your screen. It never touches your game
files and never connects to the internet or any server.

On Windows, it only ever looks inside actual TLOPO game windows
themselves (found by their window title) - not your whole screen. This
means other things you have open, like Discord, YouTube, or a browser,
are never scanned, even if you're looking at a screenshot of someone
else's loot at the same time. If you have more than one TLOPO window
open at once (for example, multiple characters logged in
simultaneously), this experimental branch watches each of them on its
own, so a chest opened on one character is detected even if a different
character's window currently has Windows' focus.

IMPORTANT: because chests are logged against whichever target is
currently selected in the tracker (see section 3 below), and the
tracker doesn't yet know which specific character a detected chest
came from, if you're farming DIFFERENT targets on different characters
at the same time, all of their loot will still be logged under
whatever single target is currently selected in the tracker UI. This
works correctly if all your logged-in characters are farming the SAME
target together; it does not yet correctly separate loot by character
if they're farming different things simultaneously.

KNOWN LIMITATION: if something is drawn ON TOP of the game window
without actually taking focus away from it - for example, Discord's
built-in "in-game overlay" feature (not the full Discord app, just the
small overlay you can bring up while a game stays focused) - the
tracker cannot currently tell the difference and could still misread
whatever is visibly on top. Properly seeing through overlays like that
would require a much more advanced capture method that isn't reliable
for 3D game windows like this one, so for now: avoid using overlay
features that draw loot screenshots on top of the game while farming,
if you want to be sure nothing gets misread. (This window-scoping is
not yet implemented for the experimental Mac version at all - see "8B"
below.)


1. FIRST TIME SETUP (WINDOWS)
------------------------------
Double-click "install.bat" and wait for it to finish. This downloads
everything the tracker needs to run. It only takes a few minutes and
you only need to do this once. If a black window pops up with text
scrolling by, that's normal - just let it finish. When it says
"Installation complete!" you're done.

If it tells you Python is missing, follow the instructions it gives
you to install Python, then run install.bat again.


2. EVERY TIME YOU WANT TO PLAY (WINDOWS)
------------------------------------------
Double-click "START_TRACKER.bat". A small window will open in the
top-right corner of your screen. Leave it open while you play - it
will keep watching for loot windows in the background.

The very first time you start it, it needs to download a small file
for reading text (about 100MB). You'll see a message about this - just
wait for it to finish, it only happens once.


2B. MAC USERS - THIS IS NOT YET TESTED ON A REAL MAC
-------------------------------------------------------
This tracker was built and tested on Windows 11. A version for Mac
("install.sh" and "start_tracker.sh") is included and SHOULD work in
theory, since the underlying tools it's built on all support Mac, but
nobody has actually confirmed it runs correctly on a real Mac yet. If
you try it and run into problems, that's expected until it's been
properly tested - please report what happened.

To try it on a Mac:

  a) Open the Terminal app, then navigate to the folder where you
     unzipped the tracker (drag the folder onto the Terminal window
     after typing "cd " to fill in the path automatically, then press
     Enter).

  b) Run the setup script once by typing:
         bash install.sh
     and pressing Enter. Follow along with any messages it prints.

  c) Every time you want to play, run:
         bash start_tracker.sh

  d) IMPORTANT - Screen Recording permission: this tracker works by
     taking screenshots of your screen, and Macs block that by default
     for privacy. The FIRST time you run it, macOS will likely either
     ask you to grant permission, or the tracker will just fail to see
     anything without any obvious error. To turn this on yourself:
         Open System Settings -> Privacy & Security -> Screen Recording
         -> turn ON the switch next to "Terminal" (or whichever app
         you used to launch the tracker).
     After changing this setting, you may need to fully quit and
     reopen Terminal (and the tracker) for it to take effect.

  e) If macOS shows a warning about running a downloaded script, you
     may need to allow it under System Settings -> Privacy & Security
     (look for a message near the bottom mentioning the blocked file,
     with an "Allow Anyway" button).

  f) If you don't already have Python 3.10 or newer, install.sh will
     tell you so. You can install it from https://www.python.org/downloads/
     or, if you use Homebrew, by running: brew install python@3.12

  g) IMPORTANT - Mac currently scans your WHOLE screen, not just the
     game: on Windows, the tracker only looks inside the actual game
     window itself, so nothing else you have open (Discord, a browser,
     etc.) can ever be mistaken for the game. That window-finding trick
     hasn't been built for Mac yet, so on Mac the tracker currently
     watches everything on your screen. In practice this means: if you
     have a loot screenshot or similar tan/parchment-colored image open
     in another app (like scrolling through a Discord channel) while
     the tracker is running, it could misread that instead of your
     actual game. Until this is fixed for Mac, it's safest to keep
     other windows with loot screenshots closed while farming.

Everything else in this guide (setting a target, exporting, etc.)
works the same way on Mac as it does on Windows - only the setup/launch
steps are different.


3. BEFORE YOU START FARMING
-----------------------------
Pick who or what you're farming from the "Current Target" dropdown
(for example: Palifico, or Gold Room Enemies). If your target isn't
in the list, choose "Custom..." and type its name. Then click
"Set Target". You'll see "Farming: [name]" appear to confirm.


4. WHILE YOU'RE PLAYING
-------------------------
Every time you kill your target, click the +1 button (or +5 / +10 if
you're killing a group quickly). This keeps your kill count accurate
so the tracker can calculate your Skull Chest drop rate.

You do NOT need to do anything for loot - the tracker watches your
screen automatically and logs each chest the moment it appears,
including the gold amount and every item's rarity.


5. FAMED AND LEGENDARY DROPS
------------------------------
These are the rare, important items. Any time one drops, the tracker
automatically writes down its exact name and keeps a running count.
Look at the panel on the right side of the tracker window - it always
shows every Famed and Legendary item you've gotten this session and
how many of each. If you get a Legendary item, a pop-up will also
appear on your screen to let you know right away.


6. SAVING YOUR RESULTS
------------------------
Click "Export to Excel" or "Export to Text" any time you want a copy
of your session. The files are saved to your Desktop in a folder
called "TLOPO_Tracker_Exports". The Excel file has a page for your
overall totals, a page listing every named Famed/Legendary item you
found, and a page listing every single item from every chest. The
text file has the same information in plain, readable text.


7. SWITCHING TO A NEW TARGET
------------------------------
Finished farming one enemy and moving to another? Just pick the new
target from the dropdown and click "Set Target" again (or use the
"New Target" button as a reminder). Your progress on the previous
target is kept - nothing is lost. Your Famed/Legendary item counts
carry across your whole session, no matter how many targets you farm.


8. IF SOMETHING ISN'T WORKING
--------------------------------
- If chests aren't being detected AT ALL (the status bar never even
  briefly says "Loot window detected"): this almost always means the
  tracker's idea of the loot popup's background color doesn't match
  how your game actually renders it. See "8B. FIXING COLOR DETECTION"
  below - this is the most likely thing to need adjusting on a Mac,
  since colors have never been confirmed to render identically there.
- If item rarities look wrong (a Common item logged as Rare, etc.):
  click the gear icon (Settings) in the top-right of the tracker and
  adjust the rarity color sliders slightly.
- If item names are misspelled or garbled: open Settings and increase
  the "Detection polling interval" slightly - this gives the reading
  engine a bit more time and can improve accuracy.
- If the tracker window ever crashes, just double-click
  START_TRACKER.bat again (or re-run start_tracker.sh on Mac). Your
  session is automatically saved every minute, and the tracker will
  offer to restore it when it reopens (as long as it's been less than
  8 hours).


8B. FIXING COLOR DETECTION (ESPECIALLY FOR MAC)
---------------------------------------------------
The tracker finds the loot popup by looking for its tan/parchment
background color, then reads item rarity by the color of each item's
text. These colors were measured from a real screenshot on Windows -
if your computer renders the game with even slightly different colors
(different monitor, color profile, or - especially on Mac, since this
hasn't been tested there yet - a different graphics path), detection
can fail completely, or pick the wrong rarity for items.

How to tell which one is happening:
  - Chests are NEVER detected, not even briefly: the background
    (parchment) color needs fixing.
  - Chests ARE detected and logged, but the rarity (color) looks
    wrong for some items: the per-rarity colors need fixing instead.

How to find the right colors:
  1. Take a screenshot while a loot popup is open on screen.
       Windows: press the Windows key + Shift + S, or Print Screen.
       Mac: press Command + Shift + 4, then drag over the area (or
       Command + Shift + 3 for the whole screen). Save it as an image
       file (PNG).
  2. Run the color sampler tool on that screenshot:
       Windows: drag the screenshot file onto
                tools\run_color_sampler.bat
       Mac:     open Terminal in the tracker folder and run
                bash tools/run_color_sampler.sh /path/to/screenshot.png
  3. A list of colors will print out, largest area first (as a
     percentage of the image). The tan parchment background will
     usually be one of the first few entries. If you want the color
     of a specific item's text instead, look further down the list
     for a color that matches what you saw on screen.
  4. Open the tracker, click the gear icon (Settings), and scroll to
     "Loot Window Background Color (Parchment)". Set the Red/Green/
     Blue sliders to the numbers you found, click Save, and try again.
     (The per-rarity color sliders further up work the same way, using
     the color of an item's name text instead of the background.)

You do not need to edit any code to do this - everything is adjustable
from the Settings panel.


9. STARTING FRESH
--------------------
If you want to wipe everything and start a brand new session, click
"Reset Session" and confirm. This cannot be undone, so only use it
when you're sure you're done with the current session (for example,
after exporting your results).


That's it! Set your target, click +1 as you get kills, and let the
tracker do the rest.
