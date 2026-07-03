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

RARITY_ORDER = ["Common", "Uncommon", "Rare", "Famed", "Legendary"]

# Default HSV centers (H in degrees 0-360, S/V in 0-100) approximated from
# the hex colors given in the spec. These are used as the *default* settings
# and can be overridden at runtime via the Settings panel.
DEFAULT_HSV_TARGETS = {
    "Common":    {"h": 28,  "s": 76, "v": 91, "tolerance": 18},   # Orange  #E87820
    "Uncommon":  {"h": 53,  "s": 78, "v": 91, "tolerance": 18},   # Yellow  #E8D020
    # Green/Rare: shifted from the spec's rough estimate (h=120, tolerance=22)
    # after a real sampled Rare item ("Venomed Cutlass", RGB 42,60,26 ->
    # hue ~92) fell just outside that window and got wrongly treated as
    # untagged. Re-centered lower with a wider tolerance to cover it,
    # while keeping enough of a gap from Uncommon's range (35-71) to
    # avoid the two tiers colliding.
    "Rare":      {"h": 108, "s": 84, "v": 78, "tolerance": 26},   # Green   #20C820
    "Famed":     {"h": 226, "s": 77, "v": 91, "tolerance": 22},   # Blue    #2050E8
    "Legendary": {"h": 0,   "s": 85, "v": 91, "tolerance": 18},   # Red     #E82020
}

RARITY_DISPLAY_HEX = {
    "Common": "#9A9A9A",     # rendered grey in log per spec (de-emphasized)
    "Uncommon": "#E8D020",
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
