"""
session.py
Session state management: per-target tracking, kill counts, chest counts,
rarity breakdowns, and named (Famed/Legendary) item tracking.

Also handles JSON auto-save / restore so data survives a crash.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional

from loot_parser import ChestResult, LootItem, RARITY_ORDER

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

    def skull_rate(self) -> float:
        if self.kills == 0:
            return 0.0
        return (self.skull_chests / self.kills) * 100.0

    def to_dict(self):
        return {
            "name": self.name,
            "kills": self.kills,
            "auto_kills": self.auto_kills,
            "pouches": self.pouches,
            "chests": self.chests,
            "skull_chests": self.skull_chests,
            "rarity_counts": self.rarity_counts,
            "loot_log": self.loot_log,
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
        t.loot_log = d.get("loot_log", [])
        return t


class Session:
    """
    Top-level session state: tracks all targets farmed, the currently
    active target, and named-item (Famed/Legendary) rollups across the
    whole session.
    """

    def __init__(self):
        self.session_start = time.time()
        self.active_target: Optional[str] = None
        self.targets: Dict[str, TargetStats] = {}
        # keyed by lowercased item name -> NamedItemRecord
        self.named_items: Dict[str, NamedItemRecord] = {}
        self._listeners = []  # callables invoked on state change (for GUI refresh)

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
            if item.is_named_tier():
                self._record_named_item(item, target_name)
                named_hits.append(item)

        stats.loot_log.append({
            "timestamp": result.timestamp or datetime.now().strftime("%H:%M:%S"),
            "target": target_name,
            "chest_type": result.chest_type,
            "items": [{"name": i.name, "rarity": i.rarity} for i in result.items],
            "gold": result.gold,
            "kill_number": stats.kills,
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
            entry["items"].extend({"name": i.name, "rarity": i.rarity} for i in extra_items)
            if extra_gold:
                entry["gold"] = extra_gold

        named_hits: List[LootItem] = []
        for item in extra_items:
            if item.rarity in target_stats.rarity_counts:
                target_stats.rarity_counts[item.rarity] += 1
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

    def duration_seconds(self) -> float:
        return time.time() - self.session_start

    # ------------------------------------------------------------------
    # Persistence (autosave / restore)
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "session_start": self.session_start,
            "active_target": self.active_target,
            "targets": {k: v.to_dict() for k, v in self.targets.items()},
            "named_items": {k: v.to_dict() for k, v in self.named_items.items()},
            "saved_at": time.time(),
        }

    @staticmethod
    def from_dict(d: dict) -> "Session":
        s = Session()
        s.session_start = d.get("session_start", time.time())
        s.active_target = d.get("active_target")
        s.targets = {k: TargetStats.from_dict(v) for k, v in d.get("targets", {}).items()}
        s.named_items = {k: NamedItemRecord.from_dict(v) for k, v in d.get("named_items", {}).items()}
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
