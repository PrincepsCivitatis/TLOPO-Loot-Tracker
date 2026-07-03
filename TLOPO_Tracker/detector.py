"""
detector.py
Screen capture, loot-window detection, and OCR extraction for the
TLOPO Loot Tracker.

Runs entirely in a background thread (see LootDetector.run_loop) so the
Tk GUI stays responsive. Never touches game files or the network -- it
only reads pixels off the screen.
"""

import platform
import re
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

from loot_parser import (
    ChestResult,
    LootItem,
    classify_rarity_from_rgb,
    clean_item_name,
    normalize_chest_type,
)

# Title substring (case-insensitive) used to find the actual TLOPO game
# window on Windows, so capture can be restricted to just that window
# instead of the whole screen. Without this, anything else visible on
# screen with a similarly-colored parchment image -- e.g. someone
# scrolling a loot screenshot in a Discord channel -- can be picked up
# as a false positive and contaminate the session with someone else's
# loot. See GitHub issue #1.
GAME_WINDOW_TITLE_SUBSTRING = "legend of pirates online"


def _find_windows_game_windows(title_substring: str) -> List[Tuple[int, Tuple[int, int, int, int]]]:
    """
    Windows-only: locate every visible, non-minimized top-level window
    whose title contains title_substring (case-insensitive), and return
    a list of (hwnd, (left, top, width, height)) for each one that
    appears to be genuinely visible on top at its own location (not
    covered by some other window, like Discord dragged over it).

    Returns a list rather than a single match so multiple simultaneous
    TLOPO windows -- e.g. multiple characters logged in at once, each
    in its own window -- are ALL tracked independently. An earlier
    version of this function only ever considered the single OS-level
    foreground/focused window, which meant that with two TLOPO windows
    open, whichever one wasn't currently focused was invisible to
    detection entirely, even though it might have its own loot popup
    open. See GitHub issue #2.

    Per-window occlusion is approximated by sampling several points
    across the window's rectangle with WindowFromPoint and checking
    that they resolve back to this window (via its top-level ancestor)
    rather than some other app drawn on top of it there. This allows
    multiple TLOPO windows to be considered valid at once (as long as
    each is genuinely visible), while still rejecting a window that's
    actually covered by something else -- see GitHub issue #1. It does
    NOT catch an overlay that renders on top without stealing focus
    (e.g. Discord's own in-game overlay feature) -- reliably capturing
    a specific window's contents regardless of what's drawn on top of
    it would require per-window rendering capture (e.g. PrintWindow
    with PW_RENDERFULLCONTENT), which often doesn't work correctly for
    hardware-accelerated 3D game windows like this one and is out of
    scope here. This is a documented known limitation.
    """
    if platform.system() != "Windows":
        return []
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        user32 = ctypes.windll.user32

        # Declare explicit argument/return types for every function used
        # here. HWND is pointer-sized -- on 64-bit Windows, letting ctypes
        # guess the type of a plain Python int passed as an argument is a
        # classic source of handle-truncation bugs. Being explicit costs
        # nothing and removes that whole class of risk.
        GA_ROOT = 2
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wintypes.HWND

        found_hwnds = []

        def _enum_callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title_substring.lower() in buf.value.lower():
                found_hwnds.append(hwnd)
            return True  # keep enumerating -- there may be more than one

        user32.EnumWindows(EnumWindowsProc(_enum_callback), 0)

        results = []
        for hwnd in found_hwnds:
            if user32.IsIconic(hwnd):
                continue  # minimized -- treat as "not running" for this window

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                continue

            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                continue

            # Sample the center plus each quadrant's midpoint. Requiring a
            # majority (rather than a single point) to resolve back to
            # this window tolerates a small overlay covering part of the
            # window (e.g. a corner notification) without treating the
            # whole window as occluded, while still catching genuine
            # full/majority occlusion (e.g. Discord dragged fully over it).
            sample_points = [
                (left + width // 2, top + height // 2),
                (left + width // 4, top + height // 4),
                (left + 3 * width // 4, top + height // 4),
                (left + width // 4, top + 3 * height // 4),
                (left + 3 * width // 4, top + 3 * height // 4),
            ]
            visible_hits = 0
            for x, y in sample_points:
                pt = wintypes.POINT(x, y)
                hwnd_at_point = user32.WindowFromPoint(pt)
                if not hwnd_at_point:
                    continue
                root = user32.GetAncestor(hwnd_at_point, GA_ROOT)
                if root == hwnd:
                    visible_hits += 1

            if visible_hits < 3:  # majority of 5 sample points
                continue  # covered by something else -- don't trust this capture

            results.append((hwnd, (left, top, width, height)))

        return results
    except Exception as e:
        print(f"[TLOPO detect] Windows game-window lookup failed (treating as not running): {e}", flush=True)
        return []

# Default parchment background color, RGB. Measured directly from a real
# TLOPO loot popup screenshot on Windows via tools/color_sampler.py (the
# spec's rough estimate of 210,185,140 was off by ~40 on the blue
# channel, which caused every frame to fail matching).
#
# This is only a DEFAULT -- different platforms/graphics drivers/color
# profiles (Mac in particular, since this has never been tested there)
# may render the game with slightly different colors. Users can
# recalibrate this without editing code via the Settings panel; see
# README.txt "IF SOMETHING ISN'T WORKING" / "MAC USERS" sections.
DEFAULT_PARCHMENT_RGB = (204, 172, 100)
DEFAULT_PARCHMENT_TOLERANCE = 30  # per-channel tolerance

# Minimum contiguous parchment-colored region (in pixels) to consider as a
# candidate loot window, scaled for a 3840x2160 screen. Games at lower
# resolutions will still work since we scan relative fractions of the
# actual captured screen size, not literal pixel counts.
MIN_REGION_FRACTION = 0.01  # candidate region must cover at least 1% of screen area

# The popup must be absent for this many CONSECUTIVE seconds before we
# consider it actually closed. A single missed frame (a flickering
# animation, floating combat text passing over the window, a brief OCR
# hiccup) should not be treated as a close -- otherwise the still-open
# popup gets re-detected as a "new" one a moment later and double-logged.
ABSENCE_CONFIRM_SECONDS = 0.6

# While a loot window is being tracked as open, we periodically re-OCR it
# and compare against the highest item count/gold seen so far for that
# instance (see _seen_item_count/_seen_gold in LootDetector). This is
# what lets a SECOND,
# DIFFERENT chest opened in the exact same screen spot (very common when
# rapidly farming -- one chest closes and the next opens before the
# ABSENCE_CONFIRM_SECONDS + post_close_cooldown_s gap would otherwise
# elapse) get caught and logged immediately, instead of only ever being
# caught after the full close/cooldown cycle. Throttled independently of
# the (often much faster) poll interval since this does a real OCR pass,
# not just a cheap pixel-coverage check.
CONTENT_RECHECK_INTERVAL_S = 0.75

# Minimum cleaned item-name length to count as "real" evidence when
# comparing item counts for chest-growth detection (see
# _check_for_new_chest_content / _countable_item_total). Short, unstable
# OCR fragments from some unidentified bit of the screen near the loot
# popup (observed in practice: read as 'arg L' on one frame and as two
# separate fragments, 'LvJu' + 'TV', on the very next re-read of the same
# still-open chest) can otherwise look like new items appearing between
# rechecks. Real TLOPO item names are always multi-word and comfortably
# longer than this, so raising the bar here trades a slightly slower
# (falls back to the normal absence-confirm + cooldown path instead)
# detection of a new chest whose first distinguishing item happens to be
# unusually short, for much lower risk of a false "new chest" trigger
# from noise.
GROWTH_ITEM_MIN_NAME_LEN = 5

# EasyOCR (like most OCR engines) reads small text much less reliably
# than larger text -- game UI text captured at native resolution is
# often small enough that individual letters get confused with similar-
# looking ones. Upscaling the image before handing it to OCR is the
# single most effective lever for this. This does make each OCR read
# slower (more pixels to process), which matters more the lower the
# polling interval is set -- if OCR reads start feeling sluggish after
# raising this, that's the tradeoff to weigh against accuracy.
OCR_UPSCALE_FACTOR = 2.0

# Item names get a SECOND, targeted OCR pass on just their own small
# cropped box, upscaled far more aggressively than the whole-window
# pass above (safe to do since it's now a tiny image, not the whole
# popup). This matters specifically for item names because Famed/
# Legendary items are tracked by exact name match with a running count
# -- a misspelling ("Miracle Water" read as "Miradle Water") would
# wrongly count as a different item instead of incrementing the same
# one, which is a much bigger problem than a misread gold amount or
# chest title would be.
NAME_REREAD_UPSCALE_FACTOR = 8.0


@dataclass
class DetectorSettings:
    poll_interval_ms: int = 500
    # Time a window sits in cooldown (no fresh-detection search) after its
    # popup is confirmed closed, purely to let a closing fade animation's
    # leftover parchment pixels clear before they're mistaken for a new
    # popup at the same spot. This used to default to 2.0s (on top of the
    # old 1.2s ABSENCE_CONFIRM_SECONDS) and wasn't user-adjustable, which
    # meant a window couldn't pick up a genuinely new chest for up to
    # ~3.2s after the last one closed -- far too slow for fast
    # multi-character/rapid-loot playstyles. Lowered and exposed in
    # Settings; the CONTENT_RECHECK mechanism in LootDetector now catches
    # most same-spot rapid reopens well before this cooldown would even
    # matter, so this is mainly a safety net for the fade-animation case
    # it was originally added for.
    post_close_cooldown_s: float = 0.4
    hsv_targets: Optional[dict] = None  # overrides loot_parser.DEFAULT_HSV_TARGETS
    parchment_rgb: Optional[Tuple[int, int, int]] = None       # overrides DEFAULT_PARCHMENT_RGB
    parchment_tolerance: Optional[int] = None                  # overrides DEFAULT_PARCHMENT_TOLERANCE


class LootDetector:
    """
    Background-thread screen watcher. Call start()/stop() from the GUI
    thread. Detected chest results are delivered via the on_chest_detected
    callback (invoked from the *background* thread -- the GUI must marshal
    back to the main thread, e.g. via a queue or root.after).
    """

    def __init__(
        self,
        on_chest_detected: Callable[[ChestResult], None],
        on_status_change: Callable[[str], None],
        on_error: Callable[[str], None],
        settings: Optional[DetectorSettings] = None,
    ):
        self.on_chest_detected = on_chest_detected
        self.on_status_change = on_status_change
        self.on_error = on_error
        self.settings = settings or DetectorSettings()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused

        self._ocr_reader = None
        self._ocr_lock = threading.Lock()
        self._ocr_ready = False

        # Per-window presence/tracking state, keyed by "window key" (the
        # hwnd on Windows when multiple TLOPO windows may be open at once,
        # or a constant sentinel on non-Windows where a single full-screen
        # region is used). Keeping this per-key rather than as single
        # scalars lets multiple simultaneously-open TLOPO windows (e.g.
        # multiple characters logged in at once) each be tracked
        # independently, so a loot popup on one window is never missed
        # just because another window happened to be focused. See
        # GitHub issue #2.
        self._window_present: dict = {}
        self._cooldown_until: dict = {}
        self._first_absent_at: dict = {}
        self._last_known_box: dict = {}

        # Tracks the HIGHEST item count / gold amount seen so far for the
        # CURRENT loot window instance at each window_key, so a periodic
        # re-read while the window is still open can tell "player is
        # taking items one at a time / hit Take Small Items" (count/gold
        # only ever shrinks from here on -- never re-logged) apart from "a
        # new, different chest opened in this same screen spot" (count or
        # gold goes ABOVE anything seen so far -- only possible if the old
        # popup was actually replaced by a new one, since taking items can
        # never add to what's on screen). Deliberately count-based rather
        # than tracking individual item names: two different chests can
        # easily drop the same-named item (including two different Famed/
        # Legendary drops in a row), and a name-based "already seen this
        # exact name" check would wrongly swallow the second one instead
        # of logging it. Reset to 0 whenever a window is confirmed closed.
        # See CONTENT_RECHECK_INTERVAL_S and _check_for_new_chest_content.
        self._seen_item_count: dict = {}
        self._seen_gold: dict = {}
        self._last_content_check_at: dict = {}
        # True if the PREVIOUS recheck for this window saw growth over the
        # baseline that hasn't been confirmed by a second consecutive
        # recheck yet -- see _check_for_new_chest_content.
        self._pending_growth: dict = {}

        self.active_target_getter: Optional[Callable[[], str]] = None
        self.kill_number_getter: Optional[Callable[[], int]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def pause(self):
        self._pause_event.set()

    def resume(self):
        self._pause_event.clear()

    # ------------------------------------------------------------------
    # OCR initialization (lazy, so the GUI can launch instantly and show
    # a "downloading model" message rather than blocking startup)
    # ------------------------------------------------------------------
    def _ensure_ocr(self):
        if self._ocr_ready:
            return True
        with self._ocr_lock:
            if self._ocr_ready:
                return True

            stop_monitor = threading.Event()
            monitor_thread = threading.Thread(
                target=self._monitor_model_download, args=(stop_monitor,), daemon=True
            )
            try:
                self.on_status_change("Downloading OCR model (first launch only, ~100MB)...")
                monitor_thread.start()
                import easyocr  # imported lazily -- heavy import
                self._ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
                self._ocr_ready = True
                self.on_status_change("Waiting for TLOPO...")
                return True
            except Exception as e:
                self.on_error(f"OCR engine failed to initialize: {e}")
                return False
            finally:
                stop_monitor.set()
                monitor_thread.join(timeout=2)

    def _monitor_model_download(self, stop_event: threading.Event):
        """
        Polls the EasyOCR model folder while the model is downloading and
        reports growing byte counts to the status bar, so the user can see
        the download is actually progressing rather than appearing frozen.
        """
        import os as _os

        model_dir = _os.path.join(_os.path.expanduser("~"), ".EasyOCR", "model")
        last_reported = -1
        while not stop_event.wait(timeout=1.0):
            total_bytes = 0
            try:
                if _os.path.isdir(model_dir):
                    for fname in _os.listdir(model_dir):
                        fpath = _os.path.join(model_dir, fname)
                        if _os.path.isfile(fpath):
                            total_bytes += _os.path.getsize(fpath)
            except Exception:
                continue

            mb = total_bytes / (1024 * 1024)
            rounded = round(mb, 1)
            if rounded != last_reported:
                last_reported = rounded
                self.on_status_change(
                    f"Downloading OCR model (first launch only, ~100MB)... {rounded:.1f} MB so far"
                )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def _run_loop(self):
        try:
            import mss
        except Exception as e:
            self.on_error(f"Screen capture library (mss) failed to load: {e}")
            return

        if not self._ensure_ocr():
            return

        try:
            with mss.mss() as sct:
                while not self._stop_event.is_set():
                    # Floor matches MIN_POLL_INTERVAL_MS in tlopo_tracker.py
                    # (lowered from an earlier 100ms floor per GitHub issue
                    # #3 -- fast-looting playstyles can open/close loot
                    # containers faster than that allowed detection to
                    # catch). Very low values increase CPU usage since each
                    # cycle does real screenshot + image-matching work.
                    interval = max(0.01, self.settings.poll_interval_ms / 1000.0)

                    if self._pause_event.is_set():
                        self.on_status_change("Waiting for TLOPO...")
                        time.sleep(interval)
                        continue

                    regions = self._resolve_capture_regions(sct)
                    if not regions:
                        # Game not running, minimized, or (on Windows) no
                        # window is currently genuinely visible -- pause
                        # detection entirely rather than scanning the whole
                        # screen, which could otherwise pick up unrelated
                        # on-screen content (e.g. a loot screenshot open in
                        # a Discord window, or Discord dragged on top of
                        # the game) as a false positive.
                        self.on_status_change("Waiting for TLOPO...")
                        time.sleep(interval)
                        continue

                    any_detected = False
                    for window_key, region in regions:
                        try:
                            if self._scan_once(sct, window_key, region):
                                any_detected = True
                        except Exception:
                            # Never let a single bad frame kill the whole loop.
                            traceback.print_exc()

                    if not any_detected:
                        self.on_status_change("Waiting for TLOPO...")

                    time.sleep(interval)
        except Exception as e:
            self.on_error(f"Detection loop crashed: {e}")

    def _resolve_capture_regions(self, sct) -> List[Tuple[object, dict]]:
        """
        Returns a list of (window_key, mss_region) pairs to scan this
        frame, one per genuinely visible TLOPO window on Windows (so
        multiple simultaneously-open windows -- e.g. multiple characters
        logged in at once -- are all tracked independently; see GitHub
        issue #2), scoped so other on-screen apps (like Discord) can
        never be mistaken for the game. window_key uniquely identifies
        each window (the hwnd on Windows) so per-window presence/tracking
        state never gets mixed up between two different windows.

        Returns an empty list if the game appears to not be running, is
        minimized, or (Windows only) no matching window is currently
        genuinely visible -- see the docstring on
        _find_windows_game_windows for what the visibility check does
        and does not cover (e.g. non-focus-stealing overlays).
        """
        if platform.system() == "Windows":
            windows = _find_windows_game_windows(GAME_WINDOW_TITLE_SUBSTRING)
            return [
                (hwnd, {"left": left, "top": top, "width": width, "height": height})
                for hwnd, (left, top, width, height) in windows
            ]

        # Non-Windows: window-scoped capture isn't implemented yet, so we
        # fall back to the full virtual screen under a single constant
        # key. NOTE: this means the Discord-false-positive issue this fix
        # addresses can still occur on Mac/Linux until window-scoped
        # capture is added there too.
        return [("_fullscreen", sct.monitors[0])]

    # ------------------------------------------------------------------
    # Per-frame scan
    # ------------------------------------------------------------------
    def _scan_once(self, sct, window_key, monitor) -> bool:
        """
        Scans a single window's region. Returns True if a loot window is
        currently being tracked/read for this window_key (used by the
        caller to decide whether to show the idle "Waiting for TLOPO..."
        status), False otherwise.
        """
        now = time.time()
        if now < self._cooldown_until.get(window_key, 0.0):
            return False

        shot = sct.grab(monitor)
        frame = np.array(shot)[:, :, :3][:, :, ::-1]  # BGRA -> RGB

        last_box = self._last_known_box.get(window_key)
        if self._window_present.get(window_key) and last_box is not None:
            # We're already tracking an open window -- use the lenient
            # same-spot check instead of re-running the strict fresh-
            # detection search every frame. The strict search is tuned to
            # reliably find a NEW popup and can drop out for a frame or
            # two on a window that's still genuinely open (animations,
            # floating combat text, etc.), which was causing the same
            # chest to be treated as closed-then-reopened and logged twice.
            mask = self._parchment_mask(frame)
            if self._region_still_present(mask, last_box):
                self._first_absent_at[window_key] = None
                if now - self._last_content_check_at.get(window_key, 0.0) >= CONTENT_RECHECK_INTERVAL_S:
                    self._last_content_check_at[window_key] = now
                    self._check_for_new_chest_content(frame, last_box, window_key)
                return True

            # Coverage dropped in the tracked spot -- might still just be a
            # transient blip, so require sustained absence before treating
            # it as an actual close.
            first_absent = self._first_absent_at.get(window_key)
            if first_absent is None:
                self._first_absent_at[window_key] = now
            elif now - first_absent >= ABSENCE_CONFIRM_SECONDS:
                self._cooldown_until[window_key] = now + self.settings.post_close_cooldown_s
                self._window_present[window_key] = False
                self._last_known_box[window_key] = None
                self._first_absent_at[window_key] = None
                self._seen_item_count[window_key] = 0
                self._seen_gold[window_key] = 0
                self._pending_growth[window_key] = False
            return False

        # Not currently tracking a window -- run the strict fresh-detection
        # search to see if a brand new popup has appeared.
        region = self._find_loot_window(frame)
        if region is None:
            return False

        self._window_present[window_key] = True
        self._last_known_box[window_key] = region
        self._first_absent_at[window_key] = None
        self._last_content_check_at[window_key] = now
        self._pending_growth[window_key] = False
        self.on_status_change("Loot window detected — reading...")

        result = self._read_loot_window(frame, region)
        if result is not None:
            self._seen_item_count[window_key] = self._countable_item_total(result.items)
            self._seen_gold[window_key] = result.gold
            self.on_chest_detected(result)
        else:
            self._seen_item_count[window_key] = 0
            self._seen_gold[window_key] = 0
        return True

    @staticmethod
    def _countable_item_total(items: List[LootItem]) -> int:
        """See GROWTH_ITEM_MIN_NAME_LEN -- excludes short, noise-prone
        candidate names from the count used for chest-growth comparisons."""
        return sum(1 for i in items if len(i.name) >= GROWTH_ITEM_MIN_NAME_LEN)

    def _check_for_new_chest_content(self, frame: np.ndarray, box, window_key) -> None:
        """
        Re-OCRs an already-tracked (still visibly open) loot window and
        compares its item count / gold against the highest seen so far for
        this instance (_seen_item_count/_seen_gold). Taking items out of
        the popup one at a time, or hitting "Take Small Items" (which
        removes gold and small currency items but leaves weapons/clothes
        on screen until pressed again), can only ever make the visible
        item count and gold shrink from here -- so that's left alone,
        never re-logged.

        If instead the item count or a LABELED gold reading is HIGHER than
        anything seen so far, that can only mean a fresh, different chest
        opened in this same screen spot before the old one was confirmed
        closed (taking items never adds more to the screen) -- most likely
        because the whole open-close-reopen happened faster than
        ABSENCE_CONFIRM_SECONDS could register the popup as ever having
        gone away. Gold is read with require_labeled_gold=True here
        specifically: once gold is actually collected, its "Gold" label
        disappears from the popup, and without this, the fallback
        "first number found" logic in _extract_gold can grab something
        unrelated (e.g. the Loot Rating score) and misread it as a new,
        larger gold amount.

        This growth check alone isn't fully trustworthy on its own, though
        -- stray, non-deterministic OCR misreads of some unrelated bit of
        the popup (a texture, an icon edge) can occasionally get read as a
        short garbage "item" on one frame and not the next, which would
        otherwise look like transient growth. So a growth reading is only
        acted on once it's seen on two CONSECUTIVE recheck passes in a
        row (see _pending_growth) -- a real new chest's contents persist
        frame to frame, while one-off OCR noise generally doesn't repeat
        identically. When confirmed, the entire current read is logged as
        a new ChestResult -- deliberately NOT restricted to just the
        "new" items by name, since two different chests can easily drop
        an item with the same name (including two different Famed/
        Legendary drops in a row), and by the time a new chest's popup is
        showing, the previous one's popup is already gone, so the full
        current read IS the new chest's contents.
        """
        reread = self._read_loot_window(frame, box, require_labeled_gold=True)
        if reread is None:
            self._pending_growth[window_key] = False
            return

        seen_count = self._seen_item_count.get(window_key, 0)
        seen_gold = self._seen_gold.get(window_key, 0)
        current_count = self._countable_item_total(reread.items)
        grew = current_count > seen_count or reread.gold > seen_gold

        if not grew:
            # Back within (or still at) the known baseline -- items being
            # taken, or a one-frame OCR undercount/misread that didn't
            # repeat. Baseline is intentionally NEVER lowered here: if it
            # tracked the current (possibly transient) low count instead
            # of the running max, a later frame simply reading the same
            # still-open chest correctly again would look like "growth"
            # and get wrongly logged a second time.
            self._pending_growth[window_key] = False
            return

        if not self._pending_growth.get(window_key):
            # First frame to show growth -- could be a real new chest, or
            # a one-off OCR misread. Wait for the next recheck to confirm
            # before logging anything.
            print(f"[TLOPO detect] possible new chest content at same spot "
                  f"(awaiting confirmation): items={[i.name for i in reread.items]} "
                  f"gold={reread.gold} (previous seen_count={seen_count} seen_gold={seen_gold})", flush=True)
            self._pending_growth[window_key] = True
            return

        print(f"[TLOPO detect] new chest content confirmed at same spot: "
              f"items={[i.name for i in reread.items]} gold={reread.gold} "
              f"(previous seen_count={seen_count} seen_gold={seen_gold})", flush=True)
        self.on_chest_detected(reread)
        self._seen_item_count[window_key] = current_count
        self._seen_gold[window_key] = reread.gold
        self._pending_growth[window_key] = False

    # ------------------------------------------------------------------
    # Window region detection (color-based, resolution independent)
    # ------------------------------------------------------------------
    def _effective_parchment_rgb_tolerance(self) -> Tuple[np.ndarray, int]:
        rgb = self.settings.parchment_rgb
        rgb = np.array(rgb if rgb is not None else DEFAULT_PARCHMENT_RGB)
        tolerance = self.settings.parchment_tolerance
        tolerance = tolerance if tolerance is not None else DEFAULT_PARCHMENT_TOLERANCE
        return rgb, tolerance

    def _parchment_mask(self, frame: np.ndarray) -> np.ndarray:
        rgb, tolerance = self._effective_parchment_rgb_tolerance()
        diff = np.abs(frame.astype(np.int16) - rgb.astype(np.int16))
        return np.all(diff <= tolerance, axis=-1)

    @staticmethod
    def _region_still_present(mask: np.ndarray, box: Tuple[int, int, int, int]) -> bool:
        """
        Lenient check used ONLY while a window is already being tracked:
        is there still meaningful parchment coverage in the same spot we
        last saw it? This is intentionally much looser than the strict
        connected-component search used to detect a brand-new window --
        a transient animation, floating combat text, or a one-frame OCR
        render hiccup can briefly reduce coverage without the popup
        actually having closed, and re-running the strict fresh-detection
        search every frame was causing the same open window to drop out
        and get re-logged as "new" a moment later.
        """
        x1, y1, x2, y2 = box
        h, w = mask.shape[0], mask.shape[1]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return False
        sub = mask[y1:y2, x1:x2]
        if sub.size == 0:
            return False
        return (sub.sum() / sub.size) >= 0.15

    def _find_loot_window(self, frame: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """
        Scan for a parchment-colored rectangular region. Returns
        (x1, y1, x2, y2) bounding box in frame pixel coords, or None.

        Uses connected-component labeling rather than a single global
        bounding box of every matching pixel on screen. The game world
        often has other tan/brown/wood-toned pixels (dirt, wood textures,
        UI trim) that fall within the same color tolerance -- taking the
        bounding box of ALL matches across the whole frame would balloon
        out to include those unrelated pixels and tank the fill-ratio
        check, silently failing detection even when the actual popup is
        on screen. Isolating the single contiguous blob avoids that.

        This strict search is only used to detect a BRAND NEW window.
        Once a window is being tracked, _region_still_present() is used
        instead (see _scan_once) since it's far more tolerant of
        transient per-frame noise.
        """
        h, w = frame.shape[0], frame.shape[1]
        mask = self._parchment_mask(frame)

        total_matching = int(mask.sum())
        if total_matching < MIN_REGION_FRACTION * h * w:
            return None

        try:
            from scipy import ndimage
        except Exception as e:
            self.on_error(f"scipy is required for detection but failed to load: {e}")
            return None

        labeled, num_features = ndimage.label(mask)
        if num_features == 0:
            return None

        sizes = ndimage.sum(mask, labeled, index=range(1, num_features + 1))
        order = np.argsort(sizes)[::-1]  # largest component first

        for idx in order:
            size = sizes[idx]
            if size < MIN_REGION_FRACTION * h * w:
                break  # sorted descending -- everything after this is smaller too

            comp_id = idx + 1
            ys, xs = np.where(labeled == comp_id)
            y1, y2 = int(ys.min()), int(ys.max())
            x1, x2 = int(xs.min()), int(xs.max())

            box_area = max(1, (y2 - y1) * (x2 - x1))
            fill_ratio = size / box_area
            if fill_ratio < 0.4:
                continue
            # Loot windows are roughly square-ish parchment popups, not full-width.
            if (x2 - x1) > 0.9 * w and (y2 - y1) > 0.9 * h:
                continue

            print(f"[TLOPO detect] parchment blob at ({x1},{y1})-({x2},{y2}) "
                  f"size={int(size)}px fill_ratio={fill_ratio:.2f}", flush=True)

            # The parchment color match only covers the tan body of the popup.
            # The chest-type title ("Plundered Loot Chest!" etc.) sits in a
            # separate darker banner directly ABOVE the tan body, so we pad
            # the top of the box to pull that banner into the captured
            # region too. We also pad the other edges slightly to avoid
            # clipping glyph anti-aliasing right at the detected boundary.
            body_h = y2 - y1
            body_w = x2 - x1
            top_pad = int(body_h * 0.35)   # banner is roughly ~1/4-1/3 of body height
            side_pad = int(body_w * 0.03)
            bottom_pad = int(body_h * 0.03)

            y1 = max(0, y1 - top_pad)
            x1 = max(0, x1 - side_pad)
            x2 = min(w, x2 + side_pad)
            y2 = min(h, y2 + bottom_pad)

            return (x1, y1, x2, y2)

        return None

    # ------------------------------------------------------------------
    # OCR + rarity classification of a detected window
    # ------------------------------------------------------------------
    def _read_loot_window(self, frame: np.ndarray, region, require_labeled_gold: bool = False) -> Optional[ChestResult]:
        """
        The real popup layout (confirmed from an actual screenshot) is:
          - A dark banner across the top with the chest-type title
            ("Plundered Loot Chest!" etc.)
          - A tan parchment body below it laid out as a 2-column grid of
            icon + label pairs (one of those pairs is always "Gold" with
            its amount underneath the coin icon; the rest are item names
            in rarity-colored text, or plain white/cream text for
            non-rarity filler like playing cards)
          - A "Loot Rating:" line with a number near the bottom
          - A "Take Small Items" button

        Rather than assume fixed percentage bands (which don't match this
        grid layout), we OCR the whole window once and classify each
        detected line by keyword / shape, so this holds up even if item
        counts or grid layout shift slightly.
        """
        x1, y1, x2, y2 = region
        win = frame[y1:y2, x1:x2]
        win_h, win_w = win.shape[0], win.shape[1]
        if win_h < 20 or win_w < 20:
            return None

        lines = self._ocr_lines_with_boxes(win)
        print(f"[TLOPO detect] OCR read {len(lines)} line(s): "
              f"{[t for t, _ in lines]}", flush=True)
        if not lines:
            return None

        chest_type = None
        gold_label_boxes: List[tuple] = []
        rating_box: Optional[tuple] = None
        numeric_lines: List[Tuple[int, tuple]] = []
        name_candidates: List[Tuple[str, tuple]] = []

        for text, box in lines:
            stripped = text.strip()
            lower = stripped.lower()

            if chest_type is None:
                ct = normalize_chest_type(stripped)
                if ct:
                    chest_type = ct
                    continue

            if lower == "gold":
                gold_label_boxes.append(box)
                continue

            if "rating" in lower:
                rating_box = box
                continue  # "Loot Rating:" label

            # The button reads "Take Small Items" first, then changes to
            # "Take It All" once the small items/gold have already been
            # collected and only larger items (weapons/clothes) remain --
            # matching only "small"/"item" missed that second state, and
            # OCR noise can append stray characters (e.g. "Take It AllI"),
            # so match loosely on "all" too rather than requiring an exact
            # phrase.
            if "take" in lower and ("small" in lower or "item" in lower or "all" in lower):
                continue

            if lower in ("items", "all"):
                # The button label sometimes gets split across two OCR
                # lines ("Take Small" / "Items", or "Take It" / "All")
                # instead of being read as one -- the check above only
                # catches lines containing "take", so a lone leftover
                # "Items"/"All" line would otherwise slip through as a
                # fake untagged loot item now that untagged/currency items
                # are kept rather than discarded.
                continue

            digits_only = re.sub(r"[^0-9]", "", stripped)
            if digits_only and re.fullmatch(r"[\d,]+", stripped):
                numeric_lines.append((int(digits_only), box))
                continue

            name_candidates.append((stripped, box))

        if chest_type is None:
            # Not actually a loot window (could be some other parchment UI,
            # or the banner text wasn't captured/read cleanly this frame).
            print("[TLOPO detect] no chest-type title matched in OCR text -- discarding frame", flush=True)
            return None

        if rating_box is not None and numeric_lines:
            # The "Loot Rating:" score sits on the same row as its label,
            # just to the right of it -- exclude any number sharing that
            # row so it's never mistaken for the gold amount (this
            # happened for real: once gold/small items were already taken
            # and no "Gold" label was left on screen to anchor against,
            # the fallback-to-first-number logic in _extract_gold grabbed
            # the Loot Rating score instead, e.g. reading "110 gold" that
            # was actually the loot rating).
            ry1, ry2 = rating_box[1], rating_box[3]
            r_center_y = (ry1 + ry2) / 2.0
            row_tolerance = max(5, ry2 - ry1)
            numeric_lines = [
                (val, box) for val, box in numeric_lines
                if abs((box[1] + box[3]) / 2.0 - r_center_y) > row_tolerance
            ]

        gold = self._extract_gold(gold_label_boxes, numeric_lines, require_label=require_labeled_gold)

        name_candidates = self._merge_wrapped_item_lines(name_candidates)

        items: List[LootItem] = []
        for text, box in name_candidates:
            name = clean_item_name(text)
            if len(name) < 2:
                print(f"[TLOPO detect] candidate {text!r} skipped: cleaned name too short", flush=True)
                continue
            color = self._sample_text_color(win, box)
            if color is None:
                print(f"[TLOPO detect] candidate {name!r} box={box} skipped: "
                      f"no text-colored pixels found in box", flush=True)
                continue

            # A color WAS found (this is real text, not empty background),
            # but it may not match any rarity tier -- that's expected and
            # intentional for currency/filler items (Gold, gems, playing
            # cards) which the game renders in plain white/cream text with
            # no rarity color. These are still real loot the player
            # received and should still be tracked, just without a rarity
            # tag, rather than silently discarded.
            rarity = classify_rarity_from_rgb(color, self.settings.hsv_targets)

            # Named items (especially Famed/Legendary) are tracked by EXACT
            # name match with a running count -- if OCR spells the same
            # item slightly differently between drops (a real, observed
            # problem: "Miracle Water" read as "Miradle Water"), each
            # misspelling would wrongly count as a separate item instead
            # of incrementing the same one. The whole-window OCR pass
            # upscales everything uniformly, which isn't enough for these
            # small name labels specifically -- re-reading just this box,
            # cropped tightly and blown up much further since it's now a
            # tiny image, gets meaningfully better character accuracy.
            reread = self._reread_item_name(win, box)
            print(f"[TLOPO detect] targeted re-OCR for {name!r}: reread={reread!r}", flush=True)
            if reread and len(reread) >= len(name) - 2:
                name = reread

            print(f"[TLOPO detect] candidate {name!r} sampled color={color} -> rarity={rarity}", flush=True)
            items.append(LootItem(name=name, rarity=rarity))

        target = self.active_target_getter() if self.active_target_getter else ""
        kill_number = self.kill_number_getter() if self.kill_number_getter else 0

        print(f"[TLOPO detect] parsed chest_type={chest_type!r} gold={gold} "
              f"items={[(i.name, i.rarity) for i in items]}", flush=True)

        return ChestResult(
            chest_type=chest_type,
            items=items,
            gold=gold,
            timestamp=time.strftime("%H:%M:%S"),
            target=target or "",
            kill_number=kill_number or 0,
        )

    @staticmethod
    def _box_center(box: tuple) -> Tuple[float, float]:
        x1, y1, x2, y2 = box
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @staticmethod
    def _merge_wrapped_item_lines(candidates: List[Tuple[str, tuple]]) -> List[Tuple[str, tuple]]:
        """
        Item names that wrap onto a second line (e.g. "Light Green" /
        "Seamed Tank" for one item called "Light Green Seamed Tank") are
        read by OCR as two separate lines, which previously got logged
        as two separate items each with half the real name.

        Detects vertically-stacked candidate lines that are almost
        certainly the same wrapped label -- a very small vertical gap
        AND nearly identical left edges -- and merges them into one
        combined name/box. This is intentionally conservative: two
        different items stacked in the same grid column (not a line
        wrap) also share a similar left edge, but have noticeably more
        vertical spacing between them than two halves of one wrapped
        line do, so only a tight gap triggers a merge.
        """
        if len(candidates) < 2:
            return candidates

        ordered = sorted(candidates, key=lambda c: (c[1][1], c[1][0]))
        merged: List[Tuple[str, tuple]] = []
        current_text, current_box = ordered[0]

        for text, box in ordered[1:]:
            cx1, cy1, cx2, cy2 = current_box
            nx1, ny1, nx2, ny2 = box
            vertical_gap = ny1 - cy2
            left_edge_diff = abs(nx1 - cx1)

            if -2 <= vertical_gap <= 8 and left_edge_diff <= 15:
                print(f"[TLOPO detect] merging wrapped item name lines "
                      f"{current_text!r} + {text!r} (gap={vertical_gap}, "
                      f"left_edge_diff={left_edge_diff})", flush=True)
                current_text = f"{current_text} {text}"
                current_box = (min(cx1, nx1), min(cy1, ny1), max(cx2, nx2), max(cy2, ny2))
            else:
                merged.append((current_text, current_box))
                current_text, current_box = text, box

        merged.append((current_text, current_box))
        return merged

    def _extract_gold(
        self,
        gold_label_boxes: List[tuple],
        numeric_lines: List[Tuple[int, tuple]],
        require_label: bool = False,
    ) -> int:
        """
        The gold amount is a standalone number near the "Gold" label
        (typically just below its coin icon). We pick whichever detected
        number sits closest to a "Gold" label; if the label wasn't read
        this frame, fall back to the first number found (reading order) --
        UNLESS require_label is set, in which case we'd rather report no
        gold than guess. This matters for the periodic re-check done on an
        already-open window (see _check_for_new_chest_content): once gold
        has actually been collected, the "Gold" label disappears from the
        popup entirely, and blindly falling back to "first number found"
        can grab an unrelated number (e.g. the Loot Rating score) and
        misread it as a brand new, larger gold amount -- wrongly signaling
        that a whole new chest appeared. The initial full read of a
        freshly-opened chest doesn't have this risk (gold is essentially
        always present and labeled at that point), so it still uses the
        fallback.
        """
        if not numeric_lines:
            return 0
        if gold_label_boxes:
            gx, gy = self._box_center(gold_label_boxes[0])
            numeric_lines = sorted(
                numeric_lines,
                key=lambda nb: (
                    (self._box_center(nb[1])[0] - gx) ** 2
                    + (self._box_center(nb[1])[1] - gy) ** 2
                ),
            )
            return numeric_lines[0][0]
        if require_label:
            return 0
        return numeric_lines[0][0]

    # ------------------------------------------------------------------
    # OCR helpers
    # ------------------------------------------------------------------
    def _ocr_lines_with_boxes(self, crop: np.ndarray) -> List[Tuple[str, tuple]]:
        """
        Returns list of (text, bounding_box) using easyocr. bounding_box
        is (x1, y1, x2, y2) relative to `crop` at its ORIGINAL resolution
        -- OCR itself runs on an upscaled copy for better accuracy on
        small text (see OCR_UPSCALE_FACTOR), but the returned boxes are
        scaled back down so every caller can keep treating coordinates
        as relative to the original, unscaled crop with no other changes
        needed.
        """
        if crop.size == 0 or self._ocr_reader is None:
            return []
        try:
            from PIL import Image
            h, w = crop.shape[0], crop.shape[1]
            upscaled_img = Image.fromarray(crop).resize(
                (max(1, int(w * OCR_UPSCALE_FACTOR)), max(1, int(h * OCR_UPSCALE_FACTOR))),
                Image.LANCZOS,
            )
            results = self._ocr_reader.readtext(np.array(upscaled_img))
        except Exception:
            return []

        out = []
        for bbox, text, conf in results:
            if conf < 0.35:
                continue
            xs = [p[0] / OCR_UPSCALE_FACTOR for p in bbox]
            ys = [p[1] / OCR_UPSCALE_FACTOR for p in bbox]
            # Rounding (rather than truncating) and padding by a pixel on
            # each side compensates for the upscale/downscale round-trip
            # shrinking the box slightly. That shrinkage barely matters for
            # large text (title, gold amount) but can crop a small item-
            # name box down to too few pixels for color sampling to find
            # any text at all, silently dropping the item entirely.
            pad = 1
            box = (
                max(0, round(min(xs)) - pad),
                max(0, round(min(ys)) - pad),
                min(w, round(max(xs)) + pad),
                min(h, round(max(ys)) + pad),
            )
            out.append((text, box))
        return out

    def _reread_item_name(self, win: np.ndarray, box: tuple) -> Optional[str]:
        """
        Re-runs OCR on just this item's name box (cropped tightly with a
        small margin, upscaled far more aggressively than the whole-
        window pass since it's now a small image), to get a cleaner read
        of the name specifically. See NAME_REREAD_UPSCALE_FACTOR for why
        this matters more for item names than for other text. Returns
        the cleaned combined text if anything was read, else None --
        callers should keep the original whole-window OCR text as a
        fallback when this returns None.
        """
        if self._ocr_reader is None:
            return None
        x1, y1, x2, y2 = box
        h, w = win.shape[0], win.shape[1]
        pad = 4
        x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
        x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
        if x2 <= x1 or y2 <= y1:
            return None

        sub = win[y1:y2, x1:x2]
        if sub.size == 0:
            return None

        try:
            from PIL import Image
            sh, sw = sub.shape[0], sub.shape[1]
            upscaled = Image.fromarray(sub).resize(
                (max(1, int(sw * NAME_REREAD_UPSCALE_FACTOR)), max(1, int(sh * NAME_REREAD_UPSCALE_FACTOR))),
                Image.LANCZOS,
            )
            # mag_ratio adds further internal magnification on top of the
            # physical upscale above -- cheap here since this is already
            # a small, single-item crop rather than the whole window.
            results = self._ocr_reader.readtext(np.array(upscaled), mag_ratio=1.5)
        except Exception:
            return None

        fragments = [(bbox[0][0], text) for bbox, text, conf in results if conf >= 0.35]
        if not fragments:
            return None
        fragments.sort(key=lambda f: f[0])
        combined = " ".join(clean_item_name(t) for _, t in fragments if clean_item_name(t))
        return combined.strip() or None

    def _sample_text_color(self, crop: np.ndarray, box: tuple) -> Optional[Tuple[int, int, int]]:
        """
        Sample the dominant non-background color within a text bounding
        box. We take the pixels that differ most from the parchment
        background (i.e. the glyph pixels) and average their color.
        """
        x1, y1, x2, y2 = box
        h, w = crop.shape[0], crop.shape[1]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return None

        sub = crop[y1:y2, x1:x2].astype(np.int16)
        if sub.size == 0:
            return None

        rgb, _tolerance = self._effective_parchment_rgb_tolerance()
        diff = np.abs(sub - rgb.astype(np.int16)).sum(axis=-1)
        threshold = np.percentile(diff, 70)
        text_mask = diff >= max(threshold, 40)

        if text_mask.sum() < 3:
            return None

        pixels = sub[text_mask]
        avg = pixels.mean(axis=0)
        return (int(avg[0]), int(avg[1]), int(avg[2]))
