"""
loot_parser.py
Rarity classification (HSV-based) and item/text parsing logic for the
TLOPO Loot Tracker.

This module contains NO screen-capture or Tk code so it can be unit
tested / reused independently of the GUI and detector.
"""

import colorsys
import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


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


# Keyword -> category, checked as whole-word matches against the item
# name (longest keyword wins on overlap, e.g. "Throwing Knives" over
# "Knives" alone). Same substring/keyword-matching philosophy as
# normalize_chest_type above -- not an exhaustive TLOPO item taxonomy
# (no such list is available in this repo), just enough coverage over
# common gear-name words to be useful for "does X drop more legendary
# swords than Y"-style aggregate questions. Names that match nothing
# stay uncategorized (category=None) rather than being force-fit into
# a wrong bucket.
ITEM_CATEGORY_KEYWORDS = [
    ("Throwing Knives", "Throwing Knives"),
    ("Blunderbuss", "Blunderbuss"),
    ("Broadsword", "Sword"),
    ("Cutlass", "Sword"),
    ("Sabre", "Sword"),
    ("Rapier", "Sword"),
    ("Sword", "Sword"),
    ("Dagger", "Dagger"),
    ("Repeater", "Gun"),
    ("Pistol", "Gun"),
    ("Musket", "Gun"),
    ("Boots", "Boots"),
    ("Shoes", "Boots"),
    ("Sandals", "Boots"),
    ("Hat", "Hat"),
    ("Bandana", "Hat"),
    ("Tricorne", "Hat"),
    ("Coat", "Coat"),
    ("Jacket", "Coat"),
    ("Vest", "Shirt"),
    ("Shirt", "Shirt"),
    ("Blouse", "Shirt"),
    ("Top", "Shirt"),
    ("Trousers", "Pants"),
    ("Breeches", "Pants"),
    ("Capris", "Pants"),
    ("Shorts", "Pants"),
    ("Skirt", "Pants"),
    ("Ring", "Ring"),
    ("Charm", "Charm"),
    ("Necklace", "Necklace"),
    ("Earring", "Earring"),
    ("Tattoo", "Tattoo"),
    ("Doll", "Doll"),
    ("Spyglass", "Spyglass"),
    ("Staff", "Staff"),
    # Added from real session data (TLOPO_Session_2026-07-09_23-13) --
    # "Miracle Water" and "Faded Sea Chart" were seen uncategorized.
    ("Elixir", "Elixir"),
    ("Tonic", "Elixir"),
    ("Potion", "Elixir"),
    ("Water", "Elixir"),
    ("Chart", "Chart"),
    ("Map", "Chart"),
]

# Coarse, fixed vocabulary that the many granular categories above
# collapse into for cross-category analysis (e.g. "weapon drop rate"
# across every sword/dagger/gun rather than one at a time). The granular
# `category` field is kept as-is, not replaced -- collapsing straight to
# this vocabulary would lose exactly the distinction needed for a
# question like "does X drop more legendary swords than Y" (see
# category_group() below, which adds a second, coarser field alongside
# the granular one instead). "Quest Item" has no granular category
# mapped to it yet -- no evidence for one has shown up in real session
# data so far; it's a valid value here, just unused until one does.
CATEGORY_GROUP: Dict[str, str] = {
    "Sword": "Weapon", "Dagger": "Weapon", "Gun": "Weapon",
    "Throwing Knives": "Weapon", "Blunderbuss": "Weapon", "Staff": "Weapon",
    "Boots": "Clothing", "Hat": "Clothing", "Coat": "Clothing",
    "Shirt": "Clothing", "Pants": "Clothing",
    "Ring": "Accessory", "Charm": "Accessory", "Necklace": "Accessory",
    "Earring": "Accessory", "Tattoo": "Accessory",
    "Doll": "Collectible", "Spyglass": "Collectible",
    "Elixir": "Consumable",
    "Chart": "Treasure",
}


def category_group(category: Optional[str]) -> Optional[str]:
    """Fixed-vocabulary group for a granular category (see CATEGORY_GROUP). None passthrough."""
    if category is None:
        return None
    return CATEGORY_GROUP.get(category)


def canonical_enemy_id(display_name: str) -> str:
    """
    Deterministic, stable machine-readable ID for an enemy display name --
    lowercase, non-alphanumerics collapsed to a single underscore, e.g.
    "General Hex" -> "general_hex". Exists so a database join (see
    enrichment.py) has something stable to key on even if a display name
    ever gets a spelling/formatting tweak later.

    Pure slug of the display name, not a hand-curated ID table -- this
    repo has no such table. If a specific enemy ever needs a hand-picked
    ID that doesn't match its slug (the way a real reference database
    might, e.g. "capt_briney_palifico" instead of "palifico"), that's a
    small lookup dict to add here later, not a reason to block on one now.
    """
    if not display_name:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.strip().lower())
    return slug.strip("_")


def classify_item_category(name: str) -> Optional[str]:
    """
    Coarse item-type tag from a keyword scan over the item name (see
    ITEM_CATEGORY_KEYWORDS). Returns None if nothing matched -- a
    non-match is expected and fine (currency/filler items like "Gold"
    or playing cards, or gear words not yet in the keyword list).
    """
    if not name:
        return None
    best_category, best_len = None, -1
    for keyword, category in ITEM_CATEGORY_KEYWORDS:
        if re.search(r"(?i)\b" + re.escape(keyword) + r"\b", name):
            if len(keyword) > best_len:
                best_category, best_len = category, len(keyword)
    return best_category


# Buckets over LootItem.name_confidence (0-100) so a research consumer can
# weight/filter observations without discarding low-confidence reads
# outright -- see the "don't hide OCR failures" philosophy this module
# already follows elsewhere (untagged items, "(no items read)"). Cutoffs
# are the collaborator's own requested bands, not independently tuned.
CONFIDENCE_TIER_THRESHOLDS = [
    (95.0, "High Confidence"),
    (85.0, "Good"),
    (70.0, "Needs Review"),
]
CONFIDENCE_TIER_FLAG = "Flag"


def confidence_tier(name_confidence: Optional[float]) -> Optional[str]:
    """
    Coarse trust bucket for an OCR confidence score -- see
    CONFIDENCE_TIER_THRESHOLDS. Returns None (not "Flag") when
    name_confidence itself is None, since "unknown confidence" and
    "known to be low confidence" are different things a consumer should
    be able to tell apart.
    """
    if name_confidence is None:
        return None
    for threshold, tier in CONFIDENCE_TIER_THRESHOLDS:
        if name_confidence >= threshold:
            return tier
    return CONFIDENCE_TIER_FLAG


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
    # OCR confidence (0-100) for the name text, from EasyOCR's own
    # per-detection confidence score -- see detector.py _read_loot_window.
    # None if this item wasn't produced by OCR (e.g. constructed in a
    # test or restored from an older save with no confidence recorded).
    name_confidence: Optional[float] = None
    # Coarse item-type tag (Boots/Sword/Ring/...) from
    # classify_item_category below. None if the name didn't match any
    # known category keyword -- kept as None rather than hidden, same
    # "don't hide OCR failures" philosophy as "(no items read)".
    category: Optional[str] = None

    def is_named_tier(self) -> bool:
        return self.rarity in ("Famed", "Legendary")


# Capture-quality classification for a loot/chest observation (see
# session.py Session.log_chest). Deliberately conservative: "Partial" is
# the default for anything with an unreadable confidence, since treating
# an unknown-confidence read as trustworthy would silently overstate data
# quality to a later statistical consumer. "OCR Failure" is reserved for
# a chest that was genuinely confirmed open (we have its chest_type) but
# came back with literally nothing legible -- distinct from a kill that
# never produced a loot popup at all (TLOPO has no guaranteed drop; see
# enrichment.py's "Missed" kill classification, a different concept).
def classify_capture_quality(items: List["LootItem"], gold: int) -> str:
    if not items and not gold:
        return "OCR Failure"
    for item in items:
        if item.name_confidence is None or item.name_confidence < 70.0:
            return "Partial"
    return "Complete"


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
    # Copied in from Session.active_enemy_color / active_location at log
    # time, same way `target` already is -- both are manually set by the
    # player (see tlopo_tracker.py), not auto-detected. No on-screen
    # element for either has been confirmed/calibrated yet (no reference
    # screenshot showing an enemy color-tier indicator or a location HUD),
    # so these stay manual-entry only until one exists.
    enemy_color: Optional[str] = None
    location: Optional[str] = None
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


# ---------------------------------------------------------------------------
# Known boss/enemy names (GitHub issue #7 -- boss nameplate auto-detection)
# ---------------------------------------------------------------------------

# Canonical, correctly-spelled boss names. This is the single source of
# truth for both the GUI's target dropdown (tlopo_tracker.py PRESET_TARGETS
# is built from this) and the pool the health-bar-triggered nameplate OCR
# read snaps against (see detector.py LootDetector._detect_boss_name /
# match_known_boss_name below) -- so an auto-set target is always one of
# these exact spellings, never whatever imperfect text OCR produced.
KNOWN_BOSS_NAMES = [
    "Palifico", "Crash", "Koleniko", "Neban the Silent", "Jimmy Legs",
    "Cicatriz", "Remington the Vicious", "General Darkhart", "General Hex",
    "The Twins (Drench & Drizzle)", "Drench", "Drizzle",
    "El Patron", "Foulberto Smasho", "Jolly Roger",
]

# Lower than ITEM_NAME_FUZZY_MATCH_RATIO (detector.py, 0.75) -- confirmed
# against real screenshots, the boss nameplate reads cleanly ("Remington
# the Vicious" OCR'd exactly, ratio 1.0), but the threshold still needs
# room for a worse read than the loot popup's own text tends to produce
# (smaller font, busier background behind it) without silently rejecting
# a genuine match.
BOSS_NAME_FUZZY_MATCH_RATIO = 0.6


def match_known_boss_name(ocr_text: str, candidates: Optional[List[str]] = None) -> Optional[str]:
    """
    Fuzzy-matches a noisy nameplate OCR read against KNOWN_BOSS_NAMES (or
    a caller-supplied candidate list), returning the correctly-spelled
    name it's most likely trying to say, or None if nothing matches
    closely enough to trust.
    """
    text = ocr_text.strip().lower()
    if not text:
        return None
    pool = candidates if candidates is not None else KNOWN_BOSS_NAMES
    best_name, best_ratio = None, 0.0
    for name in pool:
        ratio = difflib.SequenceMatcher(None, text, name.lower()).ratio()
        if ratio > best_ratio:
            best_name, best_ratio = name, ratio
    if best_ratio >= BOSS_NAME_FUZZY_MATCH_RATIO:
        return best_name
    return None
