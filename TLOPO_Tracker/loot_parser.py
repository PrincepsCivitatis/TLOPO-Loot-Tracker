"""
loot_parser.py
Rarity classification (HSV-based) and item/text parsing logic for the
TLOPO Loot Tracker.

This module contains NO screen-capture or Tk code so it can be unit
tested / reused independently of the GUI and detector.
"""

import colorsys
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Rarity definitions
# ---------------------------------------------------------------------------

RARITY_ORDER = ["Crude", "Common", "Rare", "Famed", "Legendary"]

# Bump this whenever DEFAULT_HSV_TARGETS below changes. The saved settings
# file stores the version it was written with; on load, a mismatch means
# the user's file predates this recalibration, so the fresh code defaults
# are used instead of the frozen old values (see tlopo_tracker.py
# _load_settings). Without this, a hue recalibration shipped in code would
# never actually take effect on any install that has ever saved settings,
# since the saved hsv_targets dict otherwise always wins wholesale on load
# (GitHub issue #5, sub-issue 3).
#
# Bumped to 3 for the Crude/Common tier rename -- a saved settings file
# from before this rename has "Common"/"Uncommon" keys carrying the OLD
# rarity's colors under the wrong (now reassigned) names, which would be
# actively wrong to keep rather than just stale.
#
# Bumped to 4 for the Crude/Common hue recalibration below, measured from
# real item-card screenshots cropped to just the title text (isolating it
# from the shared italic subtitle line's color, which was contaminating
# earlier whole-card samples) -- see GitHub issue #5.
#
# Bumped to 5 for the matching Rare/Famed/Legendary recalibration, same
# title-cropped measurement method.
HSV_TARGETS_VERSION = 5

# Default HSV centers (H in degrees 0-360, S/V in 0-100) approximated from
# the hex colors given in the spec. These are used as the *default* settings
# and can be overridden at runtime via the Settings panel.
# Tier names/order corrected 2026-07-07 -- the app originally shipped with
# "Common"/"Uncommon" as the two lowest tiers, but TLOPO's real tier names
# are Crude/Common (the code's "Common"/"Uncommon" were each one slot too
# high). Renamed in place: same 5 slots, same colors, only the two lowest
# labels changed (Crude=orange, Common=yellow) -- see GitHub issue #5.
DEFAULT_HSV_TARGETS = {
    # Crude/Common re-measured 2026-07-07 from real item-card title text
    # (tools/color_sampler.py run against a tight crop of just the title
    # word, isolating it from other card UI elements). Both centers'
    # hue were already roughly right, but VALUE was notably too high in
    # the old estimate -- these titles render noticeably less bright than
    # assumed (Crude v=73 vs old 91, Common v=82 vs old 91).
    "Crude":     {"h": 33,  "s": 83, "v": 73, "tolerance": 18},   # Orange  #B97520
    "Common":    {"h": 60,  "s": 100, "v": 82, "tolerance": 18},   # Yellow #D1D100
    # Rare/Famed/Legendary re-measured 2026-07-07 the same way as Crude/
    # Common above (title-cropped item-card screenshots). All three had
    # roughly correct hue already, but Rare and Famed's SATURATION was
    # notably overestimated in the old defaults (Rare s=65 vs old 84,
    # Famed s=60 vs old 77) -- Legendary barely moved, it was already
    # close. Tolerance left unchanged for all three; only one real sample
    # per tier so far isn't enough to justify retuning the tolerance band
    # itself, just the centers.
    "Rare":      {"h": 113, "s": 65, "v": 65, "tolerance": 26},   # Green   #46A53A
    "Famed":     {"h": 221, "s": 60, "v": 91, "tolerance": 22},   # Blue    #5C89E7
    "Legendary": {"h": 0,   "s": 90, "v": 89, "tolerance": 18},   # Red     #E31616
}

RARITY_DISPLAY_HEX = {
    "Crude": "#9A9A9A",     # rendered grey in log per spec (de-emphasized)
    "Common": "#E8D020",
    "Rare": "#20C820",
    "Famed": "#2050E8",
    "Legendary": "#E82020",
}

CHEST_TYPES = {
    "pouch": "Plundered Loot Pouch!",
    "chest": "Plundered Loot Chest!",
    "skull": "Plundered Loot Skull Chest!",
}


def rgb_to_hsv_degrees(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    """Convert an (r,g,b) 0-255 tuple to HSV where H is 0-360, S/V are 0-100."""
    r, g, b = [max(0, min(255, c)) / 255.0 for c in rgb]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return h * 360.0, s * 100.0, v * 100.0


def _hue_distance(h1: float, h2: float) -> float:
    """Shortest distance between two hues on the 0-360 circle."""
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)


def classify_rarity_from_rgb(
    rgb: Tuple[int, int, int],
    hsv_targets: Optional[dict] = None,
) -> Optional[str]:
    """
    Classify a sampled text color into one of the rarity tiers using HSV
    distance to the configured tier targets. Returns None if the color does
    not fall within tolerance of any known tier (e.g. background/noise pixel).
    """
    targets = hsv_targets or DEFAULT_HSV_TARGETS
    h, s, v = rgb_to_hsv_degrees(rgb)

    # Ignore near-grey / near-black / near-white pixels (parchment background,
    # shadow text edges) -- these have low saturation or extreme value and
    # would otherwise be misclassified.
    if s < 20 or v < 15:
        return None

    best_rarity = None
    best_score = None
    for rarity, cfg in targets.items():
        hue_d = _hue_distance(h, cfg["h"])
        # Weight hue heavily since that's what visually differentiates tiers.
        score = hue_d
        if hue_d <= cfg["tolerance"]:
            if best_score is None or score < best_score:
                best_score = score
                best_rarity = rarity

    return best_rarity


def normalize_chest_type(ocr_title_text: str) -> Optional[str]:
    """
    Given raw OCR text from the title region, determine which of the three
    chest types it represents. Uses fuzzy substring matching to tolerate
    minor OCR errors.
    """
    if not ocr_title_text:
        return None
    text = ocr_title_text.lower()
    text = re.sub(r"[^a-z ]", "", text)

    if "skull" in text and "plunder" in text:
        return "Plundered Loot Skull Chest!"
    if "chest" in text and "plunder" in text:
        return "Plundered Loot Chest!"
    if "pouch" in text and "plunder" in text:
        return "Plundered Loot Pouch!"

    # Fallback: sometimes OCR drops "Plundered"
    if "skull" in text:
        return "Plundered Loot Skull Chest!"
    if "pouch" in text:
        return "Plundered Loot Pouch!"
    if "chest" in text:
        return "Plundered Loot Chest!"
    return None


def clean_item_name(raw_text: str) -> str:
    """Clean up an OCR'd item-name line: strip stray symbols, collapse spaces."""
    if not raw_text:
        return ""
    text = raw_text.strip()
    # Remove common OCR junk characters while keeping apostrophes/hyphens
    text = re.sub(r"[^A-Za-z0-9'\-\.\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_gold_amount(raw_text: str) -> int:
    """Extract an integer gold amount from OCR'd text near the gold icon."""
    if not raw_text:
        return 0
    digits = re.sub(r"[^0-9]", "", raw_text)
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


@dataclass
class LootItem:
    name: str
    rarity: Optional[str]  # one of RARITY_ORDER, or None for untagged
    # currency/filler items (Gold, gems, playing cards) that the game
    # renders with no rarity color but which are still real loot worth
    # tracking.

    def is_named_tier(self) -> bool:
        return self.rarity in ("Famed", "Legendary")


@dataclass
class ChestResult:
    chest_type: str          # "Plundered Loot Pouch!" / "Chest!" / "Skull Chest!"
    items: List[LootItem] = field(default_factory=list)
    gold: int = 0
    timestamp: str = ""
    target: str = ""
    kill_number: int = 0
    # Internal bookkeeping for the detector's session-based accumulation
    # (see detector.py LootDetector._start_session/_finalize_session) --
    # not shown in exports beyond session_id tagging a loot_log row so a
    # later amendment can find it again.
    session_id: Optional[str] = None
    # True for a follow-up correction to an already-logged chest (late-
    # arriving items/gold discovered after the initial log), rather than
    # a brand new chest. When True, `items`/`gold` hold only what's NEW
    # since the original log, not the chest's full contents.
    is_amendment: bool = False
    # "small" (Take Small Items showing) / "all" (Take It All showing) /
    # None (button not read this frame) -- internal signal only, used to
    # detect a chest reopening in the same spot without ever properly
    # closing (see detector.py's Layer 2 mid-session split).
    button_state: Optional[str] = None

    def chest_key(self) -> str:
        """Return 'pouch' / 'chest' / 'skull' short key for counters."""
        if "Skull" in self.chest_type:
            return "skull"
        if "Chest" in self.chest_type:
            return "chest"
        return "pouch"
