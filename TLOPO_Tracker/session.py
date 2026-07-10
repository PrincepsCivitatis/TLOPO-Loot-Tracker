"""
session.py
Session state management: per-target tracking, kill counts, chest counts,
rarity breakdowns, and named (Famed/Legendary) item tracking.

Also handles JSON auto-save / restore so data survives a crash.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional

from loot_parser import (
    ChestResult, LootItem, RARITY_ORDER,
    category_group, classify_capture_quality, confidence_tier,
)

AUTOSAVE_FILENAME = "tlopo_tracker_autosave.json"
AUTOSAVE_MAX_AGE_SECONDS = 8 * 60 * 60  # 8 hours


@dataclass
class NamedItemRecord:
    name: str
    rarity: str
    count: int = 0
    targets: set = field(default_factory=set)

    def to_dict(self):
        d = asdict(self)
        d["targets"] = sorted(self.targets)
        return d

    @staticmethod
    def from_dict(d):
        rec = NamedItemRecord(name=d["name"], rarity=d["rarity"], count=d["count"])
        rec.targets = set(d.get("targets", []))
        return rec


@dataclass
class TargetStats:
    name: str
    kills: int = 0
    # Subset of `kills` that came from the health-bar auto-detector rather
    # than a manual +1/+5/+10/Set click (see Session.add_auto_kill). Kept
    # as a separate running count -- not a replacement for `kills` -- so
    # a miscount from the (heuristic) auto-detector is easy to spot by
    # comparing against manual clicks, rather than being silently mixed
    # into one number.
    auto_kills: int = 0
    pouches: int = 0
    chests: int = 0
    skull_chests: int = 0
    rarity_counts: Dict[str, int] = field(default_factory=lambda: {r: 0 for r in RARITY_ORDER})
    loot_log: List[dict] = field(default_factory=list)  # serialized chest results
    # Full item inventory -- EVERY item seen against this target, not just
    # Famed/Legendary (see Session.named_items for that narrower rollup).
    # Keyed by lowercased item name so "Ruby" and "ruby" (a re-OCR
    # spelling variance) count as the same item; display_name/rarity/
    # category are captured from the first sighting for presentation.
    item_counts: Dict[str, int] = field(default_factory=dict)
    item_display_names: Dict[str, str] = field(default_factory=dict)
    item_rarity: Dict[str, Optional[str]] = field(default_factory=dict)
    item_category: Dict[str, Optional[str]] = field(default_factory=dict)
    item_category_group: Dict[str, Optional[str]] = field(default_factory=dict)

    def skull_rate(self) -> float:
        if self.kills == 0:
            return 0.0
        return (self.skull_chests / self.kills) * 100.0

    def record_item(self, name: str, rarity: Optional[str], category: Optional[str],
                     group: Optional[str] = None) -> None:
        key = name.strip().lower()
        if not key:
            return
        self.item_counts[key] = self.item_counts.get(key, 0) + 1
        self.item_display_names.setdefault(key, name.strip())
        self.item_rarity.setdefault(key, rarity)
        self.item_category.setdefault(key, category)
        self.item_category_group.setdefault(key, group)

    def to_dict(self):
        return {
            "name": self.name,
            "kills": self.kills,
            "auto_kills": self.auto_kills,
            "pouches": self.pouches,
            "chests": self.chests,
            "skull_chests": self.skull_chests,
            "rarity_counts": self.rarity_counts,
            "loot_log": list(self.loot_log),
            "item_counts": self.item_counts,
            "item_display_names": self.item_display_names,
            "item_rarity": self.item_rarity,
            "item_category": self.item_category,
            "item_category_group": self.item_category_group,
        }

    @staticmethod
    def from_dict(d):
        t = TargetStats(name=d["name"])
        t.kills = d.get("kills", 0)
        t.auto_kills = d.get("auto_kills", 0)
        t.pouches = d.get("pouches", 0)
        t.chests = d.get("chests", 0)
        t.skull_chests = d.get("skull_chests", 0)
        loaded_rarity_counts = d.get("rarity_counts", {})
        t.rarity_counts = {r: loaded_rarity_counts.get(r, 0) for r in RARITY_ORDER}
        t.loot_log = list(d.get("loot_log", []))
        t.item_counts = dict(d.get("item_counts", {}))
        t.item_display_names = dict(d.get("item_display_names", {}))
        t.item_rarity = dict(d.get("item_rarity", {}))
        t.item_category = dict(d.get("item_category", {}))
        t.item_category_group = dict(d.get("item_category_group", {}))
        return t


class Session:
    """
    Top-level session state: tracks all targets farmed, the currently
    active target, and named-item (Famed/Legendary) rollups across the
    whole session.
    """

    def __init__(self):
        self.session_start = time.time()
        # Stable per-session identifier, format "YYYYMMDD-HHMM-xxxx" (the
        # collaborator's requested "Session 20260709-1645" shape, plus a
        # short random suffix so two sessions started in the same minute
        # -- e.g. a crash-restart -- don't collide). Generated once here
        # and persisted/restored via to_dict/from_dict so it stays the
        # same across an autosave restore, not regenerated on load.
        self.session_id = f"{datetime.now().strftime('%Y%m%d-%H%M')}-{uuid.uuid4().hex[:4]}"
        self.active_target: Optional[str] = None
        # Manually set by the player (see tlopo_tracker.py) -- no on-screen
        # element for either has been confirmed/calibrated yet, so these
        # are not auto-detected (see loot_parser.ChestResult.enemy_color /
        # .location docstring note).
        self.active_enemy_color: Optional[str] = None
        self.active_location: Optional[str] = None
        self.targets: Dict[str, TargetStats] = {}
        # keyed by lowercased item name -> NamedItemRecord
        self.named_items: Dict[str, NamedItemRecord] = {}
        # One dict per observation -- a kill or a chest/loot event -- in
        # the order they happened. See log_chest/amend_chest/add_auto_kill
        # for what gets appended and why (e.g. why bulk +5/+10 clicks do
        # NOT get exploded into synthetic per-kill events here).
        self.raw_events: List[dict] = []
        # Running counter behind observation_id (see _next_observation_id)
        # -- persisted/restored alongside raw_events so a restored session
        # never reissues an ID that's already on an earlier event.
        self._observation_seq: int = 0
        self._listeners = []  # callables invoked on state change (for GUI refresh)

    def _next_observation_id(self) -> str:
        """
        Unique, monotonically increasing ID for one raw_events entry --
        format "YYYYMMDD-HHMMSS-NNNN". Meant to let a screenshot, video
        timestamp, or later re-analysis reference the exact same
        observation; never reused, never mutated once assigned.
        """
        self._observation_seq += 1
        return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{self._observation_seq:04d}"

    # ------------------------------------------------------------------
    # Listener / GUI hook plumbing
    # ------------------------------------------------------------------
    def add_listener(self, fn):
        self._listeners.append(fn)

    def _notify(self, event: str, payload=None):
        for fn in list(self._listeners):
            try:
                fn(event, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Target management
    # ------------------------------------------------------------------
    def set_target(self, name: str):
        name = name.strip()
        if not name:
            return
        if name not in self.targets:
            self.targets[name] = TargetStats(name=name)
        self.active_target = name
        self._notify("target_changed", name)

    def get_active_stats(self) -> Optional[TargetStats]:
        if self.active_target is None:
            return None
        return self.targets.get(self.active_target)

    def set_enemy_color(self, value: str):
        self.active_enemy_color = value.strip() or None

    def set_location(self, value: str):
        self.active_location = value.strip() or None

    # ------------------------------------------------------------------
    # Kill counting
    # ------------------------------------------------------------------
    def add_kills(self, amount: int):
        stats = self._ensure_active()
        if stats is None:
            return
        stats.kills = max(0, stats.kills + amount)
        self._notify("kills_changed", stats.kills)

    def set_kills(self, amount: int):
        stats = self._ensure_active()
        if stats is None:
            return
        stats.kills = max(0, amount)
        self._notify("kills_changed", stats.kills)

    def add_auto_kill(self):
        """
        Records a kill detected automatically by the boss health-bar
        tracker (detector.py LootDetector.on_kill_detected), rather than
        a manual +1/+5/+10/Set click. Still increments the same `kills`
        total the rest of the app reads, but also bumps `auto_kills` so
        the two can be compared -- see TargetStats.auto_kills.
        """
        stats = self._ensure_active()
        if stats is None:
            return
        stats.kills = max(0, stats.kills + 1)
        stats.auto_kills += 1
        self.raw_events.append({
            "observation_id": self._next_observation_id(),
            "event_type": "kill",
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "target": stats.name,
            "enemy_color": self.active_enemy_color,
            "location": self.active_location,
            "kill_number": stats.kills,
        })
        self._notify("kills_changed", stats.kills)

    def _ensure_active(self) -> Optional[TargetStats]:
        if self.active_target is None:
            return None
        return self.targets.setdefault(self.active_target, TargetStats(name=self.active_target))

    # ------------------------------------------------------------------
    # Chest / loot logging
    # ------------------------------------------------------------------
    def log_chest(self, result: ChestResult) -> List[LootItem]:
        """
        Record a detected chest result against the active target (or the
        result's own .target if provided). Returns the list of Famed/
        Legendary items found (for triggering GUI alerts).
        """
        target_name = result.target or self.active_target
        if not target_name:
            target_name = "Unknown"
            self.set_target(target_name)
        stats = self.targets.setdefault(target_name, TargetStats(name=target_name))

        key = result.chest_key()
        if key == "pouch":
            stats.pouches += 1
        elif key == "chest":
            stats.chests += 1
        else:
            stats.skull_chests += 1

        named_hits: List[LootItem] = []
        for item in result.items:
            if item.rarity in stats.rarity_counts:
                stats.rarity_counts[item.rarity] += 1
            stats.record_item(item.name, item.rarity, item.category, category_group(item.category))
            if item.is_named_tier():
                self._record_named_item(item, target_name)
                named_hits.append(item)

        timestamp = result.timestamp or datetime.now().strftime("%H:%M:%S")
        item_dicts = [
            {"name": i.name, "rarity": i.rarity, "category": i.category,
             "category_group": category_group(i.category),
             "name_confidence": i.name_confidence, "confidence_tier": confidence_tier(i.name_confidence)}
            for i in result.items
        ]
        stats.loot_log.append({
            "timestamp": timestamp,
            "target": target_name,
            "chest_type": result.chest_type,
            "items": item_dicts,
            "gold": result.gold,
            "kill_number": stats.kills,
            "session_id": result.session_id,
        })

        self.raw_events.append({
            "observation_id": self._next_observation_id(),
            "event_type": "chest",
            "timestamp": timestamp,
            "target": target_name,
            "enemy_color": result.enemy_color,
            "location": result.location,
            "kill_number": stats.kills,
            "chest_type": result.chest_type,
            "capture_quality": classify_capture_quality(result.items, result.gold),
            "items": item_dicts,
            "gold": result.gold,
            "session_id": result.session_id,
        })

        self._notify("chest_logged", result)
        return named_hits

    def amend_chest(self, session_id: str, extra_items: List[LootItem], extra_gold: int) -> List[LootItem]:
        """
        Adds late-arriving content to an ALREADY-logged chest, identified
        by session_id, without incrementing the chest-open counters again
        -- this is the same physical chest, not a new one. See
        detector.py's LootDetector._finalize_session for why a chest can
        need amending (an item that hadn't finished rendering on the very
        first frame, or a higher gold amount that only became readable
        later) and where session_id comes from. extra_gold of 0 means
        "no gold correction needed"; otherwise it's the corrected total
        for this chest, replacing (not adding to) what was first logged.

        Returns the list of Famed/Legendary items among extra_items (for
        triggering GUI alerts), same contract as log_chest.
        """
        target_stats = None
        entry = None
        for stats in self.targets.values():
            for row in stats.loot_log:
                if row.get("session_id") == session_id:
                    target_stats = stats
                    entry = row
                    break
            if entry is not None:
                break

        if target_stats is None:
            # No matching row (shouldn't normally happen) -- fall back to
            # the active target so this loot is never silently dropped,
            # even without a loot_log row to attach it to for export.
            target_stats = self._ensure_active()
            if target_stats is None:
                return []
        elif entry is not None:
            entry["items"].extend(
                {"name": i.name, "rarity": i.rarity, "category": i.category,
                 "category_group": category_group(i.category),
                 "name_confidence": i.name_confidence, "confidence_tier": confidence_tier(i.name_confidence)}
                for i in extra_items
            )
            if extra_gold:
                entry["gold"] = extra_gold

        for raw_event in self.raw_events:
            if raw_event.get("event_type") == "chest" and raw_event.get("session_id") == session_id:
                raw_event["items"].extend(
                    {"name": i.name, "rarity": i.rarity, "category": i.category,
                     "category_group": category_group(i.category),
                     "name_confidence": i.name_confidence, "confidence_tier": confidence_tier(i.name_confidence)}
                    for i in extra_items
                )
                if extra_gold:
                    raw_event["gold"] = extra_gold
                # Re-derive capture_quality from the now-more-complete
                # picture -- more content just arrived, so a quality
                # judgment made before it existed (e.g. "OCR Failure" on
                # an initially-empty read) can go stale otherwise. Mirrors
                # loot_parser.classify_capture_quality's own rule directly
                # against the already-serialized item dicts here, since
                # the merged item list is no longer LootItem objects.
                confidences = [it.get("name_confidence") for it in raw_event["items"]]
                if not raw_event["items"] and not raw_event["gold"]:
                    raw_event["capture_quality"] = "OCR Failure"
                elif any(c is None or c < 70.0 for c in confidences):
                    raw_event["capture_quality"] = "Partial"
                else:
                    raw_event["capture_quality"] = "Complete"
                break

        named_hits: List[LootItem] = []
        for item in extra_items:
            if item.rarity in target_stats.rarity_counts:
                target_stats.rarity_counts[item.rarity] += 1
            target_stats.record_item(item.name, item.rarity, item.category, category_group(item.category))
            if item.is_named_tier():
                self._record_named_item(item, target_stats.name)
                named_hits.append(item)

        self._notify("chest_amended", (session_id, extra_items, extra_gold))
        return named_hits

    def _record_named_item(self, item: LootItem, target_name: str):
        key = item.name.strip().lower()
        if not key:
            return
        rec = self.named_items.get(key)
        if rec is None:
            rec = NamedItemRecord(name=item.name.strip(), rarity=item.rarity)
            self.named_items[key] = rec
        rec.count += 1
        rec.targets.add(target_name)
        self._notify("named_item", rec)

    # ------------------------------------------------------------------
    # Aggregation helpers
    # ------------------------------------------------------------------
    def session_totals(self) -> TargetStats:
        total = TargetStats(name="TOTAL")
        for stats in self.targets.values():
            total.kills += stats.kills
            total.auto_kills += stats.auto_kills
            total.pouches += stats.pouches
            total.chests += stats.chests
            total.skull_chests += stats.skull_chests
            for r in RARITY_ORDER:
                total.rarity_counts[r] += stats.rarity_counts.get(r, 0)
        return total

    def named_items_by_rarity(self, rarity: str) -> List[NamedItemRecord]:
        items = [r for r in self.named_items.values() if r.rarity == rarity]
        items.sort(key=lambda r: r.count, reverse=True)
        return items

    def session_item_counts(self) -> Dict[str, dict]:
        """
        Session-wide item inventory across ALL targets -- every item ever
        seen (not just Famed/Legendary; see named_items for that
        narrower rollup), aggregated by name. Keyed by lowercased item
        name; each value is {"name": display name, "rarity": ...,
        "category": ..., "count": total across every target}.
        """
        totals: Dict[str, dict] = {}
        for stats in self.targets.values():
            for key, count in stats.item_counts.items():
                entry = totals.setdefault(key, {
                    "name": stats.item_display_names.get(key, key),
                    "rarity": stats.item_rarity.get(key),
                    "category": stats.item_category.get(key),
                    "category_group": stats.item_category_group.get(key),
                    "count": 0,
                })
                entry["count"] += count
        return totals

    def duration_seconds(self) -> float:
        return time.time() - self.session_start

    # ------------------------------------------------------------------
    # Persistence (autosave / restore)
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "session_start": self.session_start,
            "session_id": self.session_id,
            "active_target": self.active_target,
            "active_enemy_color": self.active_enemy_color,
            "active_location": self.active_location,
            "targets": {k: v.to_dict() for k, v in self.targets.items()},
            "named_items": {k: v.to_dict() for k, v in self.named_items.items()},
            # Copy (not alias) -- to_dict() is also called by autosave
            # while the live session keeps running and appending more
            # events; without a copy here, a restored Session built from
            # this same dict later (from_dict) would share the exact
            # list object with whatever session produced it, so mutating
            # one would silently corrupt the other.
            "raw_events": list(self.raw_events),
            "observation_seq": self._observation_seq,
            "saved_at": time.time(),
        }

    @staticmethod
    def from_dict(d: dict) -> "Session":
        s = Session()
        s.session_start = d.get("session_start", time.time())
        # Restore the ORIGINAL session_id rather than keeping the fresh
        # one generated by Session() above -- an autosave restore is
        # continuing the same session, not starting a new one.
        s.session_id = d.get("session_id", s.session_id)
        s.active_target = d.get("active_target")
        s.active_enemy_color = d.get("active_enemy_color")
        s.active_location = d.get("active_location")
        s.targets = {k: TargetStats.from_dict(v) for k, v in d.get("targets", {}).items()}
        s.named_items = {k: NamedItemRecord.from_dict(v) for k, v in d.get("named_items", {}).items()}
        s.raw_events = list(d.get("raw_events", []))
        s._observation_seq = d.get("observation_seq", len(s.raw_events))
        return s

    def autosave(self, folder: str):
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, AUTOSAVE_FILENAME)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            # Autosave must never crash the app.
            pass

    @staticmethod
    def find_recent_autosave(folder: str) -> Optional[str]:
        path = os.path.join(folder, AUTOSAVE_FILENAME)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_at = data.get("saved_at", 0)
            if time.time() - saved_at > AUTOSAVE_MAX_AGE_SECONDS:
                return None
            return path
        except Exception:
            return None

    @staticmethod
    def load(path: str) -> Optional["Session"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Session.from_dict(data)
        except Exception:
            return None

    def reset(self):
        self.__init__()
