"""
tlopo_tracker.py
TLOPO Loot Tracker - main application entry point and GUI.

A compact, always-on-top tkinter tracker that runs alongside The Legend of
Pirates Online, auto-detects loot popup windows via screen capture + OCR,
classifies item rarity by text color, and logs everything to a session
you can export to Excel or plain text.

This app only reads your screen. It never touches game files or the network.
"""

import copy
import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime
from typing import List
from tkinter import (
    Tk, Toplevel, Frame, Label, Button, Entry, StringVar, IntVar,
    Listbox, Scrollbar, Text, END, DISABLED, NORMAL, messagebox, ttk, BOTH,
    LEFT, RIGHT, TOP, BOTTOM, X, Y, VERTICAL, HORIZONTAL, W, E,
    Checkbutton, BooleanVar, Scale,
)

from loot_parser import (
    ChestResult, LootItem, RARITY_ORDER, RARITY_DISPLAY_HEX,
    DEFAULT_HSV_TARGETS,
)
from session import Session
from exporter import export_to_excel, export_to_text, default_export_folder
from detector import LootDetector, DetectorSettings, DEFAULT_PARCHMENT_RGB, DEFAULT_PARCHMENT_TOLERANCE

APP_TITLE = "TLOPO Loot Tracker"
WINDOW_W, WINDOW_H = 440, 750

# Detection polling interval bounds. Lowered from an earlier 200ms floor
# per player feedback (GitHub issue #3) -- fast-looting playstyles (e.g.
# bilge farming, or aggro-then-burst strategies that drop many chests
# at once) can open/close containers faster than 200ms allowed the
# tracker to catch. See detector.py for the matching backend floor.
MIN_POLL_INTERVAL_MS = 10
MAX_POLL_INTERVAL_MS = 5000

# Bounds for the post-chest-close cooldown (see DetectorSettings.post_close_cooldown_s
# in detector.py). Used to briefly ignore a spot right after a popup closes so a
# fading-out animation's leftover pixels aren't mistaken for a new popup there.
MIN_CLOSE_COOLDOWN_MS = 0
MAX_CLOSE_COOLDOWN_MS = 3000

PRESET_TARGETS = [
    "Palifico", "Crash", "Koleniko", "Neban the Silent", "Jimmy Legs",
    "Cicatriz", "Remington the Vicious", "General Darkhart", "General Hex",
    "The Twins (Drench & Drizzle)", "Drench", "Drizzle",
    "El Patron", "Foulberto Smasho", "Jolly Roger",
    "Gold Room Enemies", "Cursed Caverns Enemies",
    "Forsaken Shallows Enemies", "El Patron's Mine Enemies",
    "Raven's Cove Enemies", "Custom...",
]

BG = "#20242b"
PANEL_BG = "#262b33"
FG = "#e8e6df"
ACCENT = "#d8b25a"
GREY = "#9a9a9a"

CONFIG_FILENAME = "tlopo_tracker_settings.json"


class ToolTip:
    """Minimal tooltip helper (used sparingly for icon buttons)."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        self.tip = None

    def _show(self, _evt=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 20
        self.tip = Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        Label(self.tip, text=self.text, background="#333", foreground="white",
              relief="solid", borderwidth=1, padx=4, pady=2).pack()

    def _hide(self, _evt=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class TLOPOTrackerApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.minsize(380, 560)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self._position_top_right()

        self.session = Session()
        self.event_queue: "queue.Queue" = queue.Queue()

        self.app_dir = os.path.dirname(os.path.abspath(__file__))
        self.settings = self._load_settings()

        self.detector: LootDetector = None
        self._detector_started = False

        self._build_style()
        self._maybe_restore_session()
        self._build_ui()

        self.detector = LootDetector(
            on_chest_detected=self._detector_on_chest,
            on_status_change=self._detector_on_status,
            on_error=self._detector_on_error,
            settings=DetectorSettings(
                poll_interval_ms=self.settings.get("poll_interval_ms", 500),
                post_close_cooldown_s=self.settings.get("close_cooldown_ms", 400) / 1000.0,
                hsv_targets=self.settings.get("hsv_targets", DEFAULT_HSV_TARGETS),
                parchment_rgb=tuple(self.settings.get("parchment_rgb", DEFAULT_PARCHMENT_RGB)),
                parchment_tolerance=self.settings.get("parchment_tolerance", DEFAULT_PARCHMENT_TOLERANCE),
            ),
        )
        self.detector.active_target_getter = lambda: self.session.active_target or ""
        self.detector.kill_number_getter = lambda: (
            self.session.get_active_stats().kills if self.session.get_active_stats() else 0
        )

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._start_background_threads()
        self._tick_ui()

    # ------------------------------------------------------------------
    # Window placement / styling
    # ------------------------------------------------------------------
    def _position_top_right(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        x = sw - WINDOW_W - 20
        y = 20
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=PANEL_BG, background=PANEL_BG)

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------
    def _settings_path(self):
        return os.path.join(self.app_dir, CONFIG_FILENAME)

    def _load_settings(self):
        default = {
            "poll_interval_ms": 500,
            "close_cooldown_ms": 400,
            "hide_common": True,
            "hide_uncommon": False,
            "hsv_targets": copy.deepcopy(DEFAULT_HSV_TARGETS),
            "parchment_rgb": list(DEFAULT_PARCHMENT_RGB),
            "parchment_tolerance": DEFAULT_PARCHMENT_TOLERANCE,
            "export_folder": default_export_folder(),
        }
        path = self._settings_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                default.update(loaded)
            except Exception:
                pass
        return default

    def _save_settings(self):
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Session restore
    # ------------------------------------------------------------------
    def _maybe_restore_session(self):
        folder = self.settings.get("export_folder") or default_export_folder()
        autosave_dir = self.app_dir
        found = Session.find_recent_autosave(autosave_dir)
        if not found:
            return
        try:
            answer = messagebox.askyesno(
                APP_TITLE,
                "A previous session was found from within the last 8 hours.\n\n"
                "Would you like to restore it?",
            )
        except Exception:
            answer = False
        if answer:
            restored = Session.load(found)
            if restored:
                self.session = restored

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        self._build_top_bar()

        canvas_frame = Frame(self.root, bg=BG)
        canvas_frame.pack(fill=BOTH, expand=True)

        import tkinter as tk
        self._canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        vscroll = Scrollbar(canvas_frame, orient=VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=RIGHT, fill=Y)
        self._canvas.pack(side=LEFT, fill=BOTH, expand=True)

        self._scroll_frame = Frame(self._canvas, bg=BG)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")

        def _on_configure(_evt):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._scroll_frame.bind("<Configure>", _on_configure)

        def _on_canvas_resize(evt):
            self._canvas.itemconfig(self._canvas_window, width=evt.width)
        self._canvas.bind("<Configure>", _on_canvas_resize)

        def _on_mousewheel(evt):
            self._canvas.yview_scroll(int(-1 * (evt.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _on_mousewheel)

        parent = self._scroll_frame
        self._build_status_bar(parent)
        self._build_target_selector(parent)
        self._build_kill_chest_counters(parent)
        self._build_loot_log(parent)
        self._build_named_items_panel(parent)
        self._build_session_summary(parent)
        self._build_export_controls(parent)

    def _build_top_bar(self):
        bar = Frame(self.root, bg=BG)
        bar.pack(fill=X, padx=8, pady=(6, 0))
        Label(bar, text=APP_TITLE, bg=BG, fg=ACCENT, font=("Segoe UI", 12, "bold")).pack(side=LEFT)
        gear = Button(bar, text="⚙", command=self._open_settings, bg=BG, fg=FG,
                      relief="flat", font=("Segoe UI", 12), cursor="hand2")
        gear.pack(side=RIGHT)
        ToolTip(gear, "Settings")

    def _build_status_bar(self, parent):
        frame = Frame(parent, bg=PANEL_BG)
        frame.pack(fill=X, padx=8, pady=6)
        self.status_var = StringVar(value="Waiting for TLOPO...")
        self.status_label = Label(frame, textvariable=self.status_var, bg=PANEL_BG, fg=GREY,
                                   font=("Segoe UI", 9), anchor=W, padx=8, pady=4)
        self.status_label.pack(fill=X)

    # -- Target selector -------------------------------------------------
    def _build_target_selector(self, parent):
        frame = Frame(parent, bg=PANEL_BG)
        frame.pack(fill=X, padx=8, pady=4)

        Label(frame, text="Current Target:", bg=PANEL_BG, fg=FG,
              font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky=W, padx=6, pady=(6, 2))

        self.target_var = StringVar(value=PRESET_TARGETS[0])
        self.target_combo = ttk.Combobox(frame, textvariable=self.target_var,
                                          values=PRESET_TARGETS, state="readonly", width=26)
        self.target_combo.grid(row=1, column=0, sticky=W, padx=6)
        self.target_combo.bind("<<ComboboxSelected>>", self._on_target_combo_change)

        self.custom_target_var = StringVar()
        self.custom_target_entry = Entry(frame, textvariable=self.custom_target_var, width=28)
        # only shown when "Custom..." selected

        self.set_target_btn = Button(frame, text="Set Target", command=self._on_set_target,
                                      bg=ACCENT, fg="#20242b", relief="flat", cursor="hand2")
        self.set_target_btn.grid(row=1, column=1, padx=6)

        self.active_target_label = Label(frame, text="Farming: (none selected)", bg=PANEL_BG,
                                          fg=ACCENT, font=("Segoe UI", 10, "bold"))
        self.active_target_label.grid(row=3, column=0, columnspan=2, sticky=W, padx=6, pady=(4, 8))

        self._target_frame = frame

    def _on_target_combo_change(self, _evt=None):
        if self.target_var.get() == "Custom...":
            self.custom_target_entry.grid(row=2, column=0, sticky=W, padx=6, pady=(2, 4))
        else:
            self.custom_target_entry.grid_forget()

    def _on_set_target(self):
        name = self.target_var.get()
        if name == "Custom...":
            name = self.custom_target_var.get().strip()
            if not name:
                messagebox.showwarning(APP_TITLE, "Type a custom target name first.")
                return
        self.session.set_target(name)
        self.active_target_label.config(text=f"Farming: {name}")
        self._refresh_all()

    # -- Kill & chest counters -------------------------------------------
    def _build_kill_chest_counters(self, parent):
        frame = Frame(parent, bg=PANEL_BG)
        frame.pack(fill=X, padx=8, pady=4)

        Label(frame, text="Kills", bg=PANEL_BG, fg=FG,
              font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky=W, padx=6, pady=(6, 0))
        self.kills_var = StringVar(value="0")
        Label(frame, textvariable=self.kills_var, bg=PANEL_BG, fg=ACCENT,
              font=("Segoe UI", 16, "bold")).grid(row=1, column=0, sticky=W, padx=6)

        btn_frame = Frame(frame, bg=PANEL_BG)
        btn_frame.grid(row=1, column=1, sticky=E, padx=6)
        for label, amount in [("+1", 1), ("+5", 5), ("+10", 10)]:
            Button(btn_frame, text=label, width=4, command=lambda a=amount: self._add_kills(a),
                   bg="#3a4150", fg=FG, relief="flat", cursor="hand2").pack(side=LEFT, padx=2)

        set_frame = Frame(frame, bg=PANEL_BG)
        set_frame.grid(row=2, column=0, columnspan=2, sticky=W, padx=6, pady=(4, 8))
        Label(set_frame, text="Set kills:", bg=PANEL_BG, fg=GREY).pack(side=LEFT)
        self.set_kills_var = StringVar()
        Entry(set_frame, textvariable=self.set_kills_var, width=8).pack(side=LEFT, padx=4)
        Button(set_frame, text="Set", command=self._on_set_kills, bg="#3a4150", fg=FG,
               relief="flat", cursor="hand2").pack(side=LEFT)

        sep = Frame(frame, bg="#3a4150", height=1)
        sep.grid(row=3, column=0, columnspan=2, sticky="ew", padx=6, pady=4)

        Label(frame, text="Auto-Detected Chests (read-only)", bg=PANEL_BG, fg=GREY,
              font=("Segoe UI", 8, "italic")).grid(row=4, column=0, columnspan=2, sticky=W, padx=6)

        self.pouches_var = StringVar(value="Pouches: 0")
        self.chests_var = StringVar(value="Chests: 0")
        self.skulls_var = StringVar(value="Skull Chests: 0")
        self.skull_rate_var = StringVar(value="0.0% skull rate")

        Label(frame, textvariable=self.pouches_var, bg=PANEL_BG, fg=FG).grid(row=5, column=0, sticky=W, padx=6)
        Label(frame, textvariable=self.chests_var, bg=PANEL_BG, fg=FG).grid(row=6, column=0, sticky=W, padx=6)
        Label(frame, textvariable=self.skulls_var, bg=PANEL_BG, fg="#f0c060",
              font=("Segoe UI", 9, "bold")).grid(row=7, column=0, sticky=W, padx=6)
        Label(frame, textvariable=self.skull_rate_var, bg=PANEL_BG, fg=ACCENT,
              font=("Segoe UI", 9, "bold")).grid(row=7, column=1, sticky=E, padx=6, pady=(0, 8))

    def _add_kills(self, amount):
        if self.session.active_target is None:
            messagebox.showwarning(APP_TITLE, "Set a target first.")
            return
        self.session.add_kills(amount)
        self._refresh_all()

    def _on_set_kills(self):
        try:
            val = int(self.set_kills_var.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Enter a whole number.")
            return
        if self.session.active_target is None:
            messagebox.showwarning(APP_TITLE, "Set a target first.")
            return
        self.session.set_kills(val)
        self._refresh_all()

    # -- Loot log ----------------------------------------------------------
    def _build_loot_log(self, parent):
        frame = Frame(parent, bg=PANEL_BG)
        frame.pack(fill=X, padx=8, pady=4)
        Label(frame, text="Loot Log", bg=PANEL_BG, fg=FG,
              font=("Segoe UI", 9, "bold")).pack(anchor=W, padx=6, pady=(6, 2))

        text_frame = Frame(frame, bg=PANEL_BG)
        text_frame.pack(fill=X, padx=6, pady=(0, 6))
        self.loot_text = Text(text_frame, height=10, bg="#15181d", fg=FG, wrap="word",
                               relief="flat", font=("Consolas", 9), state=DISABLED)
        scroll = Scrollbar(text_frame, command=self.loot_text.yview)
        self.loot_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=RIGHT, fill=Y)
        self.loot_text.pack(side=LEFT, fill=BOTH, expand=True)

        for rarity in RARITY_ORDER:
            self.loot_text.tag_configure(rarity, foreground=RARITY_DISPLAY_HEX[rarity])
        self.loot_text.tag_configure("Famed_bold", foreground=RARITY_DISPLAY_HEX["Famed"], font=("Consolas", 9, "bold"))
        self.loot_text.tag_configure("Legendary_row", foreground=RARITY_DISPLAY_HEX["Legendary"],
                                      font=("Consolas", 9, "bold"), background="#3a1010")
        self.loot_text.tag_configure("meta", foreground=GREY)

    # -- Named items panel --------------------------------------------------
    def _build_named_items_panel(self, parent):
        frame = Frame(parent, bg=PANEL_BG)
        frame.pack(fill=X, padx=8, pady=4)

        self.famed_header_var = StringVar(value="FAMED DROPS — 0 total")
        Label(frame, textvariable=self.famed_header_var, bg=PANEL_BG, fg=RARITY_DISPLAY_HEX["Famed"],
              font=("Segoe UI", 9, "bold")).pack(anchor=W, padx=6, pady=(6, 0))
        self.famed_listbox = Listbox(frame, height=4, bg="#15181d", fg=RARITY_DISPLAY_HEX["Famed"],
                                      relief="flat", font=("Consolas", 9), selectmode="browse")
        self.famed_listbox.pack(fill=X, padx=6, pady=(2, 6))

        self.legendary_header_var = StringVar(value="LEGENDARY DROPS — 0 total")
        Label(frame, textvariable=self.legendary_header_var, bg=PANEL_BG, fg=RARITY_DISPLAY_HEX["Legendary"],
              font=("Segoe UI", 9, "bold")).pack(anchor=W, padx=6, pady=(0, 0))
        self.legendary_listbox = Listbox(frame, height=4, bg="#15181d", fg=RARITY_DISPLAY_HEX["Legendary"],
                                          relief="flat", font=("Consolas", 9, "bold"), selectmode="browse")
        self.legendary_listbox.pack(fill=X, padx=6, pady=(2, 6))

    # -- Session summary ------------------------------------------------
    def _build_session_summary(self, parent):
        frame = Frame(parent, bg=PANEL_BG)
        frame.pack(fill=X, padx=8, pady=4)
        Label(frame, text="Session Summary", bg=PANEL_BG, fg=FG,
              font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=2, sticky=W, padx=6, pady=(6, 2))

        self.current_summary_var = StringVar(value="Current target: —")
        Label(frame, textvariable=self.current_summary_var, bg=PANEL_BG, fg=FG, justify=LEFT,
              font=("Consolas", 8)).grid(row=1, column=0, columnspan=2, sticky=W, padx=6)

        self.total_summary_var = StringVar(value="All targets: —")
        Label(frame, textvariable=self.total_summary_var, bg=PANEL_BG, fg=FG, justify=LEFT,
              font=("Consolas", 8)).grid(row=2, column=0, columnspan=2, sticky=W, padx=6, pady=(4, 4))

        self.duration_var = StringVar(value="Session Duration: 00:00:00")
        Label(frame, textvariable=self.duration_var, bg=PANEL_BG, fg=ACCENT,
              font=("Segoe UI", 9, "bold")).grid(row=3, column=0, columnspan=2, sticky=W, padx=6, pady=(0, 8))

    # -- Export / control buttons ----------------------------------------
    def _build_export_controls(self, parent):
        frame = Frame(parent, bg=BG)
        frame.pack(fill=X, padx=8, pady=(4, 12))

        row1 = Frame(frame, bg=BG)
        row1.pack(fill=X, pady=2)
        Button(row1, text="Export to Excel", command=self._on_export_excel, bg="#2e7d32", fg="white",
               relief="flat", cursor="hand2").pack(side=LEFT, expand=True, fill=X, padx=2)
        Button(row1, text="Export to Text", command=self._on_export_text, bg="#455a64", fg="white",
               relief="flat", cursor="hand2").pack(side=LEFT, expand=True, fill=X, padx=2)

        row2 = Frame(frame, bg=BG)
        row2.pack(fill=X, pady=2)
        Button(row2, text="New Target", command=self._on_new_target, bg="#3a4150", fg=FG,
               relief="flat", cursor="hand2").pack(side=LEFT, expand=True, fill=X, padx=2)
        Button(row2, text="Reset Session", command=self._on_reset_session, bg="#8e2424", fg="white",
               relief="flat", cursor="hand2").pack(side=LEFT, expand=True, fill=X, padx=2)

    # ------------------------------------------------------------------
    # Detector event handling (marshaled through a thread-safe queue)
    # ------------------------------------------------------------------
    def _detector_on_chest(self, result: ChestResult):
        self.event_queue.put(("chest", result))

    def _detector_on_status(self, text: str):
        self.event_queue.put(("status", text))

    def _detector_on_error(self, text: str):
        self.event_queue.put(("error", text))

    def _start_background_threads(self):
        try:
            self.detector.start()
        except Exception as e:
            self.status_var.set(f"Detector failed to start: {e}")

    # ------------------------------------------------------------------
    # Main UI tick loop -- processes queued detector events, updates
    # timers, and handles periodic autosave. Runs on the Tk main thread.
    # ------------------------------------------------------------------
    _last_autosave = 0.0

    def _tick_ui(self):
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "chest":
                    self._handle_chest_detected(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "error":
                    self.status_var.set(f"Error: {payload}")
        except queue.Empty:
            pass

        self.duration_var.set(
            "Session Duration: " + self._format_duration(self.session.duration_seconds())
        )

        now = time.time()
        if now - self._last_autosave > 60:
            self._last_autosave = now
            threading.Thread(target=self._autosave_async, daemon=True).start()

        self.root.after(200, self._tick_ui)

    def _autosave_async(self):
        try:
            self.session.autosave(self.app_dir)
        except Exception:
            pass

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ------------------------------------------------------------------
    # Chest detection -> session log -> UI refresh
    # ------------------------------------------------------------------
    def _handle_chest_detected(self, result: ChestResult):
        if result.is_amendment:
            self._handle_chest_amended(result)
            return

        if not result.target:
            result.target = self.session.active_target or "Unknown"
        named_hits = self.session.log_chest(result)

        self._append_loot_log_row(result)
        self._flash_ui()
        self._refresh_all()

        for item in named_hits:
            if item.rarity == "Legendary":
                self._show_legendary_alert(item.name)

    def _handle_chest_amended(self, result: ChestResult):
        """
        Late-arriving correction to an already-logged chest (see
        detector.py's session accumulation / LootDetector._finalize_session)
        -- an item that hadn't finished rendering on the very first frame,
        or a higher gold amount that only became readable later. Adds the
        extra loot to the existing log entry without re-counting the
        chest itself as a new one.
        """
        named_hits = self.session.amend_chest(result.session_id, result.items, result.gold)
        if result.items or result.gold:
            self._append_amendment_log_row(result)
            self._flash_ui()
        self._refresh_all()

        for item in named_hits:
            if item.rarity == "Legendary":
                self._show_legendary_alert(item.name)

    def _insert_loot_items(self, items: List[LootItem]):
        hide_common = self.settings.get("hide_common", True)
        hide_uncommon = self.settings.get("hide_uncommon", False)

        display_items = []
        for item in items:
            if item.rarity == "Common" and hide_common:
                continue
            if item.rarity == "Uncommon" and hide_uncommon:
                continue
            display_items.append(item)

        if not display_items and not items:
            self.loot_text.insert(END, "(no items read)", "meta")
            return

        for item in display_items:
            if item.rarity == "Legendary":
                self.loot_text.insert(END, f"★ {item.name} (Legendary) ", "Legendary_row")
            elif item.rarity == "Famed":
                self.loot_text.insert(END, f"{item.name} (Famed) ", "Famed_bold")
            elif item.rarity is None:
                # Untagged currency/filler item (Gold, gems, playing
                # cards) -- real loot, just not rarity-colored in-game,
                # so no parenthetical tag or rarity-specific coloring.
                self.loot_text.insert(END, f"{item.name} ", "meta")
            else:
                self.loot_text.insert(END, f"{item.name} ({item.rarity}) ", item.rarity)

    def _append_loot_log_row(self, result: ChestResult):
        self.loot_text.configure(state=NORMAL)
        prefix = f"[{result.timestamp}] [{result.target}] [{result.chest_type}] — "
        self.loot_text.insert(END, prefix, "meta")
        self._insert_loot_items(result.items)
        self.loot_text.insert(END, f" — {result.gold}g\n", "meta")
        self.loot_text.configure(state=DISABLED)
        self.loot_text.see(END)

    def _append_amendment_log_row(self, result: ChestResult):
        self.loot_text.configure(state=NORMAL)
        prefix = f"[{result.timestamp}] [{result.target}] (more loot found in same chest) — "
        self.loot_text.insert(END, prefix, "meta")
        if result.items:
            self._insert_loot_items(result.items)
        gold_suffix = f" — +{result.gold}g\n" if result.gold else "\n"
        self.loot_text.insert(END, gold_suffix, "meta")
        self.loot_text.configure(state=DISABLED)
        self.loot_text.see(END)

    def _show_legendary_alert(self, item_name: str):
        try:
            messagebox.showinfo("LEGENDARY DROP!", f"LEGENDARY DROP: {item_name}!")
        except Exception:
            pass

    def _flash_ui(self):
        original = self.status_label.cget("bg")
        def flash(n=0):
            if n >= 6:
                self.status_label.configure(bg=PANEL_BG)
                return
            color = ACCENT if n % 2 == 0 else PANEL_BG
            self.status_label.configure(bg=color)
            self.root.after(120, lambda: flash(n + 1))
        flash()

    # ------------------------------------------------------------------
    # Full UI refresh (counters, named items panel, summaries)
    # ------------------------------------------------------------------
    def _refresh_all(self):
        stats = self.session.get_active_stats()
        if stats:
            self.kills_var.set(str(stats.kills))
            self.pouches_var.set(f"Pouches: {stats.pouches}")
            self.chests_var.set(f"Chests: {stats.chests}")
            self.skulls_var.set(f"Skull Chests: {stats.skull_chests}")
            self.skull_rate_var.set(f"{stats.skull_rate():.1f}% skull rate")

            r = stats.rarity_counts
            self.current_summary_var.set(
                f"{stats.name}: {stats.kills} kills | Pouch {stats.pouches} "
                f"Chest {stats.chests} Skull {stats.skull_chests} "
                f"({stats.skull_rate():.1f}%)\n"
                f"Common {r.get('Common',0)} | Uncommon {r.get('Uncommon',0)} | "
                f"Rare {r.get('Rare',0)} | Famed {r.get('Famed',0)} | "
                f"Legendary {r.get('Legendary',0)}"
            )
        else:
            self.kills_var.set("0")
            self.pouches_var.set("Pouches: 0")
            self.chests_var.set("Chests: 0")
            self.skulls_var.set("Skull Chests: 0")
            self.skull_rate_var.set("0.0% skull rate")
            self.current_summary_var.set("Current target: (none selected)")

        total = self.session.session_totals()
        r = total.rarity_counts
        self.total_summary_var.set(
            f"ALL TARGETS: {total.kills} kills | Pouch {total.pouches} "
            f"Chest {total.chests} Skull {total.skull_chests} "
            f"({total.skull_rate():.1f}%)\n"
            f"Common {r.get('Common',0)} | Uncommon {r.get('Uncommon',0)} | "
            f"Rare {r.get('Rare',0)} | Famed {r.get('Famed',0)} | "
            f"Legendary {r.get('Legendary',0)}"
        )

        self._refresh_named_items()

    def _refresh_named_items(self):
        famed = self.session.named_items_by_rarity("Famed")
        legendary = self.session.named_items_by_rarity("Legendary")

        famed_total = sum(r.count for r in famed)
        legendary_total = sum(r.count for r in legendary)

        self.famed_header_var.set(f"FAMED DROPS — {famed_total} total")
        self.legendary_header_var.set(f"LEGENDARY DROPS — {legendary_total} total")

        self.famed_listbox.delete(0, END)
        for rec in famed:
            self.famed_listbox.insert(END, f"{rec.name} ×{rec.count}")

        self.legendary_listbox.delete(0, END)
        for rec in legendary:
            self.legendary_listbox.insert(END, f"{rec.name} ×{rec.count}")

    # ------------------------------------------------------------------
    # Export / reset / new target actions
    # ------------------------------------------------------------------
    def _on_export_excel(self):
        try:
            path = export_to_excel(self.session, self.settings.get("export_folder"))
            messagebox.showinfo(APP_TITLE, f"Excel file saved:\n{path}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Failed to export Excel file:\n{e}")

    def _on_export_text(self):
        try:
            path = export_to_text(self.session, self.settings.get("export_folder"))
            messagebox.showinfo(APP_TITLE, f"Text file saved:\n{path}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Failed to export text file:\n{e}")

    def _on_new_target(self):
        self.target_combo.focus_set()
        messagebox.showinfo(APP_TITLE, "Pick a new target from the dropdown and click Set Target.\n"
                                        "Your full session history is kept.")

    def _on_reset_session(self):
        if messagebox.askyesno(APP_TITLE, "Reset the entire session? This clears all kills, "
                                           "chests, and named item tracking. This cannot be undone."):
            self.session.reset()
            self.loot_text.configure(state=NORMAL)
            self.loot_text.delete("1.0", END)
            self.loot_text.configure(state=DISABLED)
            self.active_target_label.config(text="Farming: (none selected)")
            self._refresh_all()

    # ------------------------------------------------------------------
    # Settings panel
    # ------------------------------------------------------------------
    def _open_settings(self):
        win = Toplevel(self.root)
        win.title("Settings")
        win.configure(bg=BG)
        win.geometry("420x600")
        win.minsize(360, 300)
        win.attributes("-topmost", True)

        # Pinned button area, reserved at the bottom BEFORE the scrollable
        # canvas below is packed, so "Save" always stays visible without
        # needing to scroll down to it -- the actual button widget is
        # added into this frame further down, once save_and_close exists.
        button_frame = Frame(win, bg=BG)
        button_frame.pack(side=BOTTOM, fill=X)

        # The settings content is taller than fits in a reasonable window
        # size (especially with the per-rarity color sliders), and this
        # dialog previously had no way to scroll, so lower settings like
        # the export folder were unreachable. Same scrollable canvas
        # pattern used for the main tracker window (see _build_ui).
        import tkinter as tk

        canvas_frame = Frame(win, bg=BG)
        canvas_frame.pack(side=TOP, fill=BOTH, expand=True)

        settings_canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        vscroll = Scrollbar(canvas_frame, orient=VERTICAL, command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=RIGHT, fill=Y)
        settings_canvas.pack(side=LEFT, fill=BOTH, expand=True)

        parent = Frame(settings_canvas, bg=BG)
        canvas_window = settings_canvas.create_window((0, 0), window=parent, anchor="nw")

        def _on_configure(_evt):
            settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))
        parent.bind("<Configure>", _on_configure)

        def _on_canvas_resize(evt):
            settings_canvas.itemconfig(canvas_window, width=evt.width)
        settings_canvas.bind("<Configure>", _on_canvas_resize)

        def _on_mousewheel(evt):
            settings_canvas.yview_scroll(int(-1 * (evt.delta / 120)), "units")
        # Bound to the canvas itself (not bind_all) so this only scrolls
        # while the mouse is over the Settings dialog, and doesn't hijack
        # mouse-wheel scrolling on the main tracker window behind it.
        settings_canvas.bind("<MouseWheel>", _on_mousewheel)

        Label(parent, text="Detection polling interval (ms)", bg=BG, fg=FG).pack(anchor=W, padx=10, pady=(12, 0))
        Label(parent, text=f"Type a number ({MIN_POLL_INTERVAL_MS}-{MAX_POLL_INTERVAL_MS}). Lower catches\n"
                            "loot faster but taxes your system more -- each check does real\n"
                            "screenshot + image work, so very low values raise CPU usage.",
              bg=BG, fg=GREY, justify=LEFT, font=("Segoe UI", 8)).pack(anchor=W, padx=10)
        poll_var = StringVar(value=str(self.settings.get("poll_interval_ms", 500)))
        Entry(parent, textvariable=poll_var, width=10).pack(anchor=W, padx=10, pady=(2, 0))

        Label(parent, text="Chest close cooldown (ms)", bg=BG, fg=FG).pack(anchor=W, padx=10, pady=(10, 0))
        Label(parent, text=f"Type a number ({MIN_CLOSE_COOLDOWN_MS}-{MAX_CLOSE_COOLDOWN_MS}). After a\n"
                            "loot popup closes, this is how long that same spot is ignored\n"
                            "before a new one there counts, to avoid double-counting a fading-\n"
                            "out animation as a new chest. Lower this if a chest sometimes\n"
                            "doesn't get picked up right after another one closed.",
              bg=BG, fg=GREY, justify=LEFT, font=("Segoe UI", 8)).pack(anchor=W, padx=10)
        cooldown_var = StringVar(value=str(self.settings.get("close_cooldown_ms", 400)))
        Entry(parent, textvariable=cooldown_var, width=10).pack(anchor=W, padx=10, pady=(2, 0))

        hide_common_var = BooleanVar(value=self.settings.get("hide_common", True))
        Checkbutton(parent, text="Hide Common items in loot log", variable=hide_common_var,
                    bg=BG, fg=FG, selectcolor=PANEL_BG, activebackground=BG).pack(anchor=W, padx=10, pady=(8, 0))

        hide_uncommon_var = BooleanVar(value=self.settings.get("hide_uncommon", False))
        Checkbutton(parent, text="Hide Uncommon items in loot log", variable=hide_uncommon_var,
                    bg=BG, fg=FG, selectcolor=PANEL_BG, activebackground=BG).pack(anchor=W, padx=10)

        Label(parent, text="HSV Hue Center per Rarity (0-360)", bg=BG, fg=FG,
              font=("Segoe UI", 9, "bold")).pack(anchor=W, padx=10, pady=(14, 2))

        hsv_targets = copy.deepcopy(self.settings.get("hsv_targets", DEFAULT_HSV_TARGETS))
        hue_vars = {}
        for rarity in RARITY_ORDER:
            row = Frame(parent, bg=BG)
            row.pack(fill=X, padx=10, pady=2)
            Label(row, text=rarity, width=10, anchor=W, bg=BG, fg=RARITY_DISPLAY_HEX[rarity]).pack(side=LEFT)
            v = IntVar(value=hsv_targets.get(rarity, {}).get("h", 0))
            hue_vars[rarity] = v
            Scale(row, from_=0, to=360, orient=HORIZONTAL, variable=v, bg=BG, fg=FG,
                  troughcolor=PANEL_BG, length=220).pack(side=LEFT)

        Label(parent, text="Loot Window Background Color (Parchment)", bg=BG, fg=FG,
              font=("Segoe UI", 9, "bold")).pack(anchor=W, padx=10, pady=(14, 2))
        Label(parent, text="If chests are never detected at all (not even briefly flashing\n"
                            "\"Loot window detected\"), this color likely doesn't match your\n"
                            "game. Use tools/color_sampler.py on a screenshot of an open\n"
                            "loot popup to find the right numbers -- this matters most for\n"
                            "Mac, where rendering hasn't been tested.",
              bg=BG, fg=GREY, justify=LEFT, font=("Segoe UI", 8)).pack(anchor=W, padx=10)

        parchment_rgb = list(self.settings.get("parchment_rgb", DEFAULT_PARCHMENT_RGB))
        parchment_vars = []
        for i, channel in enumerate(["Red", "Green", "Blue"]):
            row = Frame(parent, bg=BG)
            row.pack(fill=X, padx=10, pady=2)
            Label(row, text=channel, width=10, anchor=W, bg=BG, fg=FG).pack(side=LEFT)
            v = IntVar(value=parchment_rgb[i])
            parchment_vars.append(v)
            Scale(row, from_=0, to=255, orient=HORIZONTAL, variable=v, bg=BG, fg=FG,
                  troughcolor=PANEL_BG, length=220).pack(side=LEFT)

        tol_row = Frame(parent, bg=BG)
        tol_row.pack(fill=X, padx=10, pady=2)
        Label(tol_row, text="Tolerance", width=10, anchor=W, bg=BG, fg=FG).pack(side=LEFT)
        parchment_tol_var = IntVar(value=self.settings.get("parchment_tolerance", DEFAULT_PARCHMENT_TOLERANCE))
        Scale(tol_row, from_=5, to=80, orient=HORIZONTAL, variable=parchment_tol_var, bg=BG, fg=FG,
              troughcolor=PANEL_BG, length=220).pack(side=LEFT)

        Label(parent, text="Export folder", bg=BG, fg=FG,
              font=("Segoe UI", 9, "bold")).pack(anchor=W, padx=10, pady=(14, 2))
        folder_var = StringVar(value=self.settings.get("export_folder", default_export_folder()))
        Entry(parent, textvariable=folder_var, width=48).pack(padx=10, pady=(0, 16))

        def save_and_close():
            try:
                poll_ms = int(poll_var.get().strip())
            except (ValueError, AttributeError):
                messagebox.showwarning(
                    APP_TITLE, f"Polling interval must be a whole number between "
                               f"{MIN_POLL_INTERVAL_MS} and {MAX_POLL_INTERVAL_MS}."
                )
                return
            if not (MIN_POLL_INTERVAL_MS <= poll_ms <= MAX_POLL_INTERVAL_MS):
                messagebox.showwarning(
                    APP_TITLE, f"Polling interval must be between "
                               f"{MIN_POLL_INTERVAL_MS} and {MAX_POLL_INTERVAL_MS} ms."
                )
                return

            try:
                cooldown_ms = int(cooldown_var.get().strip())
            except (ValueError, AttributeError):
                messagebox.showwarning(
                    APP_TITLE, f"Chest close cooldown must be a whole number between "
                               f"{MIN_CLOSE_COOLDOWN_MS} and {MAX_CLOSE_COOLDOWN_MS}."
                )
                return
            if not (MIN_CLOSE_COOLDOWN_MS <= cooldown_ms <= MAX_CLOSE_COOLDOWN_MS):
                messagebox.showwarning(
                    APP_TITLE, f"Chest close cooldown must be between "
                               f"{MIN_CLOSE_COOLDOWN_MS} and {MAX_CLOSE_COOLDOWN_MS} ms."
                )
                return

            self.settings["poll_interval_ms"] = poll_ms
            self.settings["close_cooldown_ms"] = cooldown_ms
            self.settings["hide_common"] = hide_common_var.get()
            self.settings["hide_uncommon"] = hide_uncommon_var.get()
            self.settings["export_folder"] = folder_var.get().strip() or default_export_folder()
            for rarity in RARITY_ORDER:
                hsv_targets.setdefault(rarity, {})
                hsv_targets[rarity]["h"] = hue_vars[rarity].get()
                hsv_targets[rarity].setdefault("s", DEFAULT_HSV_TARGETS[rarity]["s"])
                hsv_targets[rarity].setdefault("v", DEFAULT_HSV_TARGETS[rarity]["v"])
                hsv_targets[rarity].setdefault("tolerance", DEFAULT_HSV_TARGETS[rarity]["tolerance"])
            self.settings["hsv_targets"] = hsv_targets
            new_parchment_rgb = [v.get() for v in parchment_vars]
            self.settings["parchment_rgb"] = new_parchment_rgb
            self.settings["parchment_tolerance"] = parchment_tol_var.get()
            self._save_settings()
            if self.detector:
                self.detector.settings.poll_interval_ms = self.settings["poll_interval_ms"]
                self.detector.settings.post_close_cooldown_s = self.settings["close_cooldown_ms"] / 1000.0
                self.detector.settings.hsv_targets = hsv_targets
                self.detector.settings.parchment_rgb = tuple(new_parchment_rgb)
                self.detector.settings.parchment_tolerance = self.settings["parchment_tolerance"]
            win.destroy()

        # Closing the dialog via the window's own close button (instead of
        # clicking "Save") used to silently discard every change with no
        # feedback -- Tk's default close behavior just destroys the window.
        # That was reported as settings "resetting" every time the dialog
        # was reopened, when really they were never being saved at all.
        # Routing the close button through the same save-and-validate path
        # fixes this: any way of closing the dialog now persists changes.
        win.protocol("WM_DELETE_WINDOW", save_and_close)

        Button(button_frame, text="Save", command=save_and_close, bg=ACCENT, fg="#20242b",
               relief="flat", cursor="hand2").pack(pady=16)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _on_close(self):
        try:
            if self.detector:
                self.detector.stop()
        except Exception:
            pass
        try:
            self.session.autosave(self.app_dir)
        except Exception:
            pass
        self.root.destroy()


def main():
    root = Tk()
    app = TLOPOTrackerApp(root)

    def handle_exception(exc, val, tb):
        traceback.print_exception(exc, val, tb)
        try:
            messagebox.showerror(APP_TITLE, f"An unexpected error occurred:\n{val}\n\n"
                                             "The tracker will try to keep running.")
        except Exception:
            pass

    root.report_callback_exception = handle_exception
    root.mainloop()


if __name__ == "__main__":
    main()
