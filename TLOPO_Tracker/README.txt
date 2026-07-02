TLOPO LOOT TRACKER - HOW TO USE THIS
=====================================

This little program sits on your screen while you play The Legend of
Pirates Online. It watches for the loot popup window that appears when
you open a chest, reads what's inside it, and keeps track of everything
for you automatically - kills, gold, item rarity, and especially rare
"Famed" and "Legendary" items by name.

It only looks at what's on your screen. It never touches your game
files and never connects to the internet or any server.


1. FIRST TIME SETUP
--------------------
Double-click "install.bat" and wait for it to finish. This downloads
everything the tracker needs to run. It only takes a few minutes and
you only need to do this once. If a black window pops up with text
scrolling by, that's normal - just let it finish. When it says
"Installation complete!" you're done.

If it tells you Python is missing, follow the instructions it gives
you to install Python, then run install.bat again.


2. EVERY TIME YOU WANT TO PLAY
-------------------------------
Double-click "START_TRACKER.bat". A small window will open in the
top-right corner of your screen. Leave it open while you play - it
will keep watching for loot windows in the background.

The very first time you start it, it needs to download a small file
for reading text (about 100MB). You'll see a message about this - just
wait for it to finish, it only happens once.


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
- If chests aren't being detected: make sure the game window is
  visible on screen (not minimized) and try opening a chest again.
- If item rarities look wrong: click the gear icon (Settings) in the
  top-right of the tracker and adjust the color sliders slightly.
- If item names are misspelled or garbled: open Settings and increase
  the "Detection polling interval" slightly - this gives the reading
  engine a bit more time and can improve accuracy.
- If the tracker window ever crashes, just double-click
  START_TRACKER.bat again. Your session is automatically saved every
  minute, and the tracker will offer to restore it when it reopens
  (as long as it's been less than 8 hours).


9. STARTING FRESH
--------------------
If you want to wipe everything and start a brand new session, click
"Reset Session" and confirm. This cannot be undone, so only use it
when you're sure you're done with the current session (for example,
after exporting your results).


That's it! Set your target, click +1 as you get kills, and let the
tracker do the rest.
