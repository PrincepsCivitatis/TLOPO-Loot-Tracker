"""
exporter.py
Excel (.xlsx) and plaintext (.txt) export logic for the TLOPO Loot Tracker.
"""

import os
from datetime import datetime
from typing import Optional

from loot_parser import RARITY_ORDER
from session import Session

RARITY_FILL_HEX = {
    "Crude": "D9D9D9",
    "Common": "FFF2A8",
    "Rare": "C6EFCE",
    "Famed": "BDD7EE",
    "Legendary": "FFC7CE",
}
RARITY_FONT_HEX = {
    "Crude": "595959",
    "Common": "9C6500",
    "Rare": "006100",
    "Famed": "1F4E78",
    "Legendary": "9C0006",
}

RARITY_SORT_INDEX = {"Legendary": 0, "Famed": 1, "Rare": 2, "Common": 3, "Crude": 4}


def default_export_folder() -> str:
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    folder = os.path.join(desktop, "TLOPO_Tracker_Exports")
    return folder


def _timestamped_filename(prefix: str, ext: str) -> str:
    now = datetime.now()
    return f"{prefix}_{now.strftime('%Y-%m-%d_%H-%M')}.{ext}"


def export_to_excel(session: Session, folder: Optional[str] = None) -> str:
    """Write the 3-sheet Excel workbook. Returns the full path written."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError(
            "openpyxl is not installed. Run install.bat to set up dependencies."
        ) from e

    folder = folder or default_export_folder()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _timestamped_filename("TLOPO_Session", "xlsx"))

    wb = Workbook()

    # -----------------------------------------------------------------
    # Sheet 1 - Session Summary
    # -----------------------------------------------------------------
    ws1 = wb.active
    ws1.title = "Session Summary"
    headers1 = ["Target", "Kills", "Pouches", "Chests", "Skull Chests", "Skull Rate",
                "Crude", "Common", "Rare", "Famed", "Legendary"]
    ws1.append(headers1)
    for c in ws1[1]:
        c.font = Font(bold=True)

    total = session.session_totals()
    for name, stats in sorted(session.targets.items()):
        row = [
            stats.name, stats.kills, stats.pouches, stats.chests, stats.skull_chests,
            f"{stats.skull_rate():.1f}%",
            stats.rarity_counts.get("Crude", 0),
            stats.rarity_counts.get("Common", 0),
            stats.rarity_counts.get("Rare", 0),
            stats.rarity_counts.get("Famed", 0),
            stats.rarity_counts.get("Legendary", 0),
        ]
        ws1.append(row)
        r = ws1.max_row
        if stats.rarity_counts.get("Legendary", 0) > 0:
            for cell in ws1[r]:
                cell.fill = PatternFill("solid", fgColor=RARITY_FILL_HEX["Legendary"])
        elif stats.rarity_counts.get("Famed", 0) > 0:
            for cell in ws1[r]:
                cell.fill = PatternFill("solid", fgColor=RARITY_FILL_HEX["Famed"])

    ws1.append([
        "TOTAL", total.kills, total.pouches, total.chests, total.skull_chests,
        f"{total.skull_rate():.1f}%",
        total.rarity_counts.get("Crude", 0),
        total.rarity_counts.get("Common", 0),
        total.rarity_counts.get("Rare", 0),
        total.rarity_counts.get("Famed", 0),
        total.rarity_counts.get("Legendary", 0),
    ])
    for cell in ws1[ws1.max_row]:
        cell.font = Font(bold=True)

    for i, h in enumerate(headers1, start=1):
        ws1.column_dimensions[get_column_letter(i)].width = max(12, len(h) + 4)

    # -----------------------------------------------------------------
    # Sheet 2 - Named Item Log
    # -----------------------------------------------------------------
    ws2 = wb.create_sheet("Named Item Log")
    headers2 = ["Item Name", "Rarity", "Times Obtained", "Targets Found From"]
    ws2.append(headers2)
    for c in ws2[1]:
        c.font = Font(bold=True)

    named_records = sorted(
        session.named_items.values(),
        key=lambda r: (RARITY_SORT_INDEX.get(r.rarity, 99), -r.count),
    )
    for rec in named_records:
        ws2.append([rec.name, rec.rarity, rec.count, ", ".join(sorted(rec.targets))])
        r = ws2.max_row
        fill_hex = RARITY_FILL_HEX.get(rec.rarity)
        if fill_hex:
            for cell in ws2[r]:
                cell.fill = PatternFill("solid", fgColor=fill_hex)

    ws2.column_dimensions["A"].width = 30
    ws2.column_dimensions["B"].width = 14
    ws2.column_dimensions["C"].width = 16
    ws2.column_dimensions["D"].width = 40

    # -----------------------------------------------------------------
    # Sheet 3 - Full Loot Log
    # -----------------------------------------------------------------
    ws3 = wb.create_sheet("Full Loot Log")
    headers3 = ["Timestamp", "Target", "Chest Type", "Item Name", "Rarity", "Gold", "Kill Number"]
    ws3.append(headers3)
    for c in ws3[1]:
        c.font = Font(bold=True)

    for target_name, stats in sorted(session.targets.items()):
        for entry in stats.loot_log:
            items = entry.get("items", [])
            if not items:
                ws3.append([
                    entry.get("timestamp", ""), entry.get("target", target_name),
                    entry.get("chest_type", ""), "(no items)", "", entry.get("gold", 0),
                    entry.get("kill_number", 0),
                ])
                continue
            for item in items:
                ws3.append([
                    entry.get("timestamp", ""),
                    entry.get("target", target_name),
                    entry.get("chest_type", ""),
                    item.get("name", ""),
                    item.get("rarity", ""),
                    entry.get("gold", 0),
                    entry.get("kill_number", 0),
                ])
                r = ws3.max_row
                rarity = item.get("rarity", "")
                fill_hex = RARITY_FILL_HEX.get(rarity)
                font_hex = RARITY_FONT_HEX.get(rarity)
                if fill_hex:
                    ws3.cell(row=r, column=5).fill = PatternFill("solid", fgColor=fill_hex)
                if font_hex:
                    ws3.cell(row=r, column=5).font = Font(color=font_hex, bold=(rarity in ("Famed", "Legendary")))

    for i, h in enumerate(headers3, start=1):
        ws3.column_dimensions[get_column_letter(i)].width = max(14, len(h) + 4)

    wb.save(path)
    return path


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def export_to_text(session: Session, folder: Optional[str] = None) -> str:
    """Write the plaintext session export. Returns the full path written."""
    folder = folder or default_export_folder()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _timestamped_filename("TLOPO_Session", "txt"))

    lines = []
    lines.append("=== TLOPO LOOT TRACKER SESSION ===")
    lines.append(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Duration: {_format_duration(session.duration_seconds())}")
    lines.append("")

    for name, stats in sorted(session.targets.items()):
        lines.append(f"--- {name.upper()} ({stats.kills} kills) ---")
        lines.append(
            f"Pouches: {stats.pouches} | Chests: {stats.chests} | "
            f"Skull Chests: {stats.skull_chests} ({stats.skull_rate():.1f}% skull rate)"
        )
        lines.append(
            "Crude: {c} | Common: {u} | Rare: {r} | Famed: {f} | Legendary: {l}".format(
                c=stats.rarity_counts.get("Crude", 0),
                u=stats.rarity_counts.get("Common", 0),
                r=stats.rarity_counts.get("Rare", 0),
                f=stats.rarity_counts.get("Famed", 0),
                l=stats.rarity_counts.get("Legendary", 0),
            )
        )
        lines.append("")

        target_famed = [r for r in session.named_items.values()
                         if r.rarity == "Famed" and name in r.targets]
        target_legendary = [r for r in session.named_items.values()
                             if r.rarity == "Legendary" and name in r.targets]
        target_famed.sort(key=lambda r: r.count, reverse=True)
        target_legendary.sort(key=lambda r: r.count, reverse=True)

        famed_total = sum(r.count for r in target_famed)
        lines.append(f"FAMED DROPS ({famed_total} total):")
        if target_famed:
            for rec in target_famed:
                lines.append(f"  {rec.name} x{rec.count}")
        else:
            lines.append("  None")
        lines.append("")

        legendary_total = sum(r.count for r in target_legendary)
        lines.append(f"LEGENDARY DROPS ({legendary_total} total):")
        if target_legendary:
            for rec in target_legendary:
                lines.append(f"  {rec.name} x{rec.count}")
        else:
            lines.append("  None")
        lines.append("")

        if stats.loot_log:
            lines.append("Full Loot Log:")
            for entry in stats.loot_log:
                items = entry.get("items", [])
                item_strs = [
                    f"{it['name']} ({it['rarity']})" if it.get("rarity") else it['name']
                    for it in items
                ] or ["(no items read)"]
                lines.append(
                    f"  [{entry.get('timestamp','')}] [{entry.get('target', name)}] "
                    f"[{entry.get('chest_type','')}] — {' '.join(item_strs)} — {entry.get('gold',0)}g"
                )
            lines.append("")

    total = session.session_totals()
    lines.append("=== SESSION TOTALS ===")
    lines.append(
        f"Total Kills: {total.kills} | Total Skull Chests: {total.skull_chests} "
        f"({total.skull_rate():.1f}% rate)"
    )
    lines.append(
        "Crude: {c} | Common: {u} | Rare: {r} | Famed: {f} | Legendary: {l}".format(
            c=total.rarity_counts.get("Crude", 0),
            u=total.rarity_counts.get("Common", 0),
            r=total.rarity_counts.get("Rare", 0),
            f=total.rarity_counts.get("Famed", 0),
            l=total.rarity_counts.get("Legendary", 0),
        )
    )
    lines.append("")

    all_famed = session.named_items_by_rarity("Famed")
    all_famed_total = sum(r.count for r in all_famed)
    lines.append(f"ALL FAMED DROPS THIS SESSION ({all_famed_total} total):")
    if all_famed:
        for rec in all_famed:
            lines.append(f"  {rec.name} x{rec.count}")
    else:
        lines.append("  None")
    lines.append("")

    all_legendary = session.named_items_by_rarity("Legendary")
    all_legendary_total = sum(r.count for r in all_legendary)
    lines.append(f"ALL LEGENDARY DROPS THIS SESSION ({all_legendary_total} total):")
    if all_legendary:
        for rec in all_legendary:
            lines.append(f"  {rec.name} x{rec.count}")
    else:
        lines.append("  None")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return path
