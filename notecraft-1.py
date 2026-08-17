#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NoteCraft - Premium Notes Application
=====================================
A single-file, zero-asset, offline-first rich text notes application.

Design notes
------------
* Zero external assets. The application icon is drawn at runtime with QPainter,
  so the distributed EXE is genuinely standalone. `--export-icon` regenerates a
  multi-resolution .ico for the PyInstaller build step.
* One source of truth for colour. Every widget is styled by a single global
  stylesheet generated from semantic design tokens, plus a matching QPalette.
  Nothing hard-codes a hex value, so theme switching can never leave a widget
  stranded in the previous theme.
* Contrast is enforced, not hoped for. Every token pair clears WCAG AA (4.5:1),
  and note content is passed through a contrast guard on load so text authored
  in one theme stays legible in the other.

Author: built for Godfather
License: MIT
"""

from __future__ import annotations

import html as html_mod
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PyQt6.QtCore import (
    QBuffer, QEvent, QIODevice, QLineF, QMimeData, QPoint, QRectF, QSettings, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QCursor, QDesktopServices, QFont, QFontDatabase,
    QFontMetrics, QGuiApplication, QIcon, QKeySequence, QLinearGradient,
    QPageSize, QPainter, QPainterPath, QPalette, QPdfWriter, QPen, QPixmap,
    QShortcut, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFrameFormat, QTextListFormat, QTextTableFormat,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu,
    QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStackedWidget, QStatusBar, QStyleFactory, QTextEdit,
    QToolButton, QVBoxLayout, QWidget,
)

APP_NAME = "NoteCraft"
APP_VERSION = "3.0"
ORG_NAME = "NoteCraft"
APP_ID = "NoteCraft.App.3"

IS_WINDOWS = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# Colour mathematics
# ---------------------------------------------------------------------------

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_hex(rgb: Iterable[float]) -> str:
    r, g, b = (int(round(_clamp(c, 0, 255))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _linearize(channel: float) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str | QColor) -> float:
    if isinstance(color, QColor):
        r, g, b = color.red(), color.green(), color.blue()
    else:
        r, g, b = hex_to_rgb(color)
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(fg: str | QColor, bg: str | QColor) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def mix(color_a: str, color_b: str, weight: float) -> str:
    """Blend two hex colours. weight=0 returns color_a, weight=1 returns color_b."""
    a, b = hex_to_rgb(color_a), hex_to_rgb(color_b)
    return rgb_to_hex(a[i] + (b[i] - a[i]) * _clamp(weight) for i in range(3))


def is_light(color: str | QColor) -> bool:
    return relative_luminance(color) > 0.36


def readable_on(background: str | QColor, light: str = "#FFFFFF", dark: str = "#101418") -> str:
    """Pick whichever of two foregrounds contrasts better with `background`."""
    return dark if contrast_ratio(dark, background) >= contrast_ratio(light, background) else light


def ensure_contrast(fg: QColor, bg: QColor, target: float = 4.5) -> QColor:
    """
    Nudge `fg` toward legibility against `bg` while preserving hue and saturation.

    The result is a pure function of (hue, saturation, background), so repeated
    theme switches converge instead of drifting the colour further each time.
    """
    if contrast_ratio(fg, bg) >= target:
        return QColor(fg)

    h, s, _lightness, a = fg.getHslF()
    if h < 0:
        h = 0.0
    bg_is_light = relative_luminance(bg) > 0.36

    best = QColor(fg)
    best_ratio = contrast_ratio(fg, bg)
    # Walk lightness away from the background until the target is met.
    steps = 60
    for i in range(steps + 1):
        t = i / steps
        lightness = (1.0 - t) * 0.5 if bg_is_light else 0.5 + t * 0.5
        candidate = QColor.fromHslF(h, s, _clamp(lightness), a)
        ratio = contrast_ratio(candidate, bg)
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
        if ratio >= target:
            return candidate
    return best


# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

ACCENT_CHOICES: dict[str, tuple[str, str]] = {
    # key: (dark accent, light accent)
    "indigo": ("#7C9CFF", "#3A5BD9"),
    "violet": ("#B18AFF", "#6D3FD1"),
    "teal": ("#5BD0C4", "#0F7C74"),
    "emerald": ("#5FD08A", "#12794A"),
    "amber": ("#F0B454", "#9A6206"),
    "rose": ("#FF8FA8", "#C02B52"),
}

NOTE_TINTS: dict[str, dict[str, str]] = {
    # token: {dark tint, light tint, rail colour dark, rail colour light, label}
    "default": {"dark": "", "light": "", "rail_dark": "", "rail_light": "", "label": "None"},
    "red": {"dark": "#2E1B1E", "light": "#FDECEC", "rail_dark": "#E2707C", "rail_light": "#C0392B", "label": "Red"},
    "amber": {"dark": "#2C2317", "light": "#FDF3E2", "rail_dark": "#E0A94E", "rail_light": "#B57608", "label": "Amber"},
    "green": {"dark": "#16271D", "light": "#E9F7EE", "rail_dark": "#5FC98B", "rail_light": "#158043", "label": "Green"},
    "teal": {"dark": "#132628", "light": "#E4F4F4", "rail_dark": "#4FC6C0", "rail_light": "#0E7C75", "label": "Teal"},
    "blue": {"dark": "#172334", "light": "#E9F0FD", "rail_dark": "#6FA3F0", "rail_light": "#1F5FC7", "label": "Blue"},
    "violet": {"dark": "#231C33", "light": "#F0EBFC", "rail_dark": "#A98CF0", "rail_light": "#6A3FC4", "label": "Violet"},
    "rose": {"dark": "#2E1A26", "light": "#FBEAF2", "rail_dark": "#EE86AE", "rail_light": "#B92D6B", "label": "Rose"},
}

# Legacy hex values written by NoteCraft 1.x/2.x, mapped onto semantic tokens.
LEGACY_COLOR_MAP: dict[str, str] = {
    "#2b2b2b": "default", "#ffffff": "default", "#1a1a1a": "default",
    "#3d2626": "red", "#ffe6e6": "red",
    "#263d26": "green", "#e6ffe6": "green",
    "#26263d": "blue", "#e6e6ff": "blue",
    "#3d3d26": "amber", "#ffffe6": "amber",
    "#3d263d": "violet", "#ffe6ff": "violet",
    "#263d3d": "teal", "#e6ffff": "teal",
}

PRIORITY_COLORS = {
    "High": ("#FF7B8A", "#C0243C"),
    "Medium": ("#F0B454", "#9A6206"),
    "Low": ("#7FC8F8", "#1F6AA5"),
}


@dataclass
class Theme:
    """A complete, self-consistent set of semantic colour tokens."""
    name: str
    tokens: dict[str, str]

    def __getitem__(self, key: str) -> str:
        return self.tokens[key]

    def get(self, key: str, default: str = "") -> str:
        return self.tokens.get(key, default)

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"

    def note_tint(self, token: str | None) -> str:
        """Background for a note card / editor surface, per theme."""
        if not token or token == "default":
            return self["surface"]
        entry = NOTE_TINTS.get(token)
        if not entry:
            return self["surface"]
        return entry["dark" if self.is_dark else "light"] or self["surface"]

    def note_rail(self, token: str | None) -> str:
        if not token or token == "default":
            return "transparent"
        entry = NOTE_TINTS.get(token)
        if not entry:
            return "transparent"
        return entry["rail_dark" if self.is_dark else "rail_light"] or "transparent"

    def priority_color(self, priority: str | None) -> str:
        entry = PRIORITY_COLORS.get(priority or "")
        if not entry:
            return self["text_muted"]
        return entry[0] if self.is_dark else entry[1]


def build_theme(name: str, accent_key: str = "indigo") -> Theme:
    accent_pair = ACCENT_CHOICES.get(accent_key, ACCENT_CHOICES["indigo"])

    if name == "dark":
        accent = accent_pair[0]
        base = {
            "bg": "#0E1116",
            "surface": "#151A21",
            "surface_alt": "#1B212B",
            "elevated": "#222A36",
            "sunken": "#0B0E13",
            "text": "#E7EAF0",
            "text_muted": "#9BA6B7",
            "text_faint": "#7E8A9C",
            "text_disabled": "#5C6675",
            "border": "#2A3341",
            "border_strong": "#3A4557",
            "accent": accent,
            "accent_solid": mix(accent, "#000000", 0.30),
            "accent_hover": mix(accent, "#000000", 0.18),
            "accent_press": mix(accent, "#000000", 0.42),
            "accent_soft": mix(accent, "#0E1116", 0.82),
            "on_accent": "#FFFFFF",
            "selection": mix(accent, "#0E1116", 0.62),
            "danger": "#FF7B8A",
            "danger_solid": "#B23347",
            "success": "#5FD08A",
            "warning": "#F0B454",
            "scroll": "#39445466",
            "scroll_hover": "#4E5C70",
            "code_bg": "#10151C",
            "shadow": "#00000066",
        }
    else:
        accent = accent_pair[1]
        base = {
            "bg": "#F4F6F9",
            "surface": "#FFFFFF",
            "surface_alt": "#EDF0F5",
            "elevated": "#FFFFFF",
            "sunken": "#E7EBF1",
            "text": "#131924",
            "text_muted": "#54607A",
            "text_faint": "#66728A",
            "text_disabled": "#98A2B5",
            "border": "#D8DEE8",
            "border_strong": "#BCC5D4",
            "accent": accent,
            "accent_solid": accent,
            "accent_hover": mix(accent, "#000000", 0.14),
            "accent_press": mix(accent, "#000000", 0.26),
            "accent_soft": mix(accent, "#FFFFFF", 0.88),
            "on_accent": "#FFFFFF",
            "selection": mix(accent, "#FFFFFF", 0.80),
            "danger": "#C0243C",
            "danger_solid": "#C0243C",
            "success": "#12794A",
            "warning": "#9A6206",
            "scroll": "#C4CCD9",
            "scroll_hover": "#A6B1C4",
            "code_bg": "#F1F3F7",
            "shadow": "#0F172A22",
        }

    base["on_accent"] = readable_on(base["accent_solid"])
    base["selection_text"] = readable_on(base["selection"])
    return Theme(name=name, tokens=base)


# ---------------------------------------------------------------------------
# Stylesheet engine
# ---------------------------------------------------------------------------

STYLESHEET = """
* {{
    outline: 0;
}}

QWidget {{
    background-color: transparent;
    color: {text};
    font-family: "{ui_font}";
    font-size: {font_size}pt;
}}

QMainWindow, QDialog {{
    background-color: {bg};
}}

QToolTip {{
    background-color: {elevated};
    color: {text};
    border: 1px solid {border_strong};
    border-radius: 6px;
    padding: 6px 9px;
}}

/* ---------- Structural surfaces ---------- */

#Sidebar {{
    background-color: {surface};
    border-right: 1px solid {border};
}}

#ListPane {{
    background-color: {bg};
    border-right: 1px solid {border};
}}

#EditorPane {{
    background-color: {surface};
}}

#Toolbar {{
    background-color: {surface_alt};
    border: 1px solid {border};
    border-radius: 10px;
}}

#InlinePanel {{
    background-color: {surface_alt};
    border: 1px solid {border};
    border-radius: 10px;
}}

#Divider {{
    background-color: {border};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

#VDivider {{
    background-color: {border};
    max-width: 1px;
    min-width: 1px;
    border: none;
}}

/* ---------- Typography ---------- */

QLabel {{
    background: transparent;
    color: {text};
}}

QLabel#BrandMark {{
    color: {text};
    font-size: {brand_size}pt;
    font-weight: 700;
}}

QLabel#PaneTitle {{
    color: {text};
    font-size: {title_size}pt;
    font-weight: 700;
}}

QLabel#SectionLabel {{
    color: {text_faint};
    font-size: {micro_size}pt;
    font-weight: 700;
    letter-spacing: 1px;
}}

QLabel#Muted, QLabel#MetaLine {{
    color: {text_muted};
    font-size: {small_size}pt;
}}

QLabel#Faint {{
    color: {text_faint};
    font-size: {small_size}pt;
}}

QLabel#EmptyState {{
    color: {text_muted};
    font-size: {body_size}pt;
}}

QLabel#SaveBadge {{
    color: {text_faint};
    font-size: {small_size}pt;
    padding: 2px 8px;
    border-radius: 8px;
    background-color: {surface_alt};
}}

QLabel#SaveBadge[state="dirty"] {{
    color: {warning};
}}

QLabel#SaveBadge[state="saved"] {{
    color: {success};
}}

/* ---------- Buttons ---------- */

QPushButton {{
    background-color: {surface_alt};
    color: {text};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 7px 14px;
    font-size: {body_size}pt;
}}

QPushButton:hover {{
    background-color: {elevated};
    border-color: {border_strong};
}}

QPushButton:pressed {{
    background-color: {sunken};
}}

QPushButton:disabled {{
    color: {text_disabled};
    background-color: {surface_alt};
    border-color: {border};
}}

QPushButton[variant="primary"] {{
    background-color: {accent_solid};
    color: {on_accent};
    border: 1px solid {accent_solid};
    font-weight: 600;
}}

QPushButton[variant="primary"]:hover {{
    background-color: {accent_hover};
    border-color: {accent_hover};
}}

QPushButton[variant="primary"]:pressed {{
    background-color: {accent_press};
    border-color: {accent_press};
}}

QPushButton[variant="danger"] {{
    background-color: transparent;
    color: {danger};
    border: 1px solid {danger};
}}

QPushButton[variant="danger"]:hover {{
    background-color: {danger_solid};
    color: #FFFFFF;
    border-color: {danger_solid};
}}

QPushButton[variant="ghost"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {text_muted};
}}

QPushButton[variant="ghost"]:hover {{
    background-color: {surface_alt};
    color: {text};
}}

QPushButton[variant="nav"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {text};
    text-align: left;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: {body_size}pt;
}}

QPushButton[variant="nav"]:hover {{
    background-color: {surface_alt};
}}

QPushButton[variant="nav"][active="true"] {{
    background-color: {accent_soft};
    color: {accent};
    font-weight: 600;
}}

/* ---------- Tool buttons (formatting bar) ---------- */

QToolButton {{
    background-color: transparent;
    color: {text};
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 4px;
}}

QToolButton:hover {{
    background-color: {elevated};
    border-color: {border};
}}

QToolButton:pressed {{
    background-color: {sunken};
}}

QToolButton:checked {{
    background-color: {accent_solid};
    color: {on_accent};
    border-color: {accent_solid};
}}

QToolButton:disabled {{
    color: {text_disabled};
}}

QToolButton::menu-indicator {{
    image: none;
    width: 0px;
}}

/* ---------- Inputs ---------- */

QLineEdit, QSpinBox, QComboBox {{
    background-color: {surface};
    color: {text};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 7px 11px;
    selection-background-color: {selection};
    selection-color: {selection_text};
}}

QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{
    border-color: {border_strong};
}}

QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {accent};
}}

QLineEdit:disabled, QComboBox:disabled {{
    color: {text_disabled};
    background-color: {surface_alt};
}}

QLineEdit#SearchField {{
    background-color: {surface_alt};
    padding-left: 32px;
    border-radius: 9px;
}}

QLineEdit#TitleField {{
    background-color: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0px;
    padding: 4px 2px;
    font-size: {hero_size}pt;
    font-weight: 700;
    color: {text};
}}

QLineEdit#TitleField:focus {{
    border-bottom: 2px solid {accent};
}}

QComboBox::drop-down {{
    border: none;
    width: 20px;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {text_muted};
    width: 0px;
    height: 0px;
    margin-right: 8px;
}}

QComboBox QAbstractItemView {{
    background-color: {elevated};
    color: {text};
    border: 1px solid {border_strong};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {accent_solid};
    selection-color: {on_accent};
    outline: none;
}}

QCheckBox {{
    color: {text};
    spacing: 8px;
    background: transparent;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid {border_strong};
    background-color: {surface};
}}

QCheckBox::indicator:hover {{
    border-color: {accent};
}}

QCheckBox::indicator:checked {{
    background-color: {accent_solid};
    border-color: {accent_solid};
}}

QCheckBox:disabled {{
    color: {text_disabled};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {border};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {accent_solid};
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}

/* ---------- Text editor ---------- */

QTextEdit {{
    background-color: {editor_bg};
    color: {text};
    border: 1px solid {border};
    border-radius: 12px;
    padding: 18px 22px;
    selection-background-color: {selection};
    selection-color: {selection_text};
}}

QTextEdit:focus {{
    border-color: {border_strong};
}}

/* ---------- Lists ---------- */

QListWidget {{
    background-color: {surface};
    color: {text};
    border: 1px solid {border};
    border-radius: 10px;
    padding: 4px;
    outline: none;
}}

QListWidget::item {{
    padding: 8px 10px;
    border-radius: 7px;
    color: {text};
}}

QListWidget::item:hover {{
    background-color: {surface_alt};
}}

QListWidget::item:selected {{
    background-color: {accent_solid};
    color: {on_accent};
}}

/* ---------- Menus ---------- */

QMenu {{
    background-color: {elevated};
    color: {text};
    border: 1px solid {border_strong};
    border-radius: 10px;
    padding: 6px;
}}

QMenu::item {{
    padding: 7px 26px 7px 14px;
    border-radius: 6px;
    color: {text};
}}

QMenu::item:selected {{
    background-color: {accent_solid};
    color: {on_accent};
}}

QMenu::item:disabled {{
    color: {text_disabled};
}}

QMenu::separator {{
    height: 1px;
    background-color: {border};
    margin: 5px 8px;
}}

QMenu::indicator {{
    width: 14px;
    height: 14px;
    left: 8px;
}}

/* ---------- Scrollbars ---------- */

QScrollArea {{
    background-color: transparent;
    border: none;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: {scroll};
    border-radius: 4px;
    min-height: 28px;
}}

QScrollBar::handle:vertical:hover {{
    background: {scroll_hover};
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 2px;
}}

QScrollBar::handle:horizontal {{
    background: {scroll};
    border-radius: 4px;
    min-width: 28px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {scroll_hover};
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
    border: none;
    background: none;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}

/* ---------- Splitter ---------- */

QSplitter::handle {{
    background-color: {border};
}}

QSplitter::handle:horizontal {{
    width: 1px;
}}

QSplitter::handle:hover {{
    background-color: {accent};
}}

/* ---------- Status bar ---------- */

QStatusBar {{
    background-color: {surface};
    color: {text_muted};
    border-top: 1px solid {border};
}}

QStatusBar::item {{
    border: none;
}}

/* ---------- Message boxes ---------- */

QMessageBox {{
    background-color: {elevated};
}}

QMessageBox QLabel {{
    color: {text};
    font-size: {body_size}pt;
}}

QMessageBox QPushButton {{
    min-width: 84px;
}}

/* ---------- Note cards ---------- */

NoteCard {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: 11px;
}}

NoteCard:hover {{
    border-color: {border_strong};
}}

NoteCard[selected="true"] {{
    border-color: {accent};
    background-color: {accent_soft};
}}

/* ---------- Chips ---------- */

TagChip {{
    border-radius: 9px;
    padding: 2px 8px;
}}

#CommandPalette {{
    background-color: {elevated};
    border: 1px solid {border_strong};
    border-radius: 14px;
}}
"""


def build_stylesheet(theme: Theme, ui_font: str, base_size: int, editor_bg: str | None = None) -> str:
    values = dict(theme.tokens)
    values.update({
        "ui_font": ui_font,
        "font_size": base_size,
        "body_size": base_size,
        "small_size": max(7, base_size - 1),
        "micro_size": max(6, base_size - 2),
        "title_size": base_size + 4,
        "brand_size": base_size + 4,
        "hero_size": base_size + 9,
        "editor_bg": editor_bg or theme["surface"],
    })
    return STYLESHEET.format(**values)


def build_palette(theme: Theme) -> QPalette:
    """A matching QPalette so native-drawn parts never fall back to system colours."""
    p = QPalette()
    c = lambda key: QColor(theme[key])  # noqa: E731

    groups = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive)
    for group in groups:
        p.setColor(group, QPalette.ColorRole.Window, c("bg"))
        p.setColor(group, QPalette.ColorRole.WindowText, c("text"))
        p.setColor(group, QPalette.ColorRole.Base, c("surface"))
        p.setColor(group, QPalette.ColorRole.AlternateBase, c("surface_alt"))
        p.setColor(group, QPalette.ColorRole.ToolTipBase, c("elevated"))
        p.setColor(group, QPalette.ColorRole.ToolTipText, c("text"))
        p.setColor(group, QPalette.ColorRole.Text, c("text"))
        p.setColor(group, QPalette.ColorRole.Button, c("surface_alt"))
        p.setColor(group, QPalette.ColorRole.ButtonText, c("text"))
        p.setColor(group, QPalette.ColorRole.BrightText, c("danger"))
        p.setColor(group, QPalette.ColorRole.Link, c("accent"))
        p.setColor(group, QPalette.ColorRole.LinkVisited, c("accent"))
        p.setColor(group, QPalette.ColorRole.Highlight, c("accent_solid"))
        p.setColor(group, QPalette.ColorRole.HighlightedText, c("on_accent"))
        p.setColor(group, QPalette.ColorRole.PlaceholderText, c("text_faint"))
        p.setColor(group, QPalette.ColorRole.Mid, c("border"))
        p.setColor(group, QPalette.ColorRole.Dark, c("border_strong"))
        p.setColor(group, QPalette.ColorRole.Shadow, QColor(0, 0, 0, 90))

    disabled = QPalette.ColorGroup.Disabled
    p.setColor(disabled, QPalette.ColorRole.WindowText, c("text_disabled"))
    p.setColor(disabled, QPalette.ColorRole.Text, c("text_disabled"))
    p.setColor(disabled, QPalette.ColorRole.ButtonText, c("text_disabled"))
    p.setColor(disabled, QPalette.ColorRole.Base, c("surface_alt"))
    p.setColor(disabled, QPalette.ColorRole.Button, c("surface_alt"))
    p.setColor(disabled, QPalette.ColorRole.Window, c("bg"))
    p.setColor(disabled, QPalette.ColorRole.Highlight, c("border"))
    p.setColor(disabled, QPalette.ColorRole.HighlightedText, c("text_disabled"))
    return p


# ---------------------------------------------------------------------------
# Vector icon set (drawn at runtime, tinted per theme)
# ---------------------------------------------------------------------------

class Icons:
    """
    A small monochrome icon set drawn with QPainter on a 24x24 grid.

    Icons are generated on demand and cached per (name, colour, size) so they
    always match the active theme - which means they can never end up as dark
    glyphs on a dark background after a theme switch.
    """

    _cache: dict[tuple, QIcon] = {}

    # Glyph icons rendered as styled letterforms
    _TEXT_GLYPHS = {
        "bold": ("B", True, False, False),
        "italic": ("I", False, True, False),
        "underline": ("U", False, False, True),
        "strike": ("S", False, False, False),
    }

    @classmethod
    def get(cls, name: str, color: str, size: int = 18) -> QIcon:
        key = (name, color, size)
        cached = cls._cache.get(key)
        if cached is not None:
            return cached
        icon = QIcon(cls.pixmap(name, color, size))
        cls._cache[key] = icon
        return icon

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()

    @classmethod
    def pixmap(cls, name: str, color: str, size: int = 18) -> QPixmap:
        ratio = 2
        pm = QPixmap(size * ratio, size * ratio)
        pm.fill(Qt.GlobalColor.transparent)
        pm.setDevicePixelRatio(ratio)

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        # The painter works in device-independent pixels because the pixmap already
        # carries a devicePixelRatio, so scale by the logical size only.
        p.scale(size / 24.0, size / 24.0)
        try:
            cls._draw(p, name, QColor(color))
        finally:
            p.end()
        return pm

    # -- drawing helpers ----------------------------------------------------

    @staticmethod
    def _stroke(p: QPainter, color: QColor, width: float = 2.0) -> None:
        pen = QPen(color)
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

    @staticmethod
    def _fill(p: QPainter, color: QColor) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))

    @classmethod
    def _draw(cls, p: QPainter, name: str, color: QColor) -> None:
        if name in cls._TEXT_GLYPHS:
            char, bold, italic, underline = cls._TEXT_GLYPHS[name]
            font = QFont("Georgia" if italic else "Segoe UI", 15)
            font.setBold(bold)
            font.setItalic(italic)
            font.setUnderline(underline)
            font.setStrikeOut(name == "strike")
            p.setFont(font)
            p.setPen(QPen(color))
            p.drawText(QRectF(0, 0, 24, 24), int(Qt.AlignmentFlag.AlignCenter), char)
            return

        handler = getattr(cls, f"_icon_{name}", None)
        if handler is None:
            cls._stroke(p, color)
            p.drawEllipse(QRectF(6, 6, 12, 12))
            return
        handler(p, color)

    # -- individual icons ---------------------------------------------------

    @classmethod
    def _icon_plus(cls, p, c):
        cls._stroke(p, c, 2.2)
        p.drawLine(QLineF(12, 5, 12, 19))
        p.drawLine(QLineF(5, 12, 19, 12))

    @classmethod
    def _icon_search(cls, p, c):
        cls._stroke(p, c, 2.0)
        p.drawEllipse(QRectF(4, 4, 12, 12))
        p.drawLine(QLineF(15, 15, 20, 20))

    @classmethod
    def _icon_close(cls, p, c):
        cls._stroke(p, c, 2.0)
        p.drawLine(QLineF(6, 6, 18, 18))
        p.drawLine(QLineF(18, 6, 6, 18))

    @classmethod
    def _icon_notes(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawRoundedRect(QRectF(5, 3.5, 14, 17), 2.5, 2.5)
        p.drawLine(QLineF(8.5, 8, 15.5, 8))
        p.drawLine(QLineF(8.5, 12, 15.5, 12))
        p.drawLine(QLineF(8.5, 16, 13, 16))

    @classmethod
    def _icon_star(cls, p, c):
        path = QPainterPath()
        pts = [(12, 3.5), (14.6, 9.3), (20.8, 10.0), (16.2, 14.2),
               (17.5, 20.3), (12, 17.2), (6.5, 20.3), (7.8, 14.2),
               (3.2, 10.0), (9.4, 9.3)]
        path.moveTo(*pts[0])
        for pt in pts[1:]:
            path.lineTo(*pt)
        path.closeSubpath()
        cls._stroke(p, c, 1.8)
        p.drawPath(path)

    @classmethod
    def _icon_star_filled(cls, p, c):
        path = QPainterPath()
        pts = [(12, 3.5), (14.6, 9.3), (20.8, 10.0), (16.2, 14.2),
               (17.5, 20.3), (12, 17.2), (6.5, 20.3), (7.8, 14.2),
               (3.2, 10.0), (9.4, 9.3)]
        path.moveTo(*pts[0])
        for pt in pts[1:]:
            path.lineTo(*pt)
        path.closeSubpath()
        cls._fill(p, c)
        p.drawPath(path)

    @classmethod
    def _icon_pin(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(12, 14, 12, 21))
        path = QPainterPath()
        path.moveTo(7, 4)
        path.lineTo(17, 4)
        path.lineTo(15, 9)
        path.lineTo(18, 13)
        path.lineTo(6, 13)
        path.lineTo(9, 9)
        path.closeSubpath()
        p.drawPath(path)

    @classmethod
    def _icon_pin_filled(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(12, 14, 12, 21))
        path = QPainterPath()
        path.moveTo(7, 4)
        path.lineTo(17, 4)
        path.lineTo(15, 9)
        path.lineTo(18, 13)
        path.lineTo(6, 13)
        path.lineTo(9, 9)
        path.closeSubpath()
        cls._fill(p, c)
        p.drawPath(path)

    @classmethod
    def _icon_archive(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawRoundedRect(QRectF(3.5, 4, 17, 4.5), 1.5, 1.5)
        p.drawRoundedRect(QRectF(5, 8.5, 14, 11.5), 2, 2)
        p.drawLine(QLineF(9.5, 13, 14.5, 13))

    @classmethod
    def _icon_folder(cls, p, c):
        cls._stroke(p, c, 1.9)
        path = QPainterPath()
        path.moveTo(3.5, 7)
        path.lineTo(9, 7)
        path.lineTo(11, 9.5)
        path.lineTo(20.5, 9.5)
        path.lineTo(20.5, 19)
        path.lineTo(3.5, 19)
        path.closeSubpath()
        p.drawPath(path)

    @classmethod
    def _icon_tag(cls, p, c):
        cls._stroke(p, c, 1.9)
        path = QPainterPath()
        path.moveTo(11, 3.5)
        path.lineTo(20.5, 13)
        path.lineTo(13, 20.5)
        path.lineTo(3.5, 11)
        path.lineTo(3.5, 3.5)
        path.closeSubpath()
        p.drawPath(path)
        cls._fill(p, c)
        p.drawEllipse(QRectF(6.2, 6.2, 3.2, 3.2))

    @classmethod
    def _icon_gear(cls, p, c):
        p.save()
        p.translate(12, 12)
        cls._fill(p, c)
        for _ in range(8):
            p.drawRoundedRect(QRectF(-2.1, -10.4, 4.2, 4.6), 1.2, 1.2)
            p.rotate(45)
        p.restore()
        cls._stroke(p, c, 2.6)
        p.drawEllipse(QRectF(4.6, 4.6, 14.8, 14.8))
        cls._stroke(p, c, 1.8)
        p.drawEllipse(QRectF(9.2, 9.2, 5.6, 5.6))

    @classmethod
    def _icon_trash(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(4, 6.5, 20, 6.5))
        p.drawLine(QLineF(9.5, 6.5, 9.5, 4))
        p.drawLine(QLineF(14.5, 6.5, 14.5, 4))
        p.drawLine(QLineF(9.5, 4, 14.5, 4))
        path = QPainterPath()
        path.moveTo(6, 6.5)
        path.lineTo(7.2, 20)
        path.lineTo(16.8, 20)
        path.lineTo(18, 6.5)
        p.drawPath(path)
        p.drawLine(QLineF(10, 10, 10.4, 16.5))
        p.drawLine(QLineF(14, 10, 13.6, 16.5))

    @classmethod
    def _icon_restore(cls, p, c):
        cls._stroke(p, c, 1.9)
        path = QPainterPath()
        path.moveTo(5, 12)
        path.arcTo(QRectF(5, 5, 14, 14), 180, -280)
        p.drawPath(path)
        p.drawLine(QLineF(5, 12, 8.5, 8.5))
        p.drawLine(QLineF(5, 12, 1.8, 8.5))

    @classmethod
    def _icon_sun(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawEllipse(QRectF(8.5, 8.5, 7, 7))
        p.save()
        p.translate(12, 12)
        for _ in range(8):
            p.drawLine(QLineF(0, -9.6, 0, -7.4))
            p.rotate(45)
        p.restore()

    @classmethod
    def _icon_moon(cls, p, c):
        cls._stroke(p, c, 1.9)
        path = QPainterPath()
        path.moveTo(19, 14.5)
        path.arcTo(QRectF(3.5, 3.5, 17, 17), 40, 200)
        path.arcTo(QRectF(7.5, 1.5, 15, 15), 230, -150)
        path.closeSubpath()
        p.drawPath(path)

    @classmethod
    def _icon_list_bullet(cls, p, c):
        cls._stroke(p, c, 1.9)
        for y in (6.5, 12, 17.5):
            p.drawLine(QLineF(9, y, 20, y))
        cls._fill(p, c)
        for y in (6.5, 12, 17.5):
            p.drawEllipse(QRectF(3.6, y - 1.4, 2.8, 2.8))

    @classmethod
    def _icon_list_number(cls, p, c):
        cls._stroke(p, c, 1.9)
        for y in (6.5, 12, 17.5):
            p.drawLine(QLineF(9.5, y, 20, y))
        font = QFont("Segoe UI", 6)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(c))
        for i, y in enumerate((6.5, 12, 17.5), start=1):
            p.drawText(QRectF(2, y - 5, 6, 10), int(Qt.AlignmentFlag.AlignCenter), str(i))

    @classmethod
    def _icon_checklist(cls, p, c):
        cls._stroke(p, c, 1.9)
        for y in (7, 17):
            p.drawLine(QLineF(11, y, 20.5, y))
        p.drawRoundedRect(QRectF(3.2, 3.4, 7, 7), 1.6, 1.6)
        p.drawRoundedRect(QRectF(3.2, 13.4, 7, 7), 1.6, 1.6)
        p.drawLine(QLineF(4.8, 17, 6.5, 18.6))
        p.drawLine(QLineF(6.5, 18.6, 8.9, 15.4))

    @classmethod
    def _icon_table(cls, p, c):
        cls._stroke(p, c, 1.8)
        p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 2, 2)
        p.drawLine(QLineF(3.5, 9.5, 20.5, 9.5))
        p.drawLine(QLineF(3.5, 14.5, 20.5, 14.5))
        p.drawLine(QLineF(12, 4.5, 12, 19.5))

    @classmethod
    def _icon_palette(cls, p, c):
        cls._stroke(p, c, 1.8)
        path = QPainterPath()
        path.addEllipse(QRectF(3.2, 3.2, 17.6, 17.6))
        p.drawPath(path)
        cls._fill(p, c)
        for cx, cy in ((8.2, 8.0), (13.4, 6.6), (17.0, 10.6), (8.0, 14.6)):
            p.drawEllipse(QRectF(cx - 1.35, cy - 1.35, 2.7, 2.7))

    @classmethod
    def _icon_highlighter(cls, p, c):
        cls._stroke(p, c, 1.8)
        path = QPainterPath()
        path.moveTo(6, 14.5)
        path.lineTo(13.5, 4.5)
        path.lineTo(19, 8.5)
        path.lineTo(11.5, 18.5)
        path.lineTo(6, 18.5)
        path.closeSubpath()
        p.drawPath(path)
        cls._fill(p, c)
        p.drawRect(QRectF(3.5, 20, 17, 1.8))

    @classmethod
    def _icon_clear_format(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(6, 5, 18, 5))
        p.drawLine(QLineF(12.5, 5, 9.5, 19))
        p.drawLine(QLineF(15, 13, 21, 19))
        p.drawLine(QLineF(21, 13, 15, 19))

    @classmethod
    def _icon_quote(cls, p, c):
        cls._fill(p, c)
        p.drawRoundedRect(QRectF(3.5, 5, 2.6, 14), 1.3, 1.3)
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(9.5, 8, 20.5, 8))
        p.drawLine(QLineF(9.5, 12, 20.5, 12))
        p.drawLine(QLineF(9.5, 16, 16, 16))

    @classmethod
    def _icon_code(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(8.5, 7, 3.5, 12))
        p.drawLine(QLineF(3.5, 12, 8.5, 17))
        p.drawLine(QLineF(15.5, 7, 20.5, 12))
        p.drawLine(QLineF(20.5, 12, 15.5, 17))
        p.drawLine(QLineF(13.4, 4.6, 10.6, 19.4))

    @classmethod
    def _icon_hr(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(3.5, 12, 20.5, 12))
        p.drawLine(QLineF(6, 6.5, 18, 6.5))
        p.drawLine(QLineF(6, 17.5, 18, 17.5))

    @classmethod
    def _icon_link(cls, p, c):
        cls._stroke(p, c, 1.9)
        path = QPainterPath()
        path.moveTo(10, 14)
        path.lineTo(14, 10)
        p.drawPath(path)
        p.drawArc(QRectF(2.5, 9.5, 11, 11), 45 * 16, 180 * 16)
        p.drawArc(QRectF(10.5, 3.5, 11, 11), 225 * 16, 180 * 16)

    @classmethod
    def _icon_paperclip(cls, p, c):
        cls._stroke(p, c, 1.9)
        path = QPainterPath()
        path.moveTo(17.5, 8)
        path.lineTo(9, 16.5)
        path.arcTo(QRectF(4.5, 12, 9, 9), 90, 180)
        path.lineTo(16, 7)
        path.arcTo(QRectF(11.5, 2.5, 9, 9), 270, 180)
        p.drawPath(path)

    @classmethod
    def _icon_mail(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawRoundedRect(QRectF(3, 5.5, 18, 13), 2.4, 2.4)
        p.drawLine(QLineF(3.8, 7, 12, 13.2))
        p.drawLine(QLineF(20.2, 7, 12, 13.2))

    @classmethod
    def _icon_export(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(12, 3.5, 12, 14.5))
        p.drawLine(QLineF(12, 3.5, 8, 7.5))
        p.drawLine(QLineF(12, 3.5, 16, 7.5))
        path = QPainterPath()
        path.moveTo(4.5, 12.5)
        path.lineTo(4.5, 20)
        path.lineTo(19.5, 20)
        path.lineTo(19.5, 12.5)
        p.drawPath(path)

    @classmethod
    def _icon_more(cls, p, c):
        cls._fill(p, c)
        for x in (5.6, 12, 18.4):
            p.drawEllipse(QRectF(x - 1.6, 10.4, 3.2, 3.2))

    @classmethod
    def _icon_chevron_left(cls, p, c):
        cls._stroke(p, c, 2.1)
        p.drawLine(QLineF(14.5, 5.5, 8.5, 12))
        p.drawLine(QLineF(8.5, 12, 14.5, 18.5))

    @classmethod
    def _icon_chevron_right(cls, p, c):
        cls._stroke(p, c, 2.1)
        p.drawLine(QLineF(9.5, 5.5, 15.5, 12))
        p.drawLine(QLineF(15.5, 12, 9.5, 18.5))

    @classmethod
    def _icon_chevron_down(cls, p, c):
        cls._stroke(p, c, 2.1)
        p.drawLine(QLineF(5.5, 9.5, 12, 15.5))
        p.drawLine(QLineF(12, 15.5, 18.5, 9.5))

    @classmethod
    def _icon_focus(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(3.5, 8, 3.5, 3.5))
        p.drawLine(QLineF(3.5, 3.5, 8, 3.5))
        p.drawLine(QLineF(20.5, 8, 20.5, 3.5))
        p.drawLine(QLineF(20.5, 3.5, 16, 3.5))
        p.drawLine(QLineF(3.5, 16, 3.5, 20.5))
        p.drawLine(QLineF(3.5, 20.5, 8, 20.5))
        p.drawLine(QLineF(20.5, 16, 20.5, 20.5))
        p.drawLine(QLineF(20.5, 20.5, 16, 20.5))

    @classmethod
    def _icon_sidebar(cls, p, c):
        cls._stroke(p, c, 1.8)
        p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 2, 2)
        p.drawLine(QLineF(9.5, 4.5, 9.5, 19.5))

    @classmethod
    def _icon_sort(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(4, 6.5, 15, 6.5))
        p.drawLine(QLineF(4, 12, 12, 12))
        p.drawLine(QLineF(4, 17.5, 9, 17.5))
        p.drawLine(QLineF(18.5, 6, 18.5, 18))
        p.drawLine(QLineF(18.5, 18, 15.5, 15))
        p.drawLine(QLineF(18.5, 18, 21.5, 15))

    @classmethod
    def _icon_command(cls, p, c):
        cls._stroke(p, c, 1.8)
        p.drawRoundedRect(QRectF(9, 9, 6, 6), 0.8, 0.8)
        for rect in (QRectF(4.5, 4.5, 4.5, 4.5), QRectF(15, 4.5, 4.5, 4.5),
                     QRectF(4.5, 15, 4.5, 4.5), QRectF(15, 15, 4.5, 4.5)):
            p.drawRoundedRect(rect, 2.2, 2.2)

    @classmethod
    def _icon_calendar(cls, p, c):
        cls._stroke(p, c, 1.8)
        p.drawRoundedRect(QRectF(3.5, 5, 17, 15), 2.2, 2.2)
        p.drawLine(QLineF(3.5, 10, 20.5, 10))
        p.drawLine(QLineF(8, 3, 8, 6.5))
        p.drawLine(QLineF(16, 3, 16, 6.5))

    @classmethod
    def _icon_replace(cls, p, c):
        cls._stroke(p, c, 1.9)
        p.drawLine(QLineF(3.5, 7.5, 17, 7.5))
        p.drawLine(QLineF(17, 7.5, 13.5, 4))
        p.drawLine(QLineF(17, 7.5, 13.5, 11))
        p.drawLine(QLineF(20.5, 16.5, 7, 16.5))
        p.drawLine(QLineF(7, 16.5, 10.5, 13))
        p.drawLine(QLineF(7, 16.5, 10.5, 20))


# ---------------------------------------------------------------------------
# Application icon (drawn, then packed into a multi-resolution .ico on demand)
# ---------------------------------------------------------------------------

def render_app_icon(size: int) -> QPixmap:
    """Draw the NoteCraft mark: a gradient tile holding a folded page."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(size / 1024.0, size / 1024.0)

    # Tile
    tile = QPainterPath()
    tile.addRoundedRect(QRectF(32, 32, 960, 960), 224, 224)
    grad = QLinearGradient(64, 32, 960, 992)
    grad.setColorAt(0.0, QColor("#5B5BF0"))
    grad.setColorAt(0.55, QColor("#6D46E0"))
    grad.setColorAt(1.0, QColor("#8A3FD0"))
    p.fillPath(tile, QBrush(grad))

    # Soft top highlight
    gloss = QLinearGradient(0, 32, 0, 560)
    gloss.setColorAt(0.0, QColor(255, 255, 255, 46))
    gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.save()
    p.setClipPath(tile)
    p.fillRect(QRectF(32, 32, 960, 528), QBrush(gloss))
    p.restore()

    # Page with a folded top-right corner
    fold = 176.0
    page = QPainterPath()
    page.moveTo(300, 258)
    page.lineTo(724 - fold, 258)
    page.lineTo(724, 258 + fold)
    page.lineTo(724, 766)
    page.lineTo(300, 766)
    page.closeSubpath()

    p.save()
    p.translate(0, 16)
    p.fillPath(page, QBrush(QColor(0, 0, 0, 46)))
    p.restore()
    p.fillPath(page, QBrush(QColor("#FFFFFF")))

    corner = QPainterPath()
    corner.moveTo(724 - fold, 258)
    corner.lineTo(724, 258 + fold)
    corner.lineTo(724 - fold, 258 + fold)
    corner.closeSubpath()
    p.fillPath(corner, QBrush(QColor("#C9CCE8")))

    # Ruled lines - the top rule is the amber "craft" accent
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#F2A93B")))
    p.drawRoundedRect(QRectF(366, 396, 208, 52), 26, 26)
    p.setBrush(QBrush(QColor("#4A4E86")))
    p.drawRoundedRect(QRectF(366, 496, 292, 52), 26, 26)
    p.setBrush(QBrush(QColor("#7A7FB4")))
    p.drawRoundedRect(QRectF(366, 596, 232, 52), 26, 26)

    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256, 512):
        icon.addPixmap(render_app_icon(size))
    return icon


def _pixmap_to_png_bytes(pm: QPixmap) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pm.save(buffer, "PNG")
    return bytes(buffer.data())


def write_ico(path: str | Path, sizes: Iterable[int] = (16, 24, 32, 48, 64, 128, 256)) -> Path:
    """
    Pack the drawn icon into a Vista-style .ico (PNG-compressed entries).

    Only needed at build time, so PyInstaller can stamp the EXE. The shipped
    application never reads this file.
    """
    entries: list[bytes] = [_pixmap_to_png_bytes(render_app_icon(s)) for s in sizes]
    count = len(entries)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    directory = b""
    for size, data in zip(sizes, entries):
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path = Path(path)
    path.write_bytes(header + directory + b"".join(entries))
    return path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class _Unset:
    """Sentinel that lets update_note() distinguish 'leave alone' from 'set NULL'."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<unset>"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


@dataclass
class Note:
    id: int
    title: str
    content: str
    content_html: str
    folder_id: Optional[int]
    is_pinned: int
    is_archived: int
    is_favorite: int
    color_token: str
    editor_tint: str
    priority: str
    created_date: str
    edited_date: str
    last_updated: str
    word_count: int
    folder_name: Optional[str] = None
    attachment_count: int = 0
    link_count: int = 0
    tags: list[tuple[int, str, str]] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Note":
        keys = set(row.keys())
        return cls(
            id=row["id"],
            title=row["title"] or "",
            content=row["content"] or "",
            content_html=row["content_html"] or "",
            folder_id=row["folder_id"],
            is_pinned=row["is_pinned"] or 0,
            is_archived=row["is_archived"] or 0,
            is_favorite=row["is_favorite"] or 0,
            color_token=row["color_token"] or "default",
            editor_tint=row["editor_tint"] or "default",
            priority=row["priority"] or "None",
            created_date=row["created_date"] or "",
            edited_date=row["edited_date"] or "",
            last_updated=row["last_updated"] or "",
            word_count=row["word_count"] or 0,
            folder_name=row["folder_name"] if "folder_name" in keys else None,
            attachment_count=row["attachment_count"] if "attachment_count" in keys else 0,
            link_count=row["link_count"] if "link_count" in keys else 0,
        )

    @property
    def display_title(self) -> str:
        return self.title.strip() or "Untitled note"


NOTE_SELECT = """
    SELECT n.id, n.title, n.content, n.content_html, n.folder_id, n.is_pinned,
           n.is_archived, n.is_favorite, n.color_token, n.editor_tint, n.priority,
           n.created_date, n.edited_date, n.last_updated, n.word_count,
           f.name AS folder_name,
           (SELECT COUNT(*) FROM attachments a WHERE a.note_id = n.id) AS attachment_count,
           (SELECT COUNT(*) FROM links l WHERE l.note_id = n.id) AS link_count
    FROM notes n
    LEFT JOIN folders f ON f.id = n.folder_id
"""

SORT_CLAUSES = {
    "updated": "n.is_pinned DESC, n.last_updated DESC",
    "created": "n.is_pinned DESC, n.created_date DESC",
    "title": "n.is_pinned DESC, n.title COLLATE NOCASE ASC",
    "priority": ("n.is_pinned DESC, CASE n.priority WHEN 'High' THEN 0 WHEN 'Medium' "
                 "THEN 1 WHEN 'Low' THEN 2 ELSE 3 END, n.last_updated DESC"),
}


class NotesDatabase:
    """SQLite persistence with WAL journaling, schema migrations and FTS search."""

    SCHEMA_VERSION = 3

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is not None:
            self.app_data_path = Path(base_path)
        elif IS_WINDOWS and os.getenv("APPDATA"):
            self.app_data_path = Path(os.environ["APPDATA"]) / APP_NAME
        else:
            self.app_data_path = Path.home() / f".{APP_NAME.lower()}"
        self.app_data_path.mkdir(parents=True, exist_ok=True)

        self.db_path = self.app_data_path / "notes.db"
        self.attachments_path = self.app_data_path / "attachments"
        self.attachments_path.mkdir(exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            # WAL needs real file locking; a synced or network folder may refuse it.
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

        self.has_fts = False
        self._repairing = False
        self.startup_notice = ""

        self._migrate()
        self._setup_fts()
        self.cleanup_orphaned_attachments()

    # -- resilience ---------------------------------------------------------

    CORRUPTION_MARKERS = ("malformed", "corrupt", "database disk image")

    @classmethod
    def _is_corruption(cls, error: Exception) -> bool:
        message = str(error).lower()
        return any(marker in message for marker in cls.CORRUPTION_MARKERS)

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """
        Run a statement, healing a broken search index rather than crashing.

        Writes to `notes` fire the FTS triggers, so an out-of-sync index turns
        every save into a fatal "database disk image is malformed". Rebuilding
        the index recovers it; if that fails the index is dropped and search
        falls back to LIKE, which is slower but always correct.
        """
        try:
            return self.conn.execute(sql, params)
        except sqlite3.DatabaseError as error:
            if self._repairing or not self._is_corruption(error):
                raise
            if self._repair_fts():
                return self.conn.execute(sql, params)
            raise

    def file_is_intact(self) -> bool:
        try:
            return self.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    def _index_row_count(self) -> Optional[int]:
        """Rows genuinely present in the FTS index (not in the content table)."""
        try:
            return self.conn.execute("SELECT COUNT(*) FROM notes_fts_docsize").fetchone()[0]
        except sqlite3.DatabaseError:
            return None

    def fts_is_stale(self) -> bool:
        indexed = self._index_row_count()
        if indexed is None:
            return True
        notes = self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        return indexed != notes

    def rebuild_fts(self) -> bool:
        try:
            self.conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
            self.conn.commit()
            return not self.fts_is_stale()
        except sqlite3.DatabaseError:
            return False

    def _repair_fts(self) -> bool:
        self._repairing = True
        try:
            if self.rebuild_fts():
                self.startup_notice = "Search index rebuilt."
                return True
            self.disable_fts()
            self.startup_notice = ("Search index could not be repaired and was removed; "
                                   "search still works.")
            return True
        except sqlite3.DatabaseError:
            return False
        finally:
            self._repairing = False

    def disable_fts(self) -> None:
        """Remove the index and its triggers so writes can never depend on it."""
        try:
            self.conn.executescript(
                """
                DROP TRIGGER IF EXISTS notes_fts_ai;
                DROP TRIGGER IF EXISTS notes_fts_ad;
                DROP TRIGGER IF EXISTS notes_fts_au;
                DROP TABLE IF EXISTS notes_fts;
                """
            )
            self.conn.commit()
        except sqlite3.DatabaseError:
            pass
        self.has_fts = False

    # -- schema -------------------------------------------------------------

    def _columns(self, table: str) -> set[str]:
        try:
            return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    def _migrate(self) -> None:
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT DEFAULT 'default',
                created_date TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                content TEXT DEFAULT '',
                content_html TEXT DEFAULT '',
                folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
                is_pinned INTEGER DEFAULT 0,
                is_archived INTEGER DEFAULT 0,
                is_favorite INTEGER DEFAULT 0,
                color TEXT,
                priority TEXT DEFAULT 'None',
                created_date TEXT NOT NULL,
                edited_date TEXT,
                last_updated TEXT NOT NULL,
                word_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT DEFAULT 'indigo'
            );

            CREATE TABLE IF NOT EXISTS note_tags (
                note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
                tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (note_id, tag_id)
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_type TEXT,
                file_size INTEGER,
                added_date TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                title TEXT,
                added_date TEXT NOT NULL
            );
            """
        )

        columns = self._columns("notes")
        if "color_token" not in columns:
            c.execute("ALTER TABLE notes ADD COLUMN color_token TEXT DEFAULT 'default'")
        if "editor_tint" not in columns:
            c.execute("ALTER TABLE notes ADD COLUMN editor_tint TEXT DEFAULT 'default'")

        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version < 3:
            self._migrate_legacy_colors()
            c.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

        c.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_notes_archived ON notes(is_archived);
            CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder_id);
            CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(last_updated);
            CREATE INDEX IF NOT EXISTS idx_attachments_note ON attachments(note_id);
            CREATE INDEX IF NOT EXISTS idx_links_note ON links(note_id);
            """
        )
        c.commit()

    def _migrate_legacy_colors(self) -> None:
        """Translate hard-coded hex note colours from 1.x/2.x into semantic tokens."""
        if "color" not in self._columns("notes"):
            return
        rows = self.conn.execute(
            "SELECT id, color FROM notes WHERE color IS NOT NULL AND color != ''"
        ).fetchall()
        for row in rows:
            token = self._hex_to_token(row["color"])
            self.conn.execute("UPDATE notes SET color_token = ? WHERE id = ?", (token, row["id"]))

        for row in self.conn.execute("SELECT id, color FROM folders").fetchall():
            if (row["color"] or "").startswith("#"):
                self.conn.execute("UPDATE folders SET color = ? WHERE id = ?",
                                  (self._hex_to_token(row["color"]), row["id"]))

        for row in self.conn.execute("SELECT id, color FROM tags").fetchall():
            if (row["color"] or "").startswith("#"):
                self.conn.execute("UPDATE tags SET color = ? WHERE id = ?",
                                  (self._hex_to_accent(row["color"]), row["id"]))

    @staticmethod
    def _hex_to_accent(value: str) -> str:
        """Map an old tag colour onto the nearest named accent."""
        if not value or not re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
            return "indigo"
        hue = QColor(value).hue()
        if hue < 0:
            return "indigo"
        for limit, key in ((18, "rose"), (48, "amber"), (95, "amber"), (160, "emerald"),
                           (200, "teal"), (255, "indigo"), (300, "violet"), (361, "rose")):
            if hue < limit:
                return key
        return "indigo"

    @staticmethod
    def _hex_to_token(value: str) -> str:
        if not value:
            return "default"
        key = value.strip().lower()
        if key in LEGACY_COLOR_MAP:
            return LEGACY_COLOR_MAP[key]
        if not re.fullmatch(r"#[0-9a-f]{6}", key):
            return "default"
        color = QColor(key)
        h, s, _l, _a = color.getHsl()
        if s < 42:
            return "default"
        hue_bands = [(20, "red"), (48, "amber"), (95, "amber"), (160, "green"),
                     (195, "teal"), (255, "blue"), (295, "violet"), (340, "rose"), (361, "red")]
        for limit, token in hue_bands:
            if h < limit:
                return token
        return "default"

    def _setup_fts(self) -> None:
        try:
            self.conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
                USING fts5(title, content, content='notes', content_rowid='id');

                CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
                    INSERT INTO notes_fts(rowid, title, content)
                    VALUES (new.id, new.title, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, title, content)
                    VALUES ('delete', old.id, old.title, old.content);
                END;

                CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, title, content)
                    VALUES ('delete', old.id, old.title, old.content);
                    INSERT INTO notes_fts(rowid, title, content)
                    VALUES (new.id, new.title, new.content);
                END;
                """
            )
            # NOTE: COUNT(*) on an external-content FTS5 table reads the *source*
            # table, so it reports rows even when the index is completely empty.
            # The shadow table is the only honest measure of what is indexed.
            if self.fts_is_stale():
                self.conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
            self.conn.commit()

            if self.fts_is_stale():
                self.disable_fts()
                self.startup_notice = "Search index unavailable; using simple search."
                return
            self.has_fts = True
        except sqlite3.Error:
            self.disable_fts()

    # -- housekeeping -------------------------------------------------------

    def cleanup_orphaned_attachments(self) -> int:
        removed = 0
        try:
            known = {row["filepath"] for row in self.conn.execute("SELECT filepath FROM attachments")}
            for path in self.attachments_path.iterdir():
                if path.is_file() and str(path) not in known:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # -- notes --------------------------------------------------------------

    def create_note(self, title: str = "Untitled note", content: str = "",
                    content_html: str = "", folder_id: Optional[int] = None) -> int:
        now = self._now()
        cur = self._execute(
            """INSERT INTO notes (title, content, content_html, folder_id,
                                  created_date, last_updated, word_count,
                                  color_token, editor_tint)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'default', 'default')""",
            (title, content, content_html, folder_id, now, now, len(content.split())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_note(self, note_id: int, *, title=UNSET, content=UNSET, content_html=UNSET,
                    folder_id=UNSET, is_pinned=UNSET, is_archived=UNSET, is_favorite=UNSET,
                    color_token=UNSET, editor_tint=UNSET, priority=UNSET,
                    touch: bool = True) -> None:
        updates: list[str] = []
        params: list[Any] = []

        def add(column: str, value: Any) -> None:
            updates.append(f"{column} = ?")
            params.append(value)

        if title is not UNSET:
            add("title", title)
        if content is not UNSET:
            add("content", content)
            add("word_count", len(str(content).split()))
        if content_html is not UNSET:
            add("content_html", content_html)
        if folder_id is not UNSET:
            add("folder_id", folder_id)
        if is_pinned is not UNSET:
            add("is_pinned", int(is_pinned))
        if is_archived is not UNSET:
            add("is_archived", int(is_archived))
        if is_favorite is not UNSET:
            add("is_favorite", int(is_favorite))
        if color_token is not UNSET:
            add("color_token", color_token)
        if editor_tint is not UNSET:
            add("editor_tint", editor_tint)
        if priority is not UNSET:
            add("priority", priority)

        if not updates:
            return

        if touch:
            now = self._now()
            add("edited_date", now)
            add("last_updated", now)

        params.append(note_id)
        self._execute(f"UPDATE notes SET {', '.join(updates)} WHERE id = ?", params)
        self.conn.commit()

    def get_note(self, note_id: int) -> Optional[Note]:
        row = self.conn.execute(NOTE_SELECT + " WHERE n.id = ?", (note_id,)).fetchone()
        if row is None:
            return None
        note = Note.from_row(row)
        note.tags = self.get_note_tags(note_id)
        return note

    def list_notes(self, *, scope: str = "all", folder_id: Optional[int] = None,
                   tag_id: Optional[int] = None, sort: str = "updated") -> list[Note]:
        where = []
        params: list[Any] = []
        joins = ""

        if scope == "archive":
            where.append("n.is_archived = 1")
        else:
            where.append("n.is_archived = 0")

        if scope == "favorites":
            where.append("n.is_favorite = 1")
        elif scope == "pinned":
            where.append("n.is_pinned = 1")
        elif scope == "folder":
            if folder_id is None:
                where.append("n.folder_id IS NULL")
            else:
                where.append("n.folder_id = ?")
                params.append(folder_id)
        elif scope == "tag" and tag_id is not None:
            joins = " JOIN note_tags nt ON nt.note_id = n.id AND nt.tag_id = ? "
            params.insert(0, tag_id)

        order = SORT_CLAUSES.get(sort, SORT_CLAUSES["updated"])
        query = NOTE_SELECT + joins + " WHERE " + " AND ".join(where) + f" ORDER BY {order}"
        rows = self.conn.execute(query, params).fetchall()
        return [Note.from_row(r) for r in rows]

    def search_notes(self, query: str, include_archived: bool = False) -> list[Note]:
        query = query.strip()
        if not query:
            return self.list_notes()

        archived_clause = "" if include_archived else " AND n.is_archived = 0"

        if self.has_fts:
            match = " ".join(f'"{token}"*' for token in re.findall(r"\w+", query))
            if match:
                try:
                    sql = (NOTE_SELECT +
                           " JOIN notes_fts ON notes_fts.rowid = n.id "
                           " WHERE notes_fts MATCH ?" + archived_clause +
                           " ORDER BY n.is_pinned DESC, bm25(notes_fts) ASC")
                    rows = self.conn.execute(sql, (match,)).fetchall()
                    return [Note.from_row(r) for r in rows]
                except sqlite3.Error:
                    pass

        like = f"%{query}%"
        sql = (NOTE_SELECT + " WHERE (n.title LIKE ? OR n.content LIKE ?)" + archived_clause +
               " ORDER BY n.is_pinned DESC, n.last_updated DESC")
        rows = self.conn.execute(sql, (like, like)).fetchall()
        return [Note.from_row(r) for r in rows]

    def archive_note(self, note_id: int) -> None:
        self.update_note(note_id, is_archived=1, touch=False)

    def restore_note(self, note_id: int) -> None:
        self.update_note(note_id, is_archived=0, touch=False)

    def duplicate_note(self, note_id: int) -> Optional[int]:
        note = self.get_note(note_id)
        if note is None:
            return None
        new_id = self.create_note(f"{note.display_title} (copy)", note.content,
                                  note.content_html, note.folder_id)
        self.update_note(new_id, color_token=note.color_token, editor_tint=note.editor_tint,
                         priority=note.priority)
        for tag_id, _name, _color in note.tags:
            self.add_tag_to_note(new_id, tag_id)
        return new_id

    def delete_note_permanent(self, note_id: int) -> None:
        for row in self.conn.execute("SELECT filepath FROM attachments WHERE note_id = ?", (note_id,)):
            try:
                Path(row["filepath"]).unlink(missing_ok=True)
            except OSError:
                pass
        self._execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.conn.commit()

    def empty_archive(self) -> int:
        rows = self.conn.execute("SELECT id FROM notes WHERE is_archived = 1").fetchall()
        for row in rows:
            self.delete_note_permanent(row["id"])
        return len(rows)

    def counts(self) -> dict[str, int]:
        c = self.conn
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "all": one("SELECT COUNT(*) FROM notes WHERE is_archived = 0"),
            "favorites": one("SELECT COUNT(*) FROM notes WHERE is_archived = 0 AND is_favorite = 1"),
            "pinned": one("SELECT COUNT(*) FROM notes WHERE is_archived = 0 AND is_pinned = 1"),
            "archive": one("SELECT COUNT(*) FROM notes WHERE is_archived = 1"),
            "unfiled": one("SELECT COUNT(*) FROM notes WHERE is_archived = 0 AND folder_id IS NULL"),
            "words": one("SELECT COALESCE(SUM(word_count), 0) FROM notes WHERE is_archived = 0"),
        }

    # -- folders ------------------------------------------------------------

    def create_folder(self, name: str, color: str = "default") -> Optional[int]:
        try:
            cur = self.conn.execute(
                "INSERT INTO folders (name, color, created_date) VALUES (?, ?, ?)",
                (name, color, self._now()),
            )
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def get_all_folders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT f.id, f.name, f.color, f.created_date,
                      (SELECT COUNT(*) FROM notes n
                        WHERE n.folder_id = f.id AND n.is_archived = 0) AS note_count
               FROM folders f ORDER BY f.name COLLATE NOCASE"""
        ).fetchall()

    def rename_folder(self, folder_id: int, name: str) -> bool:
        try:
            self.conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def set_folder_color(self, folder_id: int, color: str) -> None:
        self.conn.execute("UPDATE folders SET color = ? WHERE id = ?", (color, folder_id))
        self.conn.commit()

    def delete_folder(self, folder_id: int) -> None:
        self.conn.execute("UPDATE notes SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
        self.conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        self.conn.commit()

    # -- tags ---------------------------------------------------------------

    def create_tag(self, name: str, color: str = "indigo") -> Optional[int]:
        try:
            cur = self.conn.execute("INSERT INTO tags (name, color) VALUES (?, ?)", (name, color))
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            row = self.conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                                    (name,)).fetchone()
            return int(row["id"]) if row else None

    def get_all_tags(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT t.id, t.name, t.color,
                      (SELECT COUNT(*) FROM note_tags nt JOIN notes n ON n.id = nt.note_id
                        WHERE nt.tag_id = t.id AND n.is_archived = 0) AS note_count
               FROM tags t ORDER BY t.name COLLATE NOCASE"""
        ).fetchall()

    def get_note_tags(self, note_id: int) -> list[tuple[int, str, str]]:
        rows = self.conn.execute(
            """SELECT t.id, t.name, t.color FROM tags t
               JOIN note_tags nt ON t.id = nt.tag_id WHERE nt.note_id = ?
               ORDER BY t.name COLLATE NOCASE""",
            (note_id,),
        ).fetchall()
        return [(r["id"], r["name"], r["color"] or "indigo") for r in rows]

    def add_tag_to_note(self, note_id: int, tag_id: int) -> bool:
        try:
            self.conn.execute("INSERT INTO note_tags (note_id, tag_id) VALUES (?, ?)",
                              (note_id, tag_id))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_tag_from_note(self, note_id: int, tag_id: int) -> None:
        self.conn.execute("DELETE FROM note_tags WHERE note_id = ? AND tag_id = ?",
                          (note_id, tag_id))
        self.conn.commit()

    def delete_tag(self, tag_id: int) -> None:
        self.conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        self.conn.commit()

    def set_tag_color(self, tag_id: int, color: str) -> None:
        self.conn.execute("UPDATE tags SET color = ? WHERE id = ?", (color, tag_id))
        self.conn.commit()

    # -- attachments & links -------------------------------------------------

    def add_attachment(self, note_id: int, filepath: str) -> Optional[int]:
        source = Path(filepath)
        if not source.is_file():
            return None
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        destination = self.attachments_path / f"{note_id}_{stamp}_{source.name}"
        shutil.copy2(source, destination)
        cur = self.conn.execute(
            """INSERT INTO attachments (note_id, filename, filepath, file_type, file_size, added_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (note_id, source.name, str(destination), source.suffix.lower(),
             destination.stat().st_size, self._now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_note_attachments(self, note_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT id, filename, filepath, file_type, file_size, added_date
               FROM attachments WHERE note_id = ? ORDER BY added_date""",
            (note_id,),
        ).fetchall()

    def delete_attachment(self, attachment_id: int) -> None:
        row = self.conn.execute("SELECT filepath FROM attachments WHERE id = ?",
                                (attachment_id,)).fetchone()
        if row:
            try:
                Path(row["filepath"]).unlink(missing_ok=True)
            except OSError:
                pass
        self.conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        self.conn.commit()

    def add_link(self, note_id: int, url: str, title: Optional[str] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO links (note_id, url, title, added_date) VALUES (?, ?, ?, ?)",
            (note_id, url, title, self._now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_note_links(self, note_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, url, title, added_date FROM links WHERE note_id = ? ORDER BY added_date",
            (note_id,),
        ).fetchall()

    def delete_link(self, link_id: int) -> None:
        self.conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
        self.conn.commit()

    # -- backup -------------------------------------------------------------

    def backup_to(self, target: str | Path) -> Path:
        target = Path(target)
        self.conn.commit()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            snapshot = self.app_data_path / "_backup_snapshot.db"
            try:
                backup_conn = sqlite3.connect(str(snapshot))
                with backup_conn:
                    self.conn.backup(backup_conn)
                backup_conn.close()
                archive.write(snapshot, "notes.db")
            finally:
                snapshot.unlink(missing_ok=True)
            for path in self.attachments_path.iterdir():
                if path.is_file():
                    archive.write(path, f"attachments/{path.name}")
        return target


# ---------------------------------------------------------------------------
# Rich text content pipeline
# ---------------------------------------------------------------------------

class ContentPipeline:
    """
    Keeps note content legible across themes.

    The original low-contrast bug came from Qt serialising the *current* palette
    colour into the saved HTML: a note written in dark mode carried
    `color:#e0e0e0` on its body tag, so reopening it on a white light-mode
    surface rendered near-invisible text. Two passes fix that permanently:

    1. `sanitize_for_storage` strips theme-derived colours out of the HTML
       before it is written to disk, so stored notes are theme-neutral.
    2. `retheme_document` runs on load. Neutral greys inherited from an old
       theme are dropped so the text follows the palette, while deliberate
       author colours keep their hue and saturation and are only nudged in
       lightness until they clear 4.5:1 against the page they sit on.
    """

    # Colours previously baked in by NoteCraft or by Qt's default palettes.
    NEUTRAL_LEGACY = {
        "#e0e0e0", "#a0a0e0", "#a0a0a0", "#808080", "#212529", "#6c757d",
        "#e7eaf0", "#131924", "#9ba6b7", "#54607a", "#ffffff", "#f8f9fa",
        "#1a1a1a", "#2b2b2b", "#353535",
    }

    BODY_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
    COLOR_DECL_RE = re.compile(r"\s*(?:background-)?color\s*:\s*[^;\"']+;?", re.IGNORECASE)

    NEUTRAL_SATURATION = 46   # 0-255; below this a colour is treated as theme grey
    MIN_CONTRAST = 4.5

    # -- storage -------------------------------------------------------------

    @classmethod
    def sanitize_for_storage(cls, html: str) -> str:
        """Remove palette-derived colours from the serialized document."""
        if not html:
            return ""

        def scrub_body(match: re.Match) -> str:
            return cls.COLOR_DECL_RE.sub("", match.group(0))

        cleaned = cls.BODY_TAG_RE.sub(scrub_body, html)

        # Drop span-level colours that merely echo a theme's body text colour.
        def scrub_span(match: re.Match) -> str:
            value = match.group(2).strip().lower()
            if value in cls.NEUTRAL_LEGACY:
                return ""
            return match.group(0)

        cleaned = re.sub(r"(\bcolor\s*:\s*)(#[0-9a-fA-F]{6})\s*;?", scrub_span, cleaned)
        return cleaned

    # -- rendering -----------------------------------------------------------

    @classmethod
    def _adapt_surface(cls, color: QColor, page: QColor) -> QColor:
        """
        Flip a background that sits on the wrong side of the page.

        A highlight or code block authored in dark mode would otherwise stay a
        dark slab on a white page. Hue and saturation are preserved and the
        target lightness depends only on the page, so switching back and forth
        returns the same two colours rather than drifting.
        """
        if is_light(color) == is_light(page):
            return QColor(color)
        h, sat, _lightness, alpha = color.getHslF()
        if h < 0:
            h = 0.0
        return QColor.fromHslF(h, sat, 0.88 if is_light(page) else 0.20, alpha)


    @classmethod
    def _is_neutral(cls, color: QColor) -> bool:
        return color.saturation() < cls.NEUTRAL_SATURATION

    @classmethod
    def retheme_document(cls, document: QTextDocument, theme: Theme, page_bg: str) -> None:
        """Adapt every colour in the document to the given page background."""
        bg = QColor(page_bg)
        text_color = QColor(theme["text"])

        undo_was_enabled = document.isUndoRedoEnabled()
        document.setUndoRedoEnabled(False)
        document.blockSignals(True)
        try:
            edits: list[tuple[int, int, QTextCharFormat]] = []

            block = document.begin()
            while block.isValid():
                iterator = block.begin()
                while not iterator.atEnd():
                    fragment = iterator.fragment()
                    if fragment.isValid():
                        fmt = QTextCharFormat(fragment.charFormat())
                        changed = False

                        has_bg = fmt.background().style() != Qt.BrushStyle.NoBrush
                        highlight = fmt.background().color() if has_bg else None
                        if has_bg and highlight is not None and highlight.alpha() == 0:
                            has_bg, highlight = False, None

                        if has_bg and highlight is not None:
                            adapted = cls._adapt_surface(highlight, bg)
                            if adapted != highlight:
                                fmt.setBackground(QBrush(adapted))
                                highlight = adapted
                                changed = True

                        local_bg = highlight if has_bg else bg

                        if fmt.foreground().style() != Qt.BrushStyle.NoBrush:
                            fg = fmt.foreground().color()
                            if cls._is_neutral(fg):
                                if has_bg:
                                    fmt.setForeground(QBrush(QColor(readable_on(local_bg))))
                                else:
                                    fmt.clearForeground()
                                changed = True
                            else:
                                adjusted = ensure_contrast(fg, local_bg, cls.MIN_CONTRAST)
                                if adjusted != fg:
                                    fmt.setForeground(QBrush(adjusted))
                                    changed = True
                        elif has_bg:
                            # Highlighted run with inherited text colour: make sure the
                            # inherited colour still reads on the highlight.
                            if contrast_ratio(text_color, local_bg) < cls.MIN_CONTRAST:
                                fmt.setForeground(QBrush(QColor(readable_on(local_bg))))
                                changed = True

                        if changed:
                            edits.append((fragment.position(), fragment.length(), fmt))
                    iterator += 1
                block = block.next()

            if edits:
                cursor = QTextCursor(document)
                cursor.beginEditBlock()
                for position, length, fmt in edits:
                    cursor.setPosition(position)
                    cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
                    cursor.setCharFormat(fmt)
                cursor.endEditBlock()

            cls._retheme_blocks(document, theme, bg)
            cls._retheme_tables(document, theme)
        finally:
            document.blockSignals(False)
            document.setUndoRedoEnabled(undo_was_enabled)

    @classmethod
    def _retheme_blocks(cls, document: QTextDocument, theme: Theme,
                        page: Optional[QColor] = None) -> None:
        """Re-tint block backgrounds (code blocks, callouts) for the active theme."""
        code_bg = QColor(theme["code_bg"])
        page = page if page is not None else QColor(theme["surface"])
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        block = document.begin()
        touched = False
        while block.isValid():
            block_format = block.blockFormat()
            brush = block_format.background()
            if brush.style() != Qt.BrushStyle.NoBrush and brush.color().alpha() > 0:
                existing = brush.color()
                if existing.saturation() < cls.NEUTRAL_SATURATION:
                    replacement = code_bg          # a neutral slab is our code block
                else:
                    replacement = cls._adapt_surface(existing, page)
                if replacement != existing:
                    block_format.setBackground(QBrush(replacement))
                    cursor.setPosition(block.position())
                    cursor.setBlockFormat(block_format)
                    touched = True
            block = block.next()
        cursor.endEditBlock()
        if not touched:
            return

    @classmethod
    def _retheme_tables(cls, document: QTextDocument, theme: Theme) -> None:
        """Repaint table chrome so grid lines never vanish into the page."""
        from PyQt6.QtGui import QTextTable

        frames = [document.rootFrame()]
        while frames:
            frame = frames.pop()
            for child in frame.childFrames():
                frames.append(child)
                if isinstance(child, QTextTable):
                    fmt = child.format()
                    fmt.setBorderBrush(QBrush(QColor(theme["border_strong"])))
                    fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                    if fmt.border() < 1:
                        fmt.setBorder(1)
                    fmt.setBackground(QBrush(Qt.GlobalColor.transparent))
                    child.setFormat(fmt)

                    header_bg = QColor(theme["surface_alt"])
                    if child.rows() > 0:
                        for column in range(child.columns()):
                            cell = child.cellAt(0, column)
                            if not cell.isValid():
                                continue
                            cell_fmt = cell.format()
                            if cell_fmt.background().style() != Qt.BrushStyle.NoBrush:
                                cell_fmt.setBackground(QBrush(header_bg))
                                cell.setFormat(cell_fmt)

    # -- export --------------------------------------------------------------

    @classmethod
    def to_portable_html(cls, content_html: str, plain_text: str = "") -> str:
        """
        Produce neutral, light-background HTML suitable for email or export.

        Runs the same contrast guard against a white page, so a note authored in
        dark mode never arrives in someone's inbox as white-on-white.
        """
        document = QTextDocument()
        if content_html:
            document.setHtml(cls.sanitize_for_storage(content_html))
        else:
            document.setPlainText(plain_text or "")

        export_theme = build_theme("light")
        cls.retheme_document(document, export_theme, "#FFFFFF")

        raw = document.toHtml()
        body_match = re.search(r"<body[^>]*>(.*)</body>", raw, re.DOTALL | re.IGNORECASE)
        body = body_match.group(1) if body_match else raw

        body = re.sub(
            r"<table[^>]*>",
            '<table border="1" cellspacing="0" cellpadding="8" '
            'style="border-collapse:collapse;width:100%;margin:14px 0;'
            'border:1px solid #B7BDC9;">',
            body,
        )
        body = re.sub(r"<td(?![^>]*style=)", '<td style="border:1px solid #B7BDC9;padding:8px 12px;"',
                      body)
        body = re.sub(r"<th(?![^>]*style=)",
                      '<th style="border:1px solid #B7BDC9;padding:8px 12px;'
                      'background-color:#EDF0F5;font-weight:bold;"', body)
        return body


# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------

def clear_layout(layout, keep: int = 0) -> None:
    """
    Empty a layout completely.

    `deleteLater` alone is not enough: `takeAt` removes the widget from the
    layout but leaves it parented and visible until the event loop catches up,
    which is what leaves ghost labels floating over the sidebar. Unparenting
    first makes the removal immediate.
    """
    while layout.count() > keep:
        item = layout.takeAt(0)
        if item is None:
            break
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)
            child.setParent(None)
            child.deleteLater()


def apply_icon(widget: QWidget, name: str, role: str = "text", size: int = 18) -> None:
    """Tag a widget with an icon spec so it can be re-tinted on theme change."""
    widget._icon_spec = (name, role, size)  # type: ignore[attr-defined]


def tint_icon(widget: QWidget, theme: Theme) -> None:
    """Re-draw a widget's icon in the right token colour for its current state."""
    spec = getattr(widget, "_icon_spec", None)
    if not spec or not hasattr(widget, "setIcon"):
        return
    name, role, size = spec
    checked = bool(widget.isChecked()) if hasattr(widget, "isChecked") else False
    color = theme["on_accent"] if checked else theme.get(role, theme["text"])
    widget.setIcon(Icons.get(name, color, size))
    if hasattr(widget, "setIconSize"):
        widget.setIconSize(QSize(size, size))


def refresh_icons(root: QWidget, theme: Theme) -> None:
    Icons.clear_cache()
    for widget in root.findChildren(QWidget):
        tint_icon(widget, theme)


def tool_button(icon_name: str, tooltip: str, callback: Optional[Callable] = None,
                checkable: bool = False, size: int = 32, icon_size: int = 18,
                role: str = "text") -> QToolButton:
    button = QToolButton()
    button.setFixedSize(size, size)
    button.setToolTip(tooltip)
    button.setCheckable(checkable)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    apply_icon(button, icon_name, role, icon_size)
    if checkable:
        # A checked tool button sits on the accent fill, so its glyph has to
        # switch to the on-accent token or it drops below AA.
        def _restate(_checked: bool, target=button) -> None:
            theme = getattr(target.window(), "theme", None)
            if theme is not None:
                tint_icon(target, theme)
        button.toggled.connect(_restate)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def push_button(text: str, variant: str = "default", icon_name: str = "",
                callback: Optional[Callable] = None) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("variant", variant)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name:
        role = "on_accent" if variant == "primary" else "text"
        apply_icon(button, icon_name, role, 17)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def divider(horizontal: bool = True) -> QFrame:
    line = QFrame()
    line.setObjectName("Divider" if horizontal else "VDivider")
    line.setFixedHeight(1) if horizontal else line.setFixedWidth(1)
    return line


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def friendly_date(stamp: str) -> str:
    """Render a timestamp as 'Today 14:32', 'Yesterday', '12 Mar' or '12 Mar 2024'."""
    if not stamp:
        return ""
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp.split()[0]
    now = datetime.now()
    delta_days = (now.date() - moment.date()).days
    if delta_days == 0:
        return f"Today {moment.strftime('%H:%M')}"
    if delta_days == 1:
        return "Yesterday"
    if delta_days < 7:
        return moment.strftime("%A")
    if moment.year == now.year:
        return moment.strftime("%d %b")
    return moment.strftime("%d %b %Y")


class FlowLayout(QVBoxLayout):
    """Minimal wrapping row container built from nested layouts."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)
        self._spacing = spacing
        self._row: Optional[QHBoxLayout] = None
        self._row_width = 0
        self._max_width = 320

    def set_max_width(self, width: int) -> None:
        self._max_width = max(120, width)

    def clear(self) -> None:
        clear_layout(self)
        self._row = None
        self._row_width = 0

    def add(self, widget: QWidget) -> None:
        width = widget.sizeHint().width() + self._spacing
        if self._row is None or self._row_width + width > self._max_width:
            self._row = QHBoxLayout()
            self._row.setContentsMargins(0, 0, 0, 0)
            self._row.setSpacing(self._spacing)
            self._row.addStretch()
            self.addLayout(self._row)
            self._row_width = 0
        self._row.insertWidget(self._row.count() - 1, widget)
        self._row_width += width


class TagChip(QFrame):
    """A compact, always-legible tag pill."""

    clicked = pyqtSignal()
    removed = pyqtSignal()

    def __init__(self, name: str, accent: str, theme: Theme,
                 removable: bool = False, parent=None):
        super().__init__(parent)
        self.name = name
        self._accent = accent
        self._theme = theme
        self._removable = removable
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(22)

        metrics = QFontMetrics(self._chip_font())
        width = metrics.horizontalAdvance(name) + (34 if removable else 20)
        self.setFixedWidth(min(200, width))
        self.setToolTip(name)

    def _chip_font(self) -> QFont:
        font = QFont(QApplication.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.2))
        font.setBold(True)
        return font

    def _colors(self) -> tuple[QColor, QColor]:
        pair = ACCENT_CHOICES.get(self._accent, ACCENT_CHOICES["indigo"])
        hue = pair[0] if self._theme.is_dark else pair[1]
        background = QColor(mix(hue, self._theme["surface"], 0.80 if self._theme.is_dark else 0.86))
        foreground = ensure_contrast(QColor(hue), background, 4.5)
        return background, foreground

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        background, foreground = self._colors()

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(mix(background.name(), foreground.name(), 0.22)), 1))
        painter.setBrush(QBrush(background))
        painter.drawRoundedRect(rect, 8, 8)

        painter.setFont(self._chip_font())
        painter.setPen(QPen(foreground))
        text_rect = rect.adjusted(9, 0, -(22 if self._removable else 8), 0)
        metrics = QFontMetrics(self._chip_font())
        label = metrics.elidedText(self.name, Qt.TextElideMode.ElideRight,
                                   int(text_rect.width()))
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         label)

        if self._removable:
            painter.setPen(QPen(foreground, 1.4))
            cx, cy = rect.right() - 11, rect.center().y()
            painter.drawLine(QLineF(cx - 3, cy - 3, cx + 3, cy + 3))
            painter.drawLine(QLineF(cx + 3, cy - 3, cx - 3, cy + 3))
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._removable and event.position().x() > self.width() - 22:
                self.removed.emit()
            else:
                self.clicked.emit()
        super().mousePressEvent(event)


class NoteCard(QFrame):
    """
    A fully custom-painted note row.

    Painting by hand (rather than nesting styled child widgets) means the card's
    colours are derived from the live theme on every repaint, so a theme switch
    can never leave a card with stale text or an unreadable tint. It also keeps
    scrolling smooth with thousands of notes.
    """

    clicked = pyqtSignal(int)
    context_requested = pyqtSignal(int, QPoint)

    HEIGHTS = {"compact": 66, "comfortable": 92}

    def __init__(self, note: Note, theme: Theme, density: str = "comfortable", parent=None):
        super().__init__(parent)
        self.note = note
        self.theme = theme
        self.density = density
        self._hovered = False
        self._selected = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.HEIGHTS.get(density, 92))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(self.note.id, self.mapToGlobal(pos))
        )
        self.setToolTip(note.display_title)

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.note.id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.clicked.emit(self.note.id)
        super().mouseDoubleClickEvent(event)

    # -- painting -----------------------------------------------------------

    def _background(self) -> QColor:
        base = self.theme.note_tint(self.note.color_token)
        if self._selected:
            return QColor(mix(base, self.theme["accent"], 0.18))
        if self._hovered:
            weight = 0.06 if self.theme.is_dark else 0.05
            target = "#FFFFFF" if self.theme.is_dark else "#000000"
            return QColor(mix(base, target, weight))
        return QColor(base)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        background = self._background()
        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)

        if self._selected:
            border, width = QColor(self.theme["accent"]), 1.5
        elif self._hovered:
            border, width = QColor(self.theme["border_strong"]), 1.0
        else:
            border, width = QColor(self.theme["border"]), 1.0

        painter.setPen(QPen(border, width))
        painter.setBrush(QBrush(background))
        painter.drawRoundedRect(rect, 10, 10)

        # Colour rail
        left = 12.0
        rail = self.theme.note_rail(self.note.color_token)
        if rail != "transparent":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(rail)))
            painter.drawRoundedRect(QRectF(rect.left() + 4, rect.top() + 9,
                                           3.5, rect.height() - 18), 1.75, 1.75)
            left = 16.0

        text_color = ensure_contrast(QColor(self.theme["text"]), background, 4.5)
        muted_color = ensure_contrast(QColor(self.theme["text_muted"]), background, 4.5)
        faint_color = ensure_contrast(QColor(self.theme["text_faint"]), background, 4.0)

        base_font = QFont(QApplication.font())
        right_edge = rect.right() - 10

        # Badges, laid out right to left
        badge_x = right_edge
        badges: list[tuple[str, str]] = []
        if self.note.is_pinned:
            badges.append(("pin_filled", self.theme["accent"]))
        if self.note.is_favorite:
            badges.append(("star_filled", self.theme["warning"]))
        if self.note.attachment_count:
            badges.append(("paperclip", self.theme["text_muted"]))
        for name, color in badges:
            pixmap = Icons.pixmap(name, ensure_contrast(QColor(color), background, 3.0).name(), 14)
            badge_x -= 18
            painter.drawPixmap(int(badge_x), int(rect.top() + 11), pixmap)

        # Priority dot
        if self.note.priority and self.note.priority != "None":
            dot = ensure_contrast(QColor(self.theme.priority_color(self.note.priority)),
                                  background, 3.0)
            badge_x -= 14
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(dot))
            painter.drawEllipse(QRectF(badge_x + 3, rect.top() + 14, 7, 7))

        # Title
        title_font = QFont(base_font)
        title_font.setPointSizeF(base_font.pointSizeF() + 0.6)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(text_color))
        metrics = QFontMetrics(title_font)
        title_rect = QRectF(rect.left() + left, rect.top() + 8,
                            badge_x - rect.left() - left - 8, 20)
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(self.note.display_title, Qt.TextElideMode.ElideRight,
                               int(max(20, title_rect.width()))),
        )

        # Snippet
        small_font = QFont(base_font)
        small_font.setPointSizeF(max(7.5, base_font.pointSizeF() - 0.8))
        small_metrics = QFontMetrics(small_font)

        if self.density == "comfortable":
            snippet = " ".join((self.note.content or "").split())
            if snippet:
                painter.setFont(small_font)
                painter.setPen(QPen(muted_color))
                snippet_rect = QRectF(rect.left() + left, rect.top() + 30,
                                      rect.width() - left - 18, 18)
                painter.drawText(
                    snippet_rect,
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    small_metrics.elidedText(snippet, Qt.TextElideMode.ElideRight,
                                             int(max(20, snippet_rect.width()))),
                )

        # Meta line
        parts = [friendly_date(self.note.last_updated)]
        if self.note.word_count:
            parts.append(f"{self.note.word_count:,} words")
        if self.note.folder_name:
            parts.append(self.note.folder_name)
        meta = "  ·  ".join(part for part in parts if part)

        painter.setFont(small_font)
        painter.setPen(QPen(faint_color))
        meta_rect = QRectF(rect.left() + left, rect.bottom() - 24,
                           rect.width() - left - 18, 18)
        painter.drawText(
            meta_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            small_metrics.elidedText(meta, Qt.TextElideMode.ElideRight,
                                     int(max(20, meta_rect.width()))),
        )
        painter.end()


class CommandPalette(QDialog):
    """Fuzzy launcher for every action and every note (Ctrl+K)."""

    def __init__(self, parent, commands: list[tuple[str, str, Callable]], notes: list[Note]):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("CommandPalette")
        self._commands = commands
        self._notes = notes
        self._results: list[tuple[str, str, Callable]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.query = QLineEdit()
        self.query.setPlaceholderText("Type a command or search your notes…")
        self.query.setMinimumHeight(38)
        self.query.textChanged.connect(self._refresh)
        layout.addWidget(self.query)

        self.results = QListWidget()
        self.results.setMinimumHeight(300)
        self.results.itemActivated.connect(self._activate)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.results)

        hint = QLabel("↑↓ to navigate · Enter to run · Esc to close")
        hint.setObjectName("Faint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self.setFixedWidth(min(640, max(420, parent.width() - 240)))
        self.query.installEventFilter(self)
        self._refresh("")

    @staticmethod
    def _score(needle: str, haystack: str) -> int:
        """Subsequence match; higher is better, -1 means no match."""
        if not needle:
            return 0
        needle, haystack = needle.lower(), haystack.lower()
        if needle in haystack:
            return 1000 - haystack.index(needle)
        index, score, streak = 0, 0, 0
        for char in haystack:
            if index < len(needle) and char == needle[index]:
                index += 1
                streak += 1
                score += 10 + streak * 2
            else:
                streak = 0
        return score if index == len(needle) else -1

    def _refresh(self, text: str = "") -> None:
        text = self.query.text().strip()
        scored: list[tuple[int, tuple[str, str, Callable]]] = []

        for label, hint, callback in self._commands:
            score = self._score(text, label)
            if score >= 0:
                scored.append((score + 40, (label, hint, callback)))

        if text:
            for note in self._notes[:400]:
                score = self._score(text, note.display_title)
                if score >= 0:
                    scored.append((score, (note.display_title, "Note", None)))

        scored.sort(key=lambda item: -item[0])
        self._results = []
        self.results.clear()

        for _score, entry in scored[:60]:
            label, hint, callback = entry
            if callback is None:
                matching = next((n for n in self._notes if n.display_title == label), None)
                if matching is None:
                    continue
                note_id = matching.id
                parent = self.parent()
                callback = lambda nid=note_id: parent.open_note(nid)  # noqa: E731
            self._results.append((label, hint, callback))
            item = QListWidgetItem(f"{label}     ·  {hint}" if hint else label)
            self.results.addItem(item)

        if self.results.count():
            self.results.setCurrentRow(0)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.query and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.results.currentRow()
                delta = 1 if key == Qt.Key.Key_Down else -1
                self.results.setCurrentRow(
                    max(0, min(self.results.count() - 1, row + delta)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._activate()
                return True
        return super().eventFilter(obj, event)

    def _activate(self, _item=None) -> None:
        row = self.results.currentRow()
        if 0 <= row < len(self._results):
            callback = self._results[row][2]
            self.accept()
            QTimer.singleShot(0, callback)


class FindReplaceBar(QWidget):
    """Inline find & replace strip for the editor (Ctrl+F inside a note, Ctrl+H)."""

    closed = pyqtSignal()

    def __init__(self, editor: QTextEdit, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.setObjectName("InlinePanel")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(7)

        self.find_field = QLineEdit()
        self.find_field.setPlaceholderText("Find in note")
        self.find_field.setMinimumWidth(160)
        self.find_field.textChanged.connect(self._highlight_all)
        self.find_field.returnPressed.connect(self.find_next)
        layout.addWidget(self.find_field, 2)

        self.replace_field = QLineEdit()
        self.replace_field.setPlaceholderText("Replace with")
        self.replace_field.setMinimumWidth(160)
        layout.addWidget(self.replace_field, 2)

        self.match_label = QLabel("")
        self.match_label.setObjectName("Faint")
        self.match_label.setMinimumWidth(70)
        layout.addWidget(self.match_label)

        self.case_box = QCheckBox("Aa")
        self.case_box.setToolTip("Match case")
        self.case_box.stateChanged.connect(self._highlight_all)
        layout.addWidget(self.case_box)

        layout.addWidget(tool_button("chevron_left", "Previous match (Shift+Enter)",
                                     self.find_previous, size=28, icon_size=16))
        layout.addWidget(tool_button("chevron_right", "Next match (Enter)",
                                     self.find_next, size=28, icon_size=16))

        self.replace_button = push_button("Replace", "default", "", self.replace_one)
        layout.addWidget(self.replace_button)
        self.replace_all_button = push_button("All", "default", "", self.replace_all)
        layout.addWidget(self.replace_all_button)

        layout.addWidget(tool_button("close", "Close (Esc)", self.dismiss, size=28, icon_size=15))

    def activate(self, replace_mode: bool = False) -> None:
        self.show()
        self.replace_field.setVisible(replace_mode)
        self.replace_button.setVisible(replace_mode)
        self.replace_all_button.setVisible(replace_mode)
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            self.find_field.setText(cursor.selectedText())
        self.find_field.setFocus()
        self.find_field.selectAll()
        self._highlight_all()

    def dismiss(self) -> None:
        self.editor.setExtraSelections([])
        self.hide()
        self.editor.setFocus()
        self.closed.emit()

    def _flags(self, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self.case_box.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _highlight_all(self) -> None:
        needle = self.find_field.text()
        selections: list[QTextEdit.ExtraSelection] = []
        if not needle:
            self.editor.setExtraSelections([])
            self.match_label.setText("")
            return

        theme: Theme = self.window().theme  # type: ignore[attr-defined]
        highlight = QColor(theme["accent_soft"])
        document = self.editor.document()
        cursor = QTextCursor(document)
        count = 0
        while True:
            cursor = document.find(needle, cursor, self._flags())
            if cursor.isNull():
                break
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(QBrush(highlight))
            selection.format.setForeground(QBrush(QColor(readable_on(highlight))))
            selections.append(selection)
            count += 1
            if count > 2000:
                break

        self.editor.setExtraSelections(selections)
        self.match_label.setText(f"{count} match{'es' if count != 1 else ''}")

    def find_next(self) -> None:
        self._step(False)

    def find_previous(self) -> None:
        self._step(True)

    def _step(self, backward: bool) -> None:
        needle = self.find_field.text()
        if not needle:
            return
        if not self.editor.find(needle, self._flags(backward)):
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End if backward
                                else QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            self.editor.find(needle, self._flags(backward))

    def replace_one(self) -> None:
        cursor = self.editor.textCursor()
        needle = self.find_field.text()
        if cursor.hasSelection() and cursor.selectedText() == needle:
            cursor.insertText(self.replace_field.text())
        self.find_next()
        self._highlight_all()

    def replace_all(self) -> None:
        needle = self.find_field.text()
        if not needle:
            return
        replacement = self.replace_field.text()
        document = self.editor.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        finder = QTextCursor(document)
        replaced = 0
        while True:
            finder = document.find(needle, finder, self._flags())
            if finder.isNull():
                break
            finder.insertText(replacement)
            replaced += 1
        cursor.endEditBlock()
        self._highlight_all()
        window = self.window()
        if hasattr(window, "toast"):
            window.toast(f"Replaced {replaced} occurrence{'s' if replaced != 1 else ''}")


class NavButton(QPushButton):
    """Custom-painted sidebar row: icon, label, trailing count, active state."""

    context_requested = pyqtSignal(QPoint)

    def __init__(self, icon_name: str, label: str, theme: Theme,
                 count: Optional[int] = None, indent: int = 0, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self.label = label
        self.theme = theme
        self.count = count
        self.indent = indent
        self.active = False
        self.accent_dot = ""
        self.setFixedHeight(34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(label)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(self.mapToGlobal(pos)))

    def set_active(self, active: bool) -> None:
        if self.active != active:
            self.active = active
            self.update()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def set_count(self, count: Optional[int]) -> None:
        self.count = count
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self.active:
            background = QColor(self.theme["accent_soft"])
            label_color = ensure_contrast(QColor(self.theme["accent"]), background, 4.5)
        elif self.underMouse():
            background = QColor(self.theme["surface_alt"])
            label_color = QColor(self.theme["text"])
        else:
            background = None
            label_color = QColor(self.theme["text"])

        if background is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(background))
            painter.drawRoundedRect(rect, 8, 8)
        else:
            background = QColor(self.theme["surface"])

        x = rect.left() + 10 + self.indent

        if self.accent_dot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(ensure_contrast(QColor(self.accent_dot), background, 3.0)))
            painter.drawEllipse(QRectF(x + 2, rect.center().y() - 3.5, 7, 7))
            x += 18
        elif self.icon_name:
            pixmap = Icons.pixmap(self.icon_name, label_color.name(), 17)
            painter.drawPixmap(int(x), int(rect.center().y() - 8.5), pixmap)
            x += 26

        font = QFont(QApplication.font())
        font.setBold(self.active)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        count_width = 0.0
        if self.count is not None:
            count_font = QFont(QApplication.font())
            count_font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
            count_metrics = QFontMetrics(count_font)
            text = f"{self.count:,}"
            count_width = count_metrics.horizontalAdvance(text) + 14
            painter.setFont(count_font)
            painter.setPen(QPen(ensure_contrast(QColor(self.theme["text_faint"]),
                                                background, 4.0)))
            painter.drawText(
                QRectF(rect.right() - count_width, rect.top(), count_width - 6, rect.height()),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), text)
            painter.setFont(font)

        painter.setPen(QPen(label_color))
        available = rect.right() - x - count_width - 6
        painter.drawText(
            QRectF(x, rect.top(), max(20.0, available), rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(self.label, Qt.TextElideMode.ElideRight, int(max(20, available))),
        )
        painter.end()



class NoteEditor(QTextEdit):
    """QTextEdit that accepts dropped files and keeps paste behaviour predictable."""

    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setTabStopDistance(32)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        if source.hasUrls():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasUrls():
            paths = [url.toLocalFile() for url in source.urls() if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                return
        super().insertFromMimeData(source)

class NoteCraftWindow(QMainWindow):
    """Main application window."""

    AUTOSAVE_MS = 900

    def __init__(self, database: NotesDatabase):
        super().__init__()
        self.db = database
        self.settings = QSettings(ORG_NAME, APP_NAME)

        # --- persisted preferences -----------------------------------------
        self.theme_mode = self.settings.value("appearance/mode", "dark", str)
        self.accent_key = self.settings.value("appearance/accent", "indigo", str)
        self.ui_font_family = self.settings.value("appearance/font", "Segoe UI", str)
        self.ui_font_size = int(self.settings.value("appearance/size", 10))
        self.density = self.settings.value("list/density", "comfortable", str)
        self.sort_mode = self.settings.value("list/sort", "updated", str)
        self.editor_font_family = self.settings.value("editor/font", "Segoe UI", str)
        self.editor_font_size = int(self.settings.value("editor/size", 12))

        if self.ui_font_family not in QFontDatabase.families():
            self.ui_font_family = QApplication.font().family()

        self.theme = build_theme(self._resolved_mode(), self.accent_key)

        # --- runtime state --------------------------------------------------
        self.current_note: Optional[Note] = None
        self.scope = "all"
        self.scope_folder_id: Optional[int] = None
        self.scope_tag_id: Optional[int] = None
        self.search_query = ""
        self.cards: dict[int, NoteCard] = {}
        self.focus_mode = False
        self._loading = False
        self._dirty = False
        self._last_sizes = [270, 340, 780]

        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.timeout.connect(self.save_current_note)

        self.status_timer = QTimer(self)
        self.status_timer.setSingleShot(True)

        self._build_ui()
        self._build_shortcuts()
        self.apply_theme()
        self._restore_geometry()

        self.refresh_sidebar()
        self.refresh_list()
        self._restore_last_note()

        if not self.db.file_is_intact():
            QTimer.singleShot(300, lambda: self.info(
                "Database needs attention",
                "The notes file failed an integrity check. Copy this folder somewhere safe "
                f"before making further changes:\n\n{self.db.app_data_path}"))
        elif self.db.startup_notice:
            QTimer.singleShot(200, lambda: self.toast(self.db.startup_notice, 6000))

    # -- appearance helpers -------------------------------------------------

    def _resolved_mode(self) -> str:
        if self.theme_mode == "system":
            try:
                scheme = QGuiApplication.styleHints().colorScheme()
                return "light" if scheme == Qt.ColorScheme.Light else "dark"
            except Exception:
                return "dark"
        return self.theme_mode if self.theme_mode in ("dark", "light") else "dark"

    def toast(self, message: str, msecs: int = 2600) -> None:
        self.status_bar.showMessage(message, msecs)

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} — Premium Notes")
        self.setMinimumSize(940, 600)
        self.resize(1440, 920)
        self.setAcceptDrops(True)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(True)

        self.sidebar = self._build_sidebar()
        self.list_pane = self._build_list_pane()
        self.editor_pane = self._build_editor_pane()

        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.list_pane)
        self.splitter.addWidget(self.editor_pane)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setCollapsible(2, False)
        self.splitter.setSizes(self._last_sizes)
        self.splitter.splitterMoved.connect(lambda *_: self._sync_view_toggles())

        root.addWidget(self.splitter)

        self.status_bar = QStatusBar()
        self.status_bar.setSizeGripEnabled(True)
        self.setStatusBar(self.status_bar)

        self.stat_label = QLabel("")
        self.stat_label.setObjectName("Faint")
        self.status_bar.addPermanentWidget(self.stat_label)
        self.toast("Ready")

    # -- sidebar ------------------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Sidebar")
        panel.setMinimumWidth(0)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 14, 12)
        layout.setSpacing(10)

        # Brand
        brand_row = QHBoxLayout()
        brand_row.setSpacing(9)
        self.brand_icon = QLabel()
        self.brand_icon.setFixedSize(26, 26)
        self.brand_icon.setPixmap(render_app_icon(26))
        brand_row.addWidget(self.brand_icon)

        brand = QLabel(APP_NAME)
        brand.setObjectName("BrandMark")
        brand_row.addWidget(brand)
        brand_row.addStretch()

        self.collapse_sidebar_button = tool_button(
            "sidebar", "Hide navigation (Ctrl+1)", self.toggle_sidebar,
            size=28, icon_size=16, role="text_muted")
        brand_row.addWidget(self.collapse_sidebar_button)
        layout.addLayout(brand_row)

        # New note
        self.new_note_button = push_button("New note", "primary", "plus", self.create_new_note)
        self.new_note_button.setFixedHeight(38)
        layout.addWidget(self.new_note_button)

        # Search
        search_wrapper = QWidget()
        search_wrapper.setFixedHeight(36)
        self.search_field = QLineEdit(search_wrapper)
        self.search_field.setObjectName("SearchField")
        self.search_field.setPlaceholderText("Search notes…")
        self.search_field.setClearButtonEnabled(True)
        self.search_field.textChanged.connect(self.on_search_changed)
        self.search_icon = QLabel(search_wrapper)
        self.search_icon.setFixedSize(18, 18)

        def _place_search(event=None):
            self.search_field.setGeometry(0, 0, search_wrapper.width(), 36)
            self.search_icon.move(9, 9)
        search_wrapper.resizeEvent = _place_search  # type: ignore[assignment]
        _place_search()
        layout.addWidget(search_wrapper)

        # Views
        self.nav_buttons: dict[str, NavButton] = {}
        for key, icon_name, label in (
            ("all", "notes", "All notes"),
            ("favorites", "star", "Favorites"),
            ("pinned", "pin", "Pinned"),
            ("archive", "archive", "Archive"),
        ):
            button = NavButton(icon_name, label, self.theme)
            button.clicked.connect(lambda _=False, k=key: self.set_scope(k))
            self.nav_buttons[key] = button
            layout.addWidget(button)

        # Folders
        layout.addSpacing(6)
        layout.addLayout(self._section_header("FOLDERS", "New folder", self.create_folder_prompt,
                                              "folders"))
        self.folder_container = QVBoxLayout()
        self.folder_container.setContentsMargins(0, 0, 0, 0)
        self.folder_container.setSpacing(2)
        layout.addLayout(self.folder_container)

        # Tags
        layout.addSpacing(6)
        layout.addLayout(self._section_header("TAGS", "Manage tags", self.open_tag_manager, "tags"))
        self.tag_container = FlowLayout(spacing=5)
        layout.addLayout(self.tag_container)

        layout.addStretch()

        # Footer
        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.theme_button = tool_button("moon", "Toggle theme (Ctrl+Shift+D)",
                                        self.toggle_theme, size=30, icon_size=17,
                                        role="text_muted")
        footer.addWidget(self.theme_button)
        footer.addWidget(tool_button("command", "Command palette (Ctrl+K)",
                                     self.open_command_palette, size=30, icon_size=17,
                                     role="text_muted"))
        footer.addStretch()
        footer.addWidget(tool_button("gear", "Settings (Ctrl+,)", self.open_settings,
                                     size=30, icon_size=17, role="text_muted"))
        layout.addLayout(footer)

        return panel

    def _section_header(self, title: str, tooltip: str, callback: Callable,
                        key: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(4, 2, 0, 0)
        label = QLabel(title)
        label.setObjectName("SectionLabel")
        row.addWidget(label)
        row.addStretch()
        button = tool_button("plus", tooltip, callback, size=22, icon_size=13, role="text_faint")
        setattr(self, f"{key}_add_button", button)
        row.addWidget(button)
        return row

    # -- list pane ----------------------------------------------------------

    def _build_list_pane(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ListPane")
        panel.setMinimumWidth(0)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 10, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.list_title = QLabel("All notes")
        self.list_title.setObjectName("PaneTitle")
        header.addWidget(self.list_title)

        self.list_count = QLabel("0")
        self.list_count.setObjectName("Faint")
        header.addWidget(self.list_count)
        header.addStretch()

        self.sort_button = tool_button("sort", "Sort and density", self.open_sort_menu,
                                       size=28, icon_size=16, role="text_muted")
        header.addWidget(self.sort_button)
        self.collapse_list_button = tool_button("chevron_left", "Hide list (Ctrl+2)",
                                                self.toggle_list_pane, size=28, icon_size=16,
                                                role="text_muted")
        header.addWidget(self.collapse_list_button)
        layout.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        holder = QWidget()
        self.cards_layout = QVBoxLayout(holder)
        self.cards_layout.setContentsMargins(0, 0, 6, 0)
        self.cards_layout.setSpacing(7)
        self.cards_layout.addStretch()
        self.scroll.setWidget(holder)
        layout.addWidget(self.scroll)

        self.list_empty = QLabel("")
        self.list_empty.setObjectName("EmptyState")
        self.list_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_empty.setWordWrap(True)
        self.list_empty.hide()
        layout.addWidget(self.list_empty)

        return panel

    # -- editor pane --------------------------------------------------------

    def _build_editor_pane(self) -> QWidget:
        self.editor_stack = QStackedWidget()
        self.editor_stack.setObjectName("EditorPane")

        # Empty state ------------------------------------------------------
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(40, 40, 40, 40)
        empty_layout.addStretch()

        self.empty_icon = QLabel()
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_icon.setPixmap(render_app_icon(72))
        empty_layout.addWidget(self.empty_icon)
        empty_layout.addSpacing(16)

        headline = QLabel("Nothing open")
        headline.setObjectName("PaneTitle")
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(headline)

        subline = QLabel("Pick a note from the list, or press Ctrl+N to start a new one.")
        subline.setObjectName("Muted")
        subline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(subline)
        empty_layout.addSpacing(18)

        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(push_button("New note", "primary", "plus", self.create_new_note))
        button_row.addStretch()
        empty_layout.addLayout(button_row)
        empty_layout.addStretch()

        # Editor -----------------------------------------------------------
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(26, 18, 26, 16)
        layout.setSpacing(11)

        # Title row
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title_field = QLineEdit()
        self.title_field.setObjectName("TitleField")
        self.title_field.setPlaceholderText("Untitled note")
        self.title_field.textChanged.connect(self.on_title_changed)
        title_row.addWidget(self.title_field, 1)

        self.save_badge = QLabel("Saved")
        self.save_badge.setObjectName("SaveBadge")
        self.save_badge.setProperty("state", "idle")
        title_row.addWidget(self.save_badge)
        layout.addLayout(title_row)

        # Meta + view controls
        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("MetaLine")
        meta_row.addWidget(self.meta_label)
        meta_row.addStretch()

        self.sidebar_toggle = tool_button("sidebar", "Navigation (Ctrl+1)",
                                          self.toggle_sidebar, checkable=True,
                                          size=28, icon_size=16, role="text_muted")
        self.sidebar_toggle.setChecked(True)
        self.list_toggle = tool_button("notes", "Note list (Ctrl+2)",
                                       self.toggle_list_pane, checkable=True,
                                       size=28, icon_size=16, role="text_muted")
        self.list_toggle.setChecked(True)
        self.focus_toggle = tool_button("focus", "Focus mode (F11)",
                                        self.toggle_focus_mode, checkable=True,
                                        size=28, icon_size=16, role="text_muted")
        for widget in (self.sidebar_toggle, self.list_toggle, self.focus_toggle):
            meta_row.addWidget(widget)
        layout.addLayout(meta_row)

        # Tag strip
        self.tag_strip = QWidget()
        self.tag_strip_layout = QHBoxLayout(self.tag_strip)
        self.tag_strip_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_strip_layout.setSpacing(5)
        self.tag_strip_layout.addStretch()
        self.tag_strip.hide()
        layout.addWidget(self.tag_strip)

        layout.addWidget(self._build_format_toolbar())
        layout.addWidget(self._build_action_bar())

        # Editor
        self.editor = NoteEditor()
        self.editor.setAcceptRichText(True)
        self.editor.textChanged.connect(self.on_content_changed)
        self.editor.cursorPositionChanged.connect(self.sync_format_buttons)
        self.editor.files_dropped.connect(self.attach_files)
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.editor, 1)

        # Find & replace
        self.find_bar = FindReplaceBar(self.editor)
        self.find_bar.hide()
        layout.addWidget(self.find_bar)

        # Attachments / links
        self.attachment_panel = QFrame()
        self.attachment_panel.setObjectName("InlinePanel")
        self.attachment_layout = QVBoxLayout(self.attachment_panel)
        self.attachment_layout.setContentsMargins(12, 9, 12, 9)
        self.attachment_layout.setSpacing(7)
        self.attachment_panel.hide()
        layout.addWidget(self.attachment_panel)

        self.editor_stack.addWidget(empty)
        self.editor_stack.addWidget(page)
        self.editor_stack.setCurrentIndex(0)
        return self.editor_stack

    FONT_CHOICES = [
        "Segoe UI", "Calibri", "Arial", "Helvetica", "Times New Roman", "Georgia",
        "Verdana", "Tahoma", "Trebuchet MS", "Courier New", "Consolas", "Cambria",
        "Garamond", "Franklin Gothic Book", "Candara", "Constantia",
    ]
    SIZE_CHOICES = ["8", "9", "10", "11", "12", "13", "14", "16", "18", "20",
                    "24", "28", "32", "36", "48", "72"]

    def _build_format_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(46)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(9, 6, 9, 6)
        layout.setSpacing(5)

        available = set(QFontDatabase.families())
        families = [f for f in self.FONT_CHOICES if f in available]
        if not families:
            families = sorted(available)[:20]

        self.font_combo = QComboBox()
        self.font_combo.addItems(families)
        self.font_combo.setFixedWidth(150)
        self.font_combo.setFixedHeight(30)
        self.font_combo.setToolTip("Font family")
        if self.editor_font_family in families:
            self.font_combo.setCurrentText(self.editor_font_family)
        self.font_combo.currentTextChanged.connect(self.set_font_family)
        layout.addWidget(self.font_combo)

        self.size_combo = QComboBox()
        self.size_combo.addItems(self.SIZE_CHOICES)
        self.size_combo.setEditable(True)
        self.size_combo.setFixedWidth(64)
        self.size_combo.setFixedHeight(30)
        self.size_combo.setToolTip("Font size")
        self.size_combo.setCurrentText(str(self.editor_font_size))
        self.size_combo.currentTextChanged.connect(self.set_font_size)
        layout.addWidget(self.size_combo)

        layout.addWidget(divider(False))

        self.bold_button = tool_button("bold", "Bold (Ctrl+B)", self.toggle_bold, checkable=True)
        self.italic_button = tool_button("italic", "Italic (Ctrl+I)", self.toggle_italic,
                                         checkable=True)
        self.underline_button = tool_button("underline", "Underline (Ctrl+U)",
                                            self.toggle_underline, checkable=True)
        self.strike_button = tool_button("strike", "Strikethrough (Ctrl+Shift+X)",
                                         self.toggle_strike, checkable=True)
        for widget in (self.bold_button, self.italic_button,
                       self.underline_button, self.strike_button):
            layout.addWidget(widget)

        layout.addWidget(divider(False))

        layout.addWidget(tool_button("palette", "Text colour", self.pick_text_color))
        layout.addWidget(tool_button("highlighter", "Highlight", self.pick_highlight_color))
        layout.addWidget(tool_button("clear_format", "Clear formatting (Ctrl+Space)",
                                     self.clear_formatting))

        layout.addWidget(divider(False))

        self.block_button = tool_button("quote", "Paragraph style", self.open_block_menu)
        layout.addWidget(self.block_button)
        layout.addWidget(tool_button("list_bullet", "Bulleted list", self.insert_bullet_list))
        layout.addWidget(tool_button("list_number", "Numbered list", self.insert_numbered_list))
        layout.addWidget(tool_button("checklist", "Checklist", self.insert_checklist))

        layout.addWidget(divider(False))

        layout.addWidget(tool_button("table", "Insert or edit table", self.open_table_menu))
        layout.addWidget(tool_button("hr", "Horizontal rule", self.insert_rule))
        layout.addWidget(tool_button("code", "Code block", self.insert_code_block))

        layout.addStretch()
        return bar

    def _build_action_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(44)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(9, 5, 9, 5)
        layout.setSpacing(5)

        layout.addWidget(tool_button("paperclip", "Attach files", self.add_attachment))
        layout.addWidget(tool_button("link", "Add link", self.add_link))
        layout.addWidget(tool_button("tag", "Tags", self.open_note_tag_menu))
        layout.addWidget(tool_button("folder", "Move to folder", self.open_folder_menu))
        layout.addWidget(tool_button("palette", "Colour", self.open_color_menu))
        layout.addStretch()

        self.favorite_button = tool_button("star", "Favorite (Ctrl+Shift+S)",
                                           self.toggle_favorite, checkable=True)
        self.pin_button = tool_button("pin", "Pin (Ctrl+Shift+P)", self.toggle_pin,
                                      checkable=True)
        layout.addWidget(self.favorite_button)
        layout.addWidget(self.pin_button)
        layout.addWidget(divider(False))
        layout.addWidget(tool_button("mail", "Send via email (Ctrl+E)", self.send_note_as_email))
        layout.addWidget(tool_button("export", "Export", self.open_export_menu))
        layout.addWidget(tool_button("more", "More actions", self.open_more_menu))
        return bar

    # -- shortcuts ----------------------------------------------------------

    def _shortcut(self, sequence: str, callback: Callable) -> None:
        QShortcut(QKeySequence(sequence), self, activated=callback)

    def _build_shortcuts(self) -> None:
        pairs = [
            ("Ctrl+N", self.create_new_note),
            ("Ctrl+K", self.open_command_palette),
            ("Ctrl+F", lambda: self.search_field.setFocus() or self.search_field.selectAll()),
            ("Ctrl+Shift+F", lambda: self.find_bar.activate(False)),
            ("Ctrl+H", lambda: self.find_bar.activate(True)),
            ("Ctrl+S", self.save_current_note),
            ("Ctrl+B", self.toggle_bold),
            ("Ctrl+I", self.toggle_italic),
            ("Ctrl+U", self.toggle_underline),
            ("Ctrl+Shift+X", self.toggle_strike),
            ("Ctrl+Space", self.clear_formatting),
            ("Ctrl+D", self.duplicate_current_note),
            ("Ctrl+E", self.send_note_as_email),
            ("Ctrl+P", self.export_pdf),
            ("Ctrl+,", self.open_settings),
            ("Ctrl+1", self.toggle_sidebar),
            ("Ctrl+2", self.toggle_list_pane),
            ("F11", self.toggle_focus_mode),
            ("Ctrl+Shift+D", self.toggle_theme),
            ("Ctrl+Shift+P", self.toggle_pin),
            ("Ctrl+Shift+S", self.toggle_favorite),
            ("Ctrl+Shift+A", self.archive_current_note),
            ("Ctrl+Return", self.toggle_checkbox),
            ("Ctrl+Enter", self.toggle_checkbox),
            ("Ctrl+=", lambda: self.nudge_editor_zoom(1)),
            ("Ctrl++", lambda: self.nudge_editor_zoom(1)),
            ("Ctrl+-", lambda: self.nudge_editor_zoom(-1)),
        ]
        for sequence, callback in pairs:
            self._shortcut(sequence, callback)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self.find_bar.isVisible():
                self.find_bar.dismiss()
                return
            if self.focus_mode:
                self.toggle_focus_mode()
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------

    def apply_theme(self) -> None:
        """
        Rebuild the entire visual layer from tokens.

        Everything downstream - stylesheet, palette, icons, painted widgets and
        the open document - is regenerated from the same Theme object, so a
        switch can never leave part of the UI in the previous scheme.
        """
        self.theme = build_theme(self._resolved_mode(), self.accent_key)
        app = QApplication.instance()

        font = QFont(self.ui_font_family, self.ui_font_size)
        app.setFont(font)
        app.setPalette(build_palette(self.theme))
        app.setStyleSheet(build_stylesheet(self.theme, self.ui_font_family, self.ui_font_size))

        refresh_icons(self, self.theme)
        self.search_icon.setPixmap(Icons.pixmap("search", self.theme["text_faint"], 16))
        self.theme_button.setIcon(
            Icons.get("sun" if self.theme.is_dark else "moon", self.theme["text_muted"], 17))
        self.theme_button.setToolTip(
            "Switch to light theme (Ctrl+Shift+D)" if self.theme.is_dark
            else "Switch to dark theme (Ctrl+Shift+D)")

        for button in self.findChildren(NavButton):
            button.set_theme(self.theme)
        for card in self.cards.values():
            card.set_theme(self.theme)

        self.setWindowIcon(app_icon())
        self._apply_editor_surface()
        self.refresh_sidebar()
        self._update_status_stats()

    def _apply_editor_surface(self) -> None:
        """
        Point the editor at the right surface colour and re-guard the document.

        The per-note tint is the one colour that legitimately varies per widget,
        so it is applied here - still derived from tokens, still regenerated on
        every theme change, never hard-coded.
        """
        tint = self.current_note.editor_tint if self.current_note else "default"
        background = self.theme.note_tint(tint)
        text_color = ensure_contrast(QColor(self.theme["text"]), QColor(background), 4.5)
        selection = QColor(self.theme["selection"])

        self.editor.setStyleSheet(f"""
            QTextEdit {{
                background-color: {background};
                color: {text_color.name()};
                border: 1px solid {self.theme['border']};
                border-radius: 12px;
                padding: 18px 22px;
                selection-background-color: {selection.name()};
                selection-color: {readable_on(selection)};
            }}
            QTextEdit:focus {{ border-color: {self.theme['border_strong']}; }}
        """)

        palette = self.editor.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(background))
        palette.setColor(QPalette.ColorRole.Text, text_color)
        palette.setColor(QPalette.ColorRole.Highlight, selection)
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(readable_on(selection)))
        self.editor.setPalette(palette)

        document = self.editor.document()
        document.setDefaultFont(QFont(self.editor_font_family, self.editor_font_size))
        document.setDocumentMargin(4)

        was_loading = self._loading
        self._loading = True
        try:
            ContentPipeline.retheme_document(document, self.theme, background)
        finally:
            self._loading = was_loading

    def toggle_theme(self) -> None:
        self.set_theme_mode("light" if self._resolved_mode() == "dark" else "dark")

    def set_theme_mode(self, mode: str) -> None:
        self.theme_mode = mode
        self.settings.setValue("appearance/mode", mode)
        self.apply_theme()
        self.refresh_list(preserve_scroll=True)
        self.toast(f"{'Light' if self._resolved_mode() == 'light' else 'Dark'} theme applied")

    def set_accent(self, key: str) -> None:
        self.accent_key = key
        self.settings.setValue("appearance/accent", key)
        self.apply_theme()
        self.refresh_list(preserve_scroll=True)

    def _set_save_state(self, state: str, text: str) -> None:
        self.save_badge.setProperty("state", state)
        self.save_badge.setText(text)
        self.save_badge.style().unpolish(self.save_badge)
        self.save_badge.style().polish(self.save_badge)

    # ------------------------------------------------------------------
    # Sidebar / list refresh
    # ------------------------------------------------------------------

    def refresh_sidebar(self) -> None:
        counts = self.db.counts()
        for key, button in self.nav_buttons.items():
            button.set_count(counts.get(key, 0))
            button.set_active(self.scope == key and not self.search_query)

        # Folders
        clear_layout(self.folder_container)

        if counts.get("unfiled"):
            unfiled = NavButton("notes", "Unfiled", self.theme, counts["unfiled"], indent=2)
            unfiled.set_active(self.scope == "folder" and self.scope_folder_id is None)
            unfiled.clicked.connect(lambda: self.set_scope("folder", folder_id=None))
            self.folder_container.addWidget(unfiled)

        folders = self.db.get_all_folders()
        for row in folders:
            button = NavButton("folder", row["name"], self.theme, row["note_count"], indent=2)
            button.accent_dot = self.theme.note_rail(row["color"]) if row["color"] not in (
                "", "default", None) else ""
            button.set_active(self.scope == "folder" and self.scope_folder_id == row["id"])
            button.clicked.connect(lambda _=False, fid=row["id"]: self.set_scope("folder",
                                                                                 folder_id=fid))
            button.context_requested.connect(
                lambda pos, fid=row["id"], name=row["name"]: self.folder_context_menu(fid, name, pos))
            self.folder_container.addWidget(button)

        if not folders and not counts.get("unfiled"):
            hint = QLabel("  No folders yet")
            hint.setObjectName("Faint")
            self.folder_container.addWidget(hint)

        # Tags
        self.tag_container.set_max_width(max(160, self.sidebar.width() - 40))
        self.tag_container.clear()
        tags = self.db.get_all_tags()
        for row in tags:
            chip = TagChip(row["name"], row["color"] or "indigo", self.theme)
            chip.clicked.connect(lambda tid=row["id"]: self.set_scope("tag", tag_id=tid))
            self.tag_container.add(chip)
        if not tags:
            hint = QLabel("  No tags yet")
            hint.setObjectName("Faint")
            self.tag_container.add(hint)

    def _scope_title(self) -> str:
        if self.search_query:
            return f'Results for "{self.search_query}"'
        if self.scope == "folder":
            if self.scope_folder_id is None:
                return "Unfiled"
            row = next((f for f in self.db.get_all_folders()
                        if f["id"] == self.scope_folder_id), None)
            return row["name"] if row else "Folder"
        if self.scope == "tag":
            row = next((t for t in self.db.get_all_tags() if t["id"] == self.scope_tag_id), None)
            return f"#{row['name']}" if row else "Tag"
        return {"all": "All notes", "favorites": "Favorites",
                "pinned": "Pinned", "archive": "Archive"}.get(self.scope, "Notes")

    def current_notes(self) -> list[Note]:
        if self.search_query:
            return self.db.search_notes(self.search_query,
                                        include_archived=self.scope == "archive")
        return self.db.list_notes(scope=self.scope, folder_id=self.scope_folder_id,
                                  tag_id=self.scope_tag_id, sort=self.sort_mode)

    MAX_CARDS = 400

    def refresh_list(self, preserve_scroll: bool = False) -> None:
        offset = self.scroll.verticalScrollBar().value() if preserve_scroll else 0

        clear_layout(self.cards_layout, keep=1)
        self.cards.clear()

        notes = self.current_notes()
        self.list_title.setText(self._scope_title())
        self.list_count.setText(f"{len(notes):,}")

        if not notes:
            self.list_empty.setText(self._empty_message())
            self.list_empty.show()
            self.scroll.hide()
        else:
            self.list_empty.hide()
            self.scroll.show()

        for note in notes[:self.MAX_CARDS]:
            card = NoteCard(note, self.theme, self.density)
            card.clicked.connect(self.open_note)
            card.context_requested.connect(self.note_context_menu)
            if self.current_note and note.id == self.current_note.id:
                card.set_selected(True)
            self.cards[note.id] = card
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

        if len(notes) > self.MAX_CARDS:
            more = QLabel(f"Showing the first {self.MAX_CARDS:,} of {len(notes):,} notes — "
                          f"refine your search to narrow this down.")
            more.setObjectName("Faint")
            more.setWordWrap(True)
            more.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, more)

        if preserve_scroll:
            QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(offset))
        self._update_status_stats()

    def _empty_message(self) -> str:
        if self.search_query:
            return f'No notes match "{self.search_query}".'
        return {
            "archive": "The archive is empty.\nArchived notes land here before permanent deletion.",
            "favorites": "No favorites yet.\nStar a note to keep it close.",
            "pinned": "Nothing pinned.\nPin the notes you return to most.",
            "tag": "No notes carry this tag yet.",
            "folder": "This folder is empty.",
        }.get(self.scope, "No notes yet.\nPress Ctrl+N to write your first one.")

    def _update_status_stats(self) -> None:
        counts = self.db.counts()
        self.stat_label.setText(
            f"{counts['all']:,} notes · {counts['words']:,} words · {self.theme.name} theme")

    def set_scope(self, scope: str, folder_id: Optional[int] = None,
                  tag_id: Optional[int] = None) -> None:
        self.scope = scope
        self.scope_folder_id = folder_id
        self.scope_tag_id = tag_id
        if self.search_query:
            self.search_field.blockSignals(True)
            self.search_field.clear()
            self.search_field.blockSignals(False)
            self.search_query = ""
        self.refresh_sidebar()
        self.refresh_list()

    def on_search_changed(self, text: str) -> None:
        self.search_query = text.strip()
        self.refresh_sidebar()
        self.refresh_list()

    def open_sort_menu(self) -> None:
        menu = QMenu(self)
        labels = {"updated": "Last updated", "created": "Date created",
                  "title": "Title (A–Z)", "priority": "Priority"}
        for key, label in labels.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.sort_mode == key)
            action.triggered.connect(lambda _=False, k=key: self.set_sort(k))
        menu.addSeparator()
        for key, label in (("comfortable", "Comfortable rows"), ("compact", "Compact rows")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.density == key)
            action.triggered.connect(lambda _=False, k=key: self.set_density(k))
        menu.exec(QCursor.pos())

    def set_sort(self, mode: str) -> None:
        self.sort_mode = mode
        self.settings.setValue("list/sort", mode)
        self.refresh_list()

    def set_density(self, density: str) -> None:
        self.density = density
        self.settings.setValue("list/density", density)
        self.refresh_list()

    # ------------------------------------------------------------------
    # Note lifecycle
    # ------------------------------------------------------------------

    def create_new_note(self) -> None:
        self.save_current_note()
        folder_id = self.scope_folder_id if self.scope == "folder" else None
        note_id = self.db.create_note("", "", "", folder_id)
        if self.scope == "tag" and self.scope_tag_id:
            self.db.add_tag_to_note(note_id, self.scope_tag_id)
        if self.scope in ("archive", "favorites", "pinned"):
            self.scope = "all"
        self.refresh_sidebar()
        self.refresh_list()
        self.open_note(note_id)
        self.title_field.setFocus()
        self.toast("New note created")

    def open_note(self, note_id: int) -> None:
        if self.current_note and self.current_note.id == note_id and not self._dirty:
            return
        self.save_current_note()
        note = self.db.get_note(note_id)
        if note is None:
            self.toast("That note no longer exists")
            self.refresh_list()
            return

        self.current_note = note
        self._loading = True
        try:
            self.title_field.setText(note.title)

            document = self.editor.document()
            document.setUndoRedoEnabled(False)
            if note.content_html:
                self.editor.setHtml(note.content_html)
            else:
                self.editor.setPlainText(note.content)
            document.setUndoRedoEnabled(True)
            document.clearUndoRedoStacks()

            self.favorite_button.setChecked(bool(note.is_favorite))
            self.pin_button.setChecked(bool(note.is_pinned))
            self.favorite_button.setIcon(
                Icons.get("star_filled" if note.is_favorite else "star",
                          self.theme["on_accent"] if note.is_favorite else self.theme["text"], 18))
            self.pin_button.setIcon(
                Icons.get("pin_filled" if note.is_pinned else "pin",
                          self.theme["on_accent"] if note.is_pinned else self.theme["text"], 18))

            self._apply_editor_surface()
            self.refresh_tag_strip()
            self.refresh_attachments()
            self.update_meta_label()
            self.editor_stack.setCurrentIndex(1)
            self._set_save_state("idle", "Saved")
            self._dirty = False
        finally:
            self._loading = False

        for note_id_key, card in self.cards.items():
            card.set_selected(note_id_key == note_id)
        self.settings.setValue("session/last_note", note_id)
        self.sync_format_buttons()

    def _restore_last_note(self) -> None:
        last = self.settings.value("session/last_note")
        if last is None:
            return
        try:
            note_id = int(last)
        except (TypeError, ValueError):
            return
        if self.db.get_note(note_id):
            self.open_note(note_id)

    def on_title_changed(self, _text: str) -> None:
        if self._loading or not self.current_note:
            return
        self._mark_dirty()

    def on_content_changed(self) -> None:
        if self._loading or not self.current_note:
            return
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._set_save_state("dirty", "Editing…")
        self.autosave_timer.start(self.AUTOSAVE_MS)

    def save_current_note(self) -> None:
        if not self.current_note or not self._dirty:
            return
        note = self.current_note
        title = self.title_field.text()
        plain = self.editor.toPlainText()
        html = ContentPipeline.sanitize_for_storage(self.editor.toHtml())

        try:
            self.db.update_note(note.id, title=title, content=plain, content_html=html)
        except sqlite3.DatabaseError as error:
            # Never let a failed save loop: the autosave timer would fire again
            # immediately and every retry would raise the same error.
            self.autosave_timer.stop()
            self._dirty = False
            self._set_save_state("dirty", "Not saved")
            self._report_save_failure(error, note, plain)
            return
        self._dirty = False

        refreshed = self.db.get_note(note.id)
        if refreshed is not None:
            refreshed.tags = note.tags
            self.current_note = refreshed
            card = self.cards.get(note.id)
            if card is not None:
                card.note = refreshed
                card.update()
            self.update_meta_label()

        self._set_save_state("saved", "Saved")
        QTimer.singleShot(1600, lambda: self._set_save_state("idle", "Saved")
                          if not self._dirty else None)
        self._update_status_stats()

    def _report_save_failure(self, error: Exception, note: Note, plain: str) -> None:
        """Explain the failure once, keep the text safe, and offer a repair."""
        rescue = self.db.app_data_path / f"unsaved-{note.id}-{datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            rescue.write_text(f"{self.title_field.text()}\n\n{plain}", encoding="utf-8")
            rescued = f"\n\nYour text was written to:\n{rescue}"
        except OSError:
            rescued = ""

        box = QMessageBox(self)
        box.setWindowTitle("Could not save")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("This note could not be saved.")
        box.setInformativeText(f"{error}{rescued}")
        repair = box.addButton("Repair database", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is repair:
            self.repair_database()

    def repair_database(self) -> None:
        if not self.db.file_is_intact():
            self.info("Database file damaged",
                      "The file itself failed an integrity check. Restore your most recent "
                      f"backup, or copy this folder somewhere safe first:\n\n"
                      f"{self.db.app_data_path}")
            return
        if self.db.rebuild_fts():
            self.toast("Search index rebuilt — try saving again", 5000)
        else:
            self.db.disable_fts()
            self.toast("Search index removed; saving works again", 5000)
        self._dirty = True
        self.save_current_note()
        self.refresh_list(preserve_scroll=True)

    def update_meta_label(self) -> None:
        note = self.current_note
        if not note:
            self.meta_label.setText("")
            return
        minutes = max(1, round(note.word_count / 220)) if note.word_count else 0
        parts = [f"Created {friendly_date(note.created_date)}"]
        if note.edited_date:
            parts.append(f"edited {friendly_date(note.edited_date)}")
        parts.append(f"{note.word_count:,} words")
        if minutes:
            parts.append(f"~{minutes} min read")
        if note.priority and note.priority != "None":
            parts.append(f"{note.priority} priority")
        if note.folder_name:
            parts.append(note.folder_name)
        if note.is_archived:
            parts.append("archived")
        self.meta_label.setText("  ·  ".join(parts))

    def refresh_tag_strip(self) -> None:
        clear_layout(self.tag_strip_layout, keep=1)

        if not self.current_note:
            self.tag_strip.hide()
            return

        tags = self.db.get_note_tags(self.current_note.id)
        self.current_note.tags = tags
        if not tags:
            self.tag_strip.hide()
            return

        for tag_id, name, color in tags:
            chip = TagChip(name, color, self.theme, removable=True)
            chip.clicked.connect(lambda tid=tag_id: self.set_scope("tag", tag_id=tid))
            chip.removed.connect(lambda tid=tag_id: self.remove_tag_from_note(tid))
            self.tag_strip_layout.insertWidget(self.tag_strip_layout.count() - 1, chip)
        self.tag_strip.show()

    def refresh_attachments(self) -> None:
        clear_layout(self.attachment_layout)

        if not self.current_note:
            self.attachment_panel.hide()
            return

        attachments = self.db.get_note_attachments(self.current_note.id)
        links = self.db.get_note_links(self.current_note.id)
        if not attachments and not links:
            self.attachment_panel.hide()
            return

        if attachments:
            self.attachment_layout.addLayout(
                self._resource_row("paperclip", f"{len(attachments)} attachment"
                                   f"{'s' if len(attachments) != 1 else ''}",
                                   [(row["id"], f"{row['filename']}  ·  "
                                     f"{human_size(row['file_size'] or 0)}",
                                     row["filepath"]) for row in attachments],
                                   self.open_attachment, self.delete_attachment_ui))
        if links:
            self.attachment_layout.addLayout(
                self._resource_row("link", f"{len(links)} link{'s' if len(links) != 1 else ''}",
                                   [(row["id"], row["title"] or row["url"], row["url"])
                                    for row in links],
                                   self.open_link,
                                   self.delete_link_ui))
        self.attachment_panel.show()

    def _resource_row(self, icon_name: str, heading: str, items: list,
                      open_callback: Callable, delete_callback: Callable) -> QVBoxLayout:
        container = QVBoxLayout()
        container.setSpacing(5)

        header = QHBoxLayout()
        header.setSpacing(6)
        badge = QLabel()
        badge.setPixmap(Icons.pixmap(icon_name, self.theme["text_muted"], 14))
        header.addWidget(badge)
        label = QLabel(heading)
        label.setObjectName("Faint")
        header.addWidget(label)
        header.addStretch()
        container.addLayout(header)

        row = QHBoxLayout()
        row.setSpacing(6)
        for item_id, label_text, payload in items[:12]:
            button = push_button(label_text[:46], "ghost", "", None)
            button.setToolTip(label_text)
            button.clicked.connect(lambda _=False, p=payload: open_callback(p))
            row.addWidget(button)

            remove = tool_button("close", "Remove", None, size=22, icon_size=12,
                                 role="text_faint")
            remove.clicked.connect(lambda _=False, i=item_id: delete_callback(i))
            row.addWidget(remove)
        row.addStretch()
        container.addLayout(row)
        return container

    # -- note attribute mutations -------------------------------------------

    def _require_note(self) -> Optional[Note]:
        if self.current_note is None:
            self.toast("Open a note first")
            return None
        return self.current_note

    def toggle_favorite(self) -> None:
        note = self._require_note()
        if not note:
            return
        value = 0 if note.is_favorite else 1
        self.db.update_note(note.id, is_favorite=value, touch=False)
        note.is_favorite = value
        self.favorite_button.setChecked(bool(value))
        self.favorite_button.setIcon(
            Icons.get("star_filled" if value else "star",
                      self.theme["on_accent"] if value else self.theme["text"], 18))
        self._after_attribute_change(note)
        self.toast("Added to favorites" if value else "Removed from favorites")

    def toggle_pin(self) -> None:
        note = self._require_note()
        if not note:
            return
        value = 0 if note.is_pinned else 1
        self.db.update_note(note.id, is_pinned=value, touch=False)
        note.is_pinned = value
        self.pin_button.setChecked(bool(value))
        self.pin_button.setIcon(
            Icons.get("pin_filled" if value else "pin",
                      self.theme["on_accent"] if value else self.theme["text"], 18))
        self._after_attribute_change(note, resort=True)
        self.toast("Pinned to the top" if value else "Unpinned")

    def set_priority(self, priority: str) -> None:
        note = self._require_note()
        if not note:
            return
        self.db.update_note(note.id, priority=priority, touch=False)
        note.priority = priority
        self.update_meta_label()
        self._after_attribute_change(note, resort=self.sort_mode == "priority")
        self.toast(f"Priority set to {priority}")

    def set_color_token(self, token: str) -> None:
        note = self._require_note()
        if not note:
            return
        self.db.update_note(note.id, color_token=token, touch=False)
        note.color_token = token
        self._after_attribute_change(note)
        self.toast(f"Card colour: {NOTE_TINTS[token]['label']}")

    def set_editor_tint(self, token: str) -> None:
        note = self._require_note()
        if not note:
            return
        self.db.update_note(note.id, editor_tint=token, touch=False)
        note.editor_tint = token
        self._apply_editor_surface()
        self.toast(f"Page colour: {NOTE_TINTS[token]['label']}")

    def move_to_folder(self, folder_id: Optional[int]) -> None:
        note = self._require_note()
        if not note:
            return
        self.db.update_note(note.id, folder_id=folder_id, touch=False)
        refreshed = self.db.get_note(note.id)
        if refreshed:
            refreshed.tags = note.tags
            self.current_note = refreshed
        self.refresh_sidebar()
        self.refresh_list(preserve_scroll=True)
        self.update_meta_label()
        self.toast("Moved" if folder_id else "Moved out of folders")

    def _after_attribute_change(self, note: Note, resort: bool = False) -> None:
        card = self.cards.get(note.id)
        if card is not None and not resort:
            card.note = note
            card.update()
        else:
            self.refresh_list(preserve_scroll=True)
        self.refresh_sidebar()

    def duplicate_current_note(self) -> None:
        note = self._require_note()
        if not note:
            return
        self.save_current_note()
        new_id = self.db.duplicate_note(note.id)
        self.refresh_list()
        if new_id:
            self.open_note(new_id)
            self.toast("Note duplicated")

    def archive_current_note(self) -> None:
        note = self._require_note()
        if not note:
            return
        if note.is_archived:
            self.db.restore_note(note.id)
            self.toast("Restored from archive")
        else:
            self.save_current_note()
            self.db.archive_note(note.id)
            self.toast("Moved to archive")
        self.current_note = None
        self.editor_stack.setCurrentIndex(0)
        self.refresh_sidebar()
        self.refresh_list()

    def delete_permanently(self, note_id: Optional[int] = None) -> None:
        note_id = note_id or (self.current_note.id if self.current_note else None)
        if note_id is None:
            return
        note = self.db.get_note(note_id)
        if note is None:
            return
        if not self.confirm("Delete permanently?",
                            f"“{note.display_title}” and its attachments will be erased.\n"
                            "This cannot be undone.", destructive=True):
            return
        self.db.delete_note_permanent(note_id)
        if self.current_note and self.current_note.id == note_id:
            self.current_note = None
            self.editor_stack.setCurrentIndex(0)
        self.refresh_sidebar()
        self.refresh_list()
        self.toast("Note deleted permanently")

    # ------------------------------------------------------------------
    # Rich text formatting
    # ------------------------------------------------------------------

    def _merge_format(self, fmt: QTextCharFormat) -> None:
        """Apply to the selection, or arm the format for the next keystrokes."""
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        self.editor.mergeCurrentCharFormat(fmt)
        self.editor.setFocus()

    def sync_format_buttons(self) -> None:
        if self._loading:
            return
        fmt = self.editor.currentCharFormat()
        self.bold_button.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        self.italic_button.setChecked(fmt.fontItalic())
        self.underline_button.setChecked(fmt.fontUnderline())
        self.strike_button.setChecked(fmt.fontStrikeOut())

        families = fmt.fontFamilies()
        family = ""
        if isinstance(families, list) and families:
            family = str(families[0])
        elif isinstance(families, str):
            family = families
        family = family or fmt.font().family()

        for combo, value in ((self.font_combo, family),
                             (self.size_combo, str(int(fmt.fontPointSize()
                                                       or self.editor_font_size)))):
            combo.blockSignals(True)
            index = combo.findText(value)
            if index >= 0:
                combo.setCurrentIndex(index)
            elif combo.isEditable():
                combo.setCurrentText(value)
            combo.blockSignals(False)

    def toggle_bold(self) -> None:
        fmt = QTextCharFormat()
        currently_bold = self.editor.currentCharFormat().fontWeight() >= QFont.Weight.Bold
        fmt.setFontWeight(QFont.Weight.Normal if currently_bold else QFont.Weight.Bold)
        self._merge_format(fmt)
        self.bold_button.setChecked(not currently_bold)

    def toggle_italic(self) -> None:
        fmt = QTextCharFormat()
        value = not self.editor.currentCharFormat().fontItalic()
        fmt.setFontItalic(value)
        self._merge_format(fmt)
        self.italic_button.setChecked(value)

    def toggle_underline(self) -> None:
        fmt = QTextCharFormat()
        value = not self.editor.currentCharFormat().fontUnderline()
        fmt.setFontUnderline(value)
        self._merge_format(fmt)
        self.underline_button.setChecked(value)

    def toggle_strike(self) -> None:
        fmt = QTextCharFormat()
        value = not self.editor.currentCharFormat().fontStrikeOut()
        fmt.setFontStrikeOut(value)
        self._merge_format(fmt)
        self.strike_button.setChecked(value)

    def set_font_family(self, family: str) -> None:
        if not family:
            return
        fmt = QTextCharFormat()
        fmt.setFontFamilies([family])
        self._merge_format(fmt)
        self.editor_font_family = family
        self.settings.setValue("editor/font", family)

    def set_font_size(self, size: str) -> None:
        try:
            value = int(float(size))
        except (TypeError, ValueError):
            return
        if not 6 <= value <= 120:
            return
        fmt = QTextCharFormat()
        fmt.setFontPointSize(value)
        self._merge_format(fmt)
        self.editor_font_size = value
        self.settings.setValue("editor/size", value)

    def nudge_editor_zoom(self, delta: int) -> None:
        self.editor.zoomIn(delta) if delta > 0 else self.editor.zoomOut(-delta)
        self.toast("Zoomed in" if delta > 0 else "Zoomed out")

    def clear_formatting(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        plain = QTextCharFormat()
        plain.setFontFamilies([self.editor_font_family])
        plain.setFontPointSize(self.editor_font_size)
        cursor.setCharFormat(plain)

        block_format = QTextBlockFormat()
        block_format.setIndent(0)
        block_format.setLeftMargin(0)
        cursor.mergeBlockFormat(block_format)
        self.editor.setFocus()
        self.toast("Formatting cleared")

    # -- colour pickers ------------------------------------------------------

    def _swatch_icon(self, color: str) -> QIcon:
        pixmap = QPixmap(28, 28)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(self.theme["border_strong"]), 1))
        painter.setBrush(QBrush(QColor(color)))
        painter.drawRoundedRect(QRectF(3, 3, 22, 22), 5, 5)
        painter.end()
        return QIcon(pixmap)

    def _text_palette(self) -> list[tuple[str, str]]:
        page = QColor(self.theme.note_tint(
            self.current_note.editor_tint if self.current_note else "default"))
        entries = []
        for key, (dark, light) in ACCENT_CHOICES.items():
            base = QColor(dark if self.theme.is_dark else light)
            entries.append((key.capitalize(), ensure_contrast(base, page, 4.5).name()))
        entries.append(("Neutral", ensure_contrast(QColor(self.theme["text_muted"]),
                                                   page, 4.5).name()))
        return entries

    def pick_text_color(self) -> None:
        menu = QMenu(self)
        default_action = menu.addAction("Follow theme (recommended)")
        default_action.triggered.connect(self._reset_text_color)
        menu.addSeparator()
        for label, color in self._text_palette():
            action = menu.addAction(self._swatch_icon(color), label)
            action.triggered.connect(lambda _=False, c=color: self._set_text_color(c))
        menu.addSeparator()
        custom = menu.addAction("Custom…")
        custom.triggered.connect(self._custom_text_color)
        menu.exec(QCursor.pos())

    def _set_text_color(self, color: str) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QBrush(QColor(color)))
        self._merge_format(fmt)

    def _reset_text_color(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        if cursor.hasSelection():
            fmt = QTextCharFormat(cursor.charFormat())
            fmt.clearForeground()
            cursor.setCharFormat(fmt)
        self.editor.setFocus()
        self.toast("Text colour follows the theme again")

    def _custom_text_color(self) -> None:
        page = QColor(self.theme.note_tint(
            self.current_note.editor_tint if self.current_note else "default"))
        color = QColorDialog.getColor(QColor(self.theme["text"]), self, "Text colour")
        if not color.isValid():
            return
        adjusted = ensure_contrast(color, page, 4.5)
        if adjusted != color:
            self.toast("Colour lightened slightly to stay readable on this page")
        self._set_text_color(adjusted.name())

    def pick_highlight_color(self) -> None:
        menu = QMenu(self)
        none_action = menu.addAction("Remove highlight")
        none_action.triggered.connect(self._clear_highlight)
        menu.addSeparator()
        for token, entry in NOTE_TINTS.items():
            if token == "default":
                continue
            base = entry["rail_dark" if self.theme.is_dark else "rail_light"]
            surface = self.theme.note_tint(
                self.current_note.editor_tint if self.current_note else "default")
            color = mix(base, surface, 0.62 if self.theme.is_dark else 0.55)
            action = menu.addAction(self._swatch_icon(color), entry["label"])
            action.triggered.connect(lambda _=False, c=color: self._set_highlight(c))
        menu.exec(QCursor.pos())

    def _set_highlight(self, color: str) -> None:
        fmt = QTextCharFormat()
        fmt.setBackground(QBrush(QColor(color)))
        fmt.setForeground(QBrush(QColor(readable_on(color))))
        self._merge_format(fmt)

    def _clear_highlight(self) -> None:
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        if cursor.hasSelection():
            fmt = QTextCharFormat(cursor.charFormat())
            fmt.clearBackground()
            fmt.clearForeground()
            cursor.setCharFormat(fmt)
        self.editor.setFocus()

    # -- block styles --------------------------------------------------------

    def open_block_menu(self) -> None:
        menu = QMenu(self)
        for label, kind in (("Body text", "body"), ("Heading 1", "h1"),
                            ("Heading 2", "h2"), ("Heading 3", "h3"),
                            ("Quote", "quote"), ("Code block", "code")):
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, k=kind: self.apply_block_style(k))
        menu.exec(QCursor.pos())

    def apply_block_style(self, kind: str) -> None:
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()

        block_format = QTextBlockFormat()
        char_format = QTextCharFormat()
        base = self.editor_font_size

        block_format.setIndent(0)
        block_format.setLeftMargin(0)
        block_format.setTopMargin(4)
        block_format.setBottomMargin(4)
        block_format.setBackground(QBrush(Qt.GlobalColor.transparent))
        char_format.setFontFamilies([self.editor_font_family])
        char_format.setFontWeight(QFont.Weight.Normal)
        char_format.setFontItalic(False)
        char_format.setFontPointSize(base)

        if kind == "h1":
            char_format.setFontPointSize(base + 10)
            char_format.setFontWeight(QFont.Weight.Bold)
            block_format.setTopMargin(14)
        elif kind == "h2":
            char_format.setFontPointSize(base + 6)
            char_format.setFontWeight(QFont.Weight.Bold)
            block_format.setTopMargin(12)
        elif kind == "h3":
            char_format.setFontPointSize(base + 3)
            char_format.setFontWeight(QFont.Weight.DemiBold)
            block_format.setTopMargin(10)
        elif kind == "quote":
            block_format.setLeftMargin(24)
            block_format.setIndent(1)
            char_format.setFontItalic(True)
            char_format.setForeground(QBrush(QColor(self.theme["text_muted"])))
        elif kind == "code":
            mono = next((f for f in ("Consolas", "Cascadia Mono", "Courier New", "Monospace")
                         if f in QFontDatabase.families()), "Courier New")
            char_format.setFontFamilies([mono])
            char_format.setFontPointSize(base - 1)
            block_format.setLeftMargin(14)
            block_format.setBackground(QBrush(QColor(self.theme["code_bg"])))

        cursor.mergeBlockFormat(block_format)
        block_cursor = QTextCursor(cursor)
        block_cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        if block_cursor.hasSelection():
            block_cursor.mergeCharFormat(char_format)
        self.editor.mergeCurrentCharFormat(char_format)
        cursor.endEditBlock()
        self.editor.setFocus()

    def insert_bullet_list(self) -> None:
        self._insert_list(QTextListFormat.Style.ListDisc)

    def insert_numbered_list(self) -> None:
        self._insert_list(QTextListFormat.Style.ListDecimal)

    def _insert_list(self, style: QTextListFormat.Style) -> None:
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        list_format = QTextListFormat()
        list_format.setStyle(style)
        list_format.setIndent(1)
        cursor.createList(list_format)
        cursor.endEditBlock()
        self.editor.setFocus()

    def insert_checklist(self) -> None:
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        list_format = QTextListFormat()
        list_format.setStyle(QTextListFormat.Style.ListDisc)
        list_format.setIndent(1)
        cursor.createList(list_format)
        block_format = cursor.blockFormat()
        block_format.setMarker(QTextBlockFormat.MarkerType.Unchecked)
        cursor.setBlockFormat(block_format)
        cursor.endEditBlock()
        self.editor.setFocus()
        self.toast("Checklist added — press Ctrl+Enter to tick an item")

    def toggle_checkbox(self) -> None:
        cursor = self.editor.textCursor()
        block_format = cursor.blockFormat()
        marker = block_format.marker()
        if marker == QTextBlockFormat.MarkerType.NoMarker:
            self.insert_checklist()
            return
        block_format.setMarker(
            QTextBlockFormat.MarkerType.Checked
            if marker == QTextBlockFormat.MarkerType.Unchecked
            else QTextBlockFormat.MarkerType.Unchecked)
        cursor.setBlockFormat(block_format)
        self.editor.setFocus()

    def insert_rule(self) -> None:
        cursor = self.editor.textCursor()
        cursor.insertHtml("<hr />")
        self.editor.setFocus()

    def insert_code_block(self) -> None:
        self.apply_block_style("code")

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def _table_format(self) -> QTextTableFormat:
        fmt = QTextTableFormat()
        fmt.setBorder(1)
        fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        fmt.setBorderBrush(QBrush(QColor(self.theme["border_strong"])))
        fmt.setCellPadding(7)
        fmt.setCellSpacing(0)
        fmt.setAlignment(Qt.AlignmentFlag.AlignLeft)
        fmt.setBackground(QBrush(Qt.GlobalColor.transparent))
        return fmt

    def open_table_menu(self) -> None:
        cursor = self.editor.textCursor()
        table = cursor.currentTable()
        menu = QMenu(self)

        menu.addAction("Insert table…").triggered.connect(self.insert_table)
        convert = menu.addAction("Convert selection to table")
        convert.setEnabled(cursor.hasSelection())
        convert.triggered.connect(self.convert_selection_to_table)
        menu.addSeparator()

        for label, callback in (
            ("Insert row above", lambda: self._table_op("row_above")),
            ("Insert row below", lambda: self._table_op("row_below")),
            ("Insert column left", lambda: self._table_op("col_left")),
            ("Insert column right", lambda: self._table_op("col_right")),
        ):
            action = menu.addAction(label)
            action.setEnabled(table is not None)
            action.triggered.connect(callback)

        menu.addSeparator()
        for label, callback in (("Delete row", lambda: self._table_op("del_row")),
                                ("Delete column", lambda: self._table_op("del_col"))):
            action = menu.addAction(label)
            action.setEnabled(table is not None)
            action.triggered.connect(callback)

        menu.addSeparator()
        restyle = menu.addAction("Restyle borders to match theme")
        restyle.setEnabled(table is not None)
        restyle.triggered.connect(self.restyle_table)
        menu.exec(QCursor.pos())

    def insert_table(self) -> None:
        rows, ok = QInputDialog.getInt(self, "Insert table", "Rows:", 3, 1, 100)
        if not ok:
            return
        columns, ok = QInputDialog.getInt(self, "Insert table", "Columns:", 3, 1, 30)
        if not ok:
            return
        cursor = self.editor.textCursor()
        table = cursor.insertTable(rows, columns, self._table_format())
        self._style_header_row(table)
        self.editor.setFocus()
        self.toast(f"Inserted a {rows}×{columns} table")

    def _style_header_row(self, table) -> None:
        header = QTextCharFormat()
        header.setFontWeight(QFont.Weight.Bold)
        cell_bg = QColor(self.theme["surface_alt"])
        for column in range(table.columns()):
            cell = table.cellAt(0, column)
            if not cell.isValid():
                continue
            fmt = cell.format()
            fmt.setBackground(QBrush(cell_bg))
            cell.setFormat(fmt)
            cell.firstCursorPosition().setCharFormat(header)

    def _table_op(self, operation: str) -> None:
        cursor = self.editor.textCursor()
        table = cursor.currentTable()
        if table is None:
            return
        cell = table.cellAt(cursor)
        row, column = cell.row(), cell.column()
        actions = {
            "row_above": lambda: table.insertRows(row, 1),
            "row_below": lambda: table.insertRows(row + 1, 1),
            "col_left": lambda: table.insertColumns(column, 1),
            "col_right": lambda: table.insertColumns(column + 1, 1),
            "del_row": lambda: table.removeRows(row, 1),
            "del_col": lambda: table.removeColumns(column, 1),
        }
        action = actions.get(operation)
        if action:
            action()
            self.editor.setFocus()

    def restyle_table(self) -> None:
        cursor = self.editor.textCursor()
        table = cursor.currentTable()
        if table is None:
            self.toast("Place the cursor inside a table first")
            return
        table.setFormat(self._table_format())
        self._style_header_row(table)
        self.toast("Table restyled")

    def convert_selection_to_table(self) -> None:
        """Turn tab- or space-delimited text into a real table."""
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self.info("Nothing selected",
                      "Select the text first. Rows should be on separate lines, with "
                      "columns split by tabs or by two or more spaces.")
            return
        if cursor.currentTable():
            self.info("Already a table", "That selection is already inside a table.")
            return

        raw = cursor.selection().toPlainText().replace("\r\n", "\n").replace("\r", "\n")
        raw = raw.replace("\u2029", "\n")
        lines = [line for line in raw.split("\n") if line.strip()]
        if not lines:
            self.info("Nothing to convert", "The selection appears to be empty.")
            return

        sample = lines[0]
        if "\t" in sample:
            splitter = lambda line: line.split("\t")  # noqa: E731
        elif re.search(r"\s{2,}", sample):
            splitter = lambda line: re.split(r"\s{2,}", line)  # noqa: E731
        elif "|" in sample:
            splitter = lambda line: [p for p in line.split("|")]  # noqa: E731
        else:
            if not self.confirm("Split on single spaces?",
                                "No tabs or double spaces were found. Split each line on "
                                "single spaces instead?"):
                return
            splitter = lambda line: line.split()  # noqa: E731

        rows = []
        for line in lines:
            cells = [cell.strip() for cell in splitter(line)]
            cells = [cell for cell in cells if cell != ""] or [line.strip()]
            rows.append(cells)

        columns = max(len(row) for row in rows)
        for row in rows:
            row.extend([""] * (columns - len(row)))

        preview = "\n".join("  |  ".join(row) for row in rows[:3])
        if len(rows) > 3:
            preview += f"\n… and {len(rows) - 3} more rows"
        if not self.confirm(f"Convert to a {len(rows)}×{columns} table?", preview):
            return

        cursor.beginEditBlock()
        cursor.removeSelectedText()
        table = cursor.insertTable(len(rows), columns, self._table_format())
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                cell_cursor = table.cellAt(row_index, column_index).firstCursorPosition()
                cell_cursor.insertText(value)
        self._style_header_row(table)
        cursor.endEditBlock()

        end = self.editor.textCursor()
        end.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(end)
        self.editor.setFocus()
        self.toast(f"Converted to a {len(rows)}×{columns} table")

    # ------------------------------------------------------------------
    # Menus
    # ------------------------------------------------------------------

    def open_color_menu(self) -> None:
        if not self._require_note():
            return
        menu = QMenu(self)
        card_menu = menu.addMenu("Card colour")
        page_menu = menu.addMenu("Page colour")
        for token, entry in NOTE_TINTS.items():
            swatch = self.theme.note_tint(token)
            rail = self.theme.note_rail(token)
            icon = self._swatch_icon(rail if rail != "transparent" else swatch)

            card_action = card_menu.addAction(icon, entry["label"])
            card_action.setCheckable(True)
            card_action.setChecked(self.current_note.color_token == token)
            card_action.triggered.connect(lambda _=False, t=token: self.set_color_token(t))

            page_action = page_menu.addAction(icon, entry["label"])
            page_action.setCheckable(True)
            page_action.setChecked(self.current_note.editor_tint == token)
            page_action.triggered.connect(lambda _=False, t=token: self.set_editor_tint(t))
        menu.exec(QCursor.pos())

    def open_folder_menu(self) -> None:
        if not self._require_note():
            return
        menu = QMenu(self)
        none_action = menu.addAction("No folder")
        none_action.setCheckable(True)
        none_action.setChecked(self.current_note.folder_id is None)
        none_action.triggered.connect(lambda: self.move_to_folder(None))
        menu.addSeparator()
        for row in self.db.get_all_folders():
            action = menu.addAction(row["name"])
            action.setCheckable(True)
            action.setChecked(self.current_note.folder_id == row["id"])
            action.triggered.connect(lambda _=False, fid=row["id"]: self.move_to_folder(fid))
        menu.addSeparator()
        menu.addAction("New folder…").triggered.connect(self.create_folder_prompt)
        menu.exec(QCursor.pos())

    def open_note_tag_menu(self) -> None:
        note = self._require_note()
        if not note:
            return
        assigned = {tag_id for tag_id, _n, _c in self.db.get_note_tags(note.id)}
        menu = QMenu(self)
        menu.addAction("New tag…").triggered.connect(self.create_tag_prompt)
        tags = self.db.get_all_tags()
        if tags:
            menu.addSeparator()
        for row in tags:
            action = menu.addAction(row["name"])
            action.setCheckable(True)
            action.setChecked(row["id"] in assigned)
            action.triggered.connect(
                lambda checked, tid=row["id"]: self.set_note_tag(tid, checked))
        menu.addSeparator()
        menu.addAction("Manage tags…").triggered.connect(self.open_tag_manager)
        menu.exec(QCursor.pos())

    def open_export_menu(self) -> None:
        if not self._require_note():
            return
        menu = QMenu(self)
        menu.addAction("PDF document…").triggered.connect(self.export_pdf)
        menu.addAction("Web page (HTML)…").triggered.connect(lambda: self.export_text("html"))
        menu.addAction("Markdown…").triggered.connect(lambda: self.export_text("md"))
        menu.addAction("Plain text…").triggered.connect(lambda: self.export_text("txt"))
        menu.addSeparator()
        menu.addAction("Copy note as plain text").triggered.connect(self.copy_plain_text)
        menu.exec(QCursor.pos())

    def open_more_menu(self) -> None:
        note = self._require_note()
        if not note:
            return
        menu = QMenu(self)
        priority_menu = menu.addMenu("Priority")
        for level in ("None", "Low", "Medium", "High"):
            action = priority_menu.addAction(level)
            action.setCheckable(True)
            action.setChecked((note.priority or "None") == level)
            action.triggered.connect(lambda _=False, p=level: self.set_priority(p))

        menu.addSeparator()
        menu.addAction("Duplicate note (Ctrl+D)").triggered.connect(self.duplicate_current_note)
        menu.addAction("Find & replace (Ctrl+H)").triggered.connect(
            lambda: self.find_bar.activate(True))
        menu.addAction("Toggle checkbox (Ctrl+Enter)").triggered.connect(self.toggle_checkbox)
        menu.addSeparator()
        if note.is_archived:
            menu.addAction("Restore from archive").triggered.connect(self.archive_current_note)
        else:
            menu.addAction("Move to archive (Ctrl+Shift+A)").triggered.connect(
                self.archive_current_note)
        menu.addAction("Delete permanently…").triggered.connect(lambda: self.delete_permanently())
        menu.exec(QCursor.pos())

    def note_context_menu(self, note_id: int, position: QPoint) -> None:
        note = self.db.get_note(note_id)
        if note is None:
            return
        menu = QMenu(self)
        menu.addAction("Open").triggered.connect(lambda: self.open_note(note_id))
        menu.addSeparator()

        pin_action = menu.addAction("Unpin" if note.is_pinned else "Pin")
        pin_action.triggered.connect(lambda: self._quick_toggle(note_id, "is_pinned"))
        favorite_action = menu.addAction(
            "Remove from favorites" if note.is_favorite else "Add to favorites")
        favorite_action.triggered.connect(lambda: self._quick_toggle(note_id, "is_favorite"))

        menu.addSeparator()
        menu.addAction("Duplicate").triggered.connect(
            lambda: (self.db.duplicate_note(note_id), self.refresh_list()))
        if note.is_archived:
            menu.addAction("Restore").triggered.connect(
                lambda: (self.db.restore_note(note_id), self.refresh_sidebar(),
                         self.refresh_list()))
        else:
            menu.addAction("Move to archive").triggered.connect(
                lambda: (self.db.archive_note(note_id), self.refresh_sidebar(),
                         self.refresh_list()))
        menu.addAction("Delete permanently…").triggered.connect(
            lambda: self.delete_permanently(note_id))
        menu.exec(position)

    def _quick_toggle(self, note_id: int, field: str) -> None:
        note = self.db.get_note(note_id)
        if note is None:
            return
        value = 0 if getattr(note, field) else 1
        self.db.update_note(note_id, **{field: value}, touch=False)
        if self.current_note and self.current_note.id == note_id:
            setattr(self.current_note, field, value)
            self.favorite_button.setChecked(bool(self.current_note.is_favorite))
            self.pin_button.setChecked(bool(self.current_note.is_pinned))
        self.refresh_sidebar()
        self.refresh_list(preserve_scroll=True)

    def folder_context_menu(self, folder_id: int, name: str, position: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Rename…").triggered.connect(
            lambda: self.rename_folder_prompt(folder_id, name))
        color_menu = menu.addMenu("Colour")
        for token, entry in NOTE_TINTS.items():
            rail = self.theme.note_rail(token)
            icon = self._swatch_icon(rail if rail != "transparent" else self.theme["surface_alt"])
            action = color_menu.addAction(icon, entry["label"])
            action.triggered.connect(
                lambda _=False, t=token: (self.db.set_folder_color(folder_id, t),
                                          self.refresh_sidebar()))
        menu.addSeparator()
        menu.addAction("Delete folder…").triggered.connect(
            lambda: self.delete_folder_prompt(folder_id, name))
        menu.exec(position)

    # ------------------------------------------------------------------
    # Dialog helpers
    # ------------------------------------------------------------------

    def info(self, title: str, message: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(message)
        box.setIcon(QMessageBox.Icon.Information)
        box.exec()

    def confirm(self, title: str, message: str, destructive: bool = False) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(message)
        box.setIcon(QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question)
        yes = box.addButton("Delete" if destructive else "Continue",
                            QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        if destructive:
            yes.setProperty("variant", "danger")
        box.exec()
        return box.clickedButton() is yes

    @staticmethod
    def open_path(path: str | Path) -> None:
        path = str(path)
        try:
            if IS_WINDOWS:
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_link(self, url: str) -> None:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url
        QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------
    # Folders & tags
    # ------------------------------------------------------------------

    def create_folder_prompt(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        name = name.strip() if ok else ""
        if not name:
            return
        if self.db.create_folder(name) is None:
            self.info("Folder exists", f"A folder called “{name}” already exists.")
            return
        self.refresh_sidebar()
        self.toast(f"Folder “{name}” created")

    def rename_folder_prompt(self, folder_id: int, current: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename folder", "Folder name:", text=current)
        name = name.strip() if ok else ""
        if not name or name == current:
            return
        if not self.db.rename_folder(folder_id, name):
            self.info("Name taken", f"A folder called “{name}” already exists.")
            return
        self.refresh_sidebar()
        self.refresh_list(preserve_scroll=True)

    def delete_folder_prompt(self, folder_id: int, name: str) -> None:
        if not self.confirm(f"Delete “{name}”?",
                            "The folder will be removed. Notes inside it are kept and "
                            "become unfiled.", destructive=True):
            return
        self.db.delete_folder(folder_id)
        if self.scope == "folder" and self.scope_folder_id == folder_id:
            self.set_scope("all")
        else:
            self.refresh_sidebar()
            self.refresh_list()
        self.toast(f"Folder “{name}” deleted")

    def create_tag_prompt(self) -> None:
        name, ok = QInputDialog.getText(self, "New tag", "Tag name:")
        name = name.strip().lstrip("#") if ok else ""
        if not name:
            return
        tag_id = self.db.create_tag(name)
        if tag_id and self.current_note:
            self.db.add_tag_to_note(self.current_note.id, tag_id)
            self.refresh_tag_strip()
        self.refresh_sidebar()
        self.toast(f"Tag “{name}” added")

    def set_note_tag(self, tag_id: int, checked: bool) -> None:
        note = self._require_note()
        if not note:
            return
        if checked:
            self.db.add_tag_to_note(note.id, tag_id)
        else:
            self.db.remove_tag_from_note(note.id, tag_id)
        self.refresh_tag_strip()
        self.refresh_sidebar()

    def remove_tag_from_note(self, tag_id: int) -> None:
        self.set_note_tag(tag_id, False)

    def open_tag_manager(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Manage tags")
        dialog.setMinimumSize(460, 460)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        heading = QLabel("Tags")
        heading.setObjectName("PaneTitle")
        layout.addWidget(heading)

        listing = QListWidget()
        layout.addWidget(listing, 1)

        def reload() -> None:
            listing.clear()
            for row in self.db.get_all_tags():
                item = QListWidgetItem(f"{row['name']}   ·   {row['note_count']} notes")
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                item.setIcon(self._swatch_icon(
                    ACCENT_CHOICES.get(row["color"] or "indigo",
                                       ACCENT_CHOICES["indigo"])[0 if self.theme.is_dark else 1]))
                listing.addItem(item)

        def selected_id() -> Optional[int]:
            item = listing.currentItem()
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        def rename() -> None:
            tag_id = selected_id()
            if tag_id is None:
                return
            current = listing.currentItem().text().split("   ·")[0]
            name, ok = QInputDialog.getText(dialog, "Rename tag", "Tag name:", text=current)
            if ok and name.strip():
                try:
                    self.db.conn.execute("UPDATE tags SET name = ? WHERE id = ?",
                                         (name.strip(), tag_id))
                    self.db.conn.commit()
                except sqlite3.IntegrityError:
                    self.info("Name taken", "Another tag already uses that name.")
                reload()

        def recolor() -> None:
            tag_id = selected_id()
            if tag_id is None:
                return
            menu = QMenu(dialog)
            for key in ACCENT_CHOICES:
                pair = ACCENT_CHOICES[key]
                action = menu.addAction(
                    self._swatch_icon(pair[0] if self.theme.is_dark else pair[1]),
                    key.capitalize())
                action.triggered.connect(
                    lambda _=False, k=key: (self.db.set_tag_color(tag_id, k), reload()))
            menu.exec(QCursor.pos())

        def remove() -> None:
            tag_id = selected_id()
            if tag_id is None:
                return
            if self.confirm("Delete tag?", "The tag is removed from every note that uses it.",
                            destructive=True):
                self.db.delete_tag(tag_id)
                reload()

        buttons = QHBoxLayout()
        buttons.setSpacing(7)
        buttons.addWidget(push_button("New", "primary", "plus",
                                      lambda: (self.create_tag_prompt(), reload())))
        buttons.addWidget(push_button("Rename", "default", "", rename))
        buttons.addWidget(push_button("Colour", "default", "", recolor))
        buttons.addWidget(push_button("Delete", "danger", "", remove))
        buttons.addStretch()
        buttons.addWidget(push_button("Close", "default", "", dialog.accept))
        layout.addLayout(buttons)

        reload()
        dialog.exec()
        self.refresh_sidebar()
        self.refresh_tag_strip()

    # ------------------------------------------------------------------
    # Attachments & links
    # ------------------------------------------------------------------

    def add_attachment(self) -> None:
        if not self._require_note():
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        if paths:
            self.attach_files(paths)

    def attach_files(self, paths: list[str]) -> None:
        note = self._require_note()
        if not note:
            return
        added = 0
        for path in paths:
            if self.db.add_attachment(note.id, path):
                added += 1
        self.refresh_attachments()
        self.refresh_list(preserve_scroll=True)
        self.toast(f"Attached {added} file{'s' if added != 1 else ''}")

    def open_attachment(self, filepath: str) -> None:
        if not Path(filepath).exists():
            self.info("File missing", "That attachment is no longer on disk.")
            return
        self.open_path(filepath)

    def delete_attachment_ui(self, attachment_id: int) -> None:
        self.db.delete_attachment(attachment_id)
        self.refresh_attachments()
        self.refresh_list(preserve_scroll=True)
        self.toast("Attachment removed")

    def add_link(self) -> None:
        note = self._require_note()
        if not note:
            return
        url, ok = QInputDialog.getText(self, "Add link", "URL:")
        url = url.strip() if ok else ""
        if not url:
            return
        title, ok = QInputDialog.getText(self, "Add link", "Label (optional):")
        self.db.add_link(note.id, url, title.strip() if ok and title.strip() else None)
        self.refresh_attachments()
        self.toast("Link added")

    def delete_link_ui(self, link_id: int) -> None:
        self.db.delete_link(link_id)
        self.refresh_attachments()
        self.toast("Link removed")

    # ------------------------------------------------------------------
    # Sharing & export
    # ------------------------------------------------------------------

    def _portable_document(self) -> tuple[Note, str]:
        self.save_current_note()
        note = self.current_note
        body = ContentPipeline.to_portable_html(note.content_html, note.content)
        return note, body

    def _email_html(self, note: Note, body: str, links) -> str:
        header = (
            f'<div style="font:9pt Segoe UI,Arial,sans-serif;color:#54607A;'
            f'background:#F1F3F7;border-left:3px solid #3A5BD9;padding:8px 12px;'
            f'margin-bottom:14px;">'
            f'<strong>Created</strong> {html_mod.escape(note.created_date)}'
        )
        if note.edited_date:
            header += f' &nbsp;|&nbsp; <strong>Edited</strong> {html_mod.escape(note.edited_date)}'
        header += f' &nbsp;|&nbsp; <strong>Words</strong> {note.word_count:,}</div>'

        link_block = ""
        if links:
            items = "".join(
                f'<li><a href="{html_mod.escape(row["url"])}" style="color:#2F4BC0;">'
                f'{html_mod.escape(row["title"] or row["url"])}</a></li>' for row in links)
            link_block = (
                '<div style="margin-top:16px;background:#E9F0FD;border-left:3px solid #2F4BC0;'
                f'padding:10px 14px;"><strong>Links</strong><ul>{items}</ul></div>')

        return (
            '<html><body style="font:11pt Segoe UI,Calibri,Arial,sans-serif;color:#131924;'
            f'line-height:1.55;">{header}{body}{link_block}'
            '<hr style="border:none;border-top:1px solid #D8DEE8;margin-top:22px;">'
            '<p style="font:8.5pt Segoe UI,Arial,sans-serif;color:#78849B;text-align:center;">'
            f'Sent from {APP_NAME}</p></body></html>')

    def send_note_as_email(self) -> None:
        note = self._require_note()
        if not note:
            return
        note, body = self._portable_document()
        attachments = self.db.get_note_attachments(note.id)
        links = self.db.get_note_links(note.id)
        message = self._email_html(note, body, links)
        subject = note.display_title

        if IS_WINDOWS:
            try:
                import win32com.client  # noqa: PLC0415

                outlook = win32com.client.Dispatch("Outlook.Application")
                mail = outlook.CreateItem(0)
                mail.Subject = subject
                mail.HTMLBody = message
                failed = []
                for row in attachments:
                    if Path(row["filepath"]).exists():
                        try:
                            mail.Attachments.Add(row["filepath"])
                        except Exception:
                            failed.append(row["filename"])
                mail.Display()
                note_text = f"Outlook draft ready with {len(attachments) - len(failed)} attachment(s)."
                if failed:
                    note_text += f" Could not attach: {', '.join(failed)}."
                self.toast(note_text, 6000)
                return
            except ImportError:
                pass
            except Exception as error:
                self.toast(f"Outlook unavailable ({error}); opening in your browser instead", 5000)

        self._email_via_browser(note, message, attachments)

    def _email_via_browser(self, note: Note, message: str, attachments) -> None:
        if attachments:
            rows = "".join(
                f'<li>{html_mod.escape(row["filename"])} '
                f'({human_size(row["file_size"] or 0)})</li>' for row in attachments)
            message = message.replace(
                "</body>",
                '<div style="margin-top:18px;background:#FDF3E2;border-left:4px solid #9A6206;'
                f'padding:12px 14px;"><strong>Attach these {len(attachments)} file(s) manually'
                f'</strong><ul>{rows}</ul>'
                f'<p style="font-size:9pt;">Folder: <code>{self.db.attachments_path}</code></p>'
                '</div></body>')

        target = self.db.app_data_path / f"email_{note.id}.html"
        target.write_text(message, encoding="utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        if attachments:
            self.open_path(self.db.attachments_path)
        self.info("Opened in your browser",
                  "Select all (Ctrl+A), copy (Ctrl+C) and paste into your email client.")

    def export_pdf(self) -> None:
        note = self._require_note()
        if not note:
            return
        self.save_current_note()
        default = self._safe_filename(note.display_title) + ".pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Export as PDF", default, "PDF (*.pdf)")
        if not path:
            return

        document = QTextDocument()
        document.setHtml(ContentPipeline.to_portable_html(note.content_html, note.content))
        document.setDefaultFont(QFont(self.editor_font_family, self.editor_font_size))

        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setResolution(300)
        writer.setTitle(note.display_title)

        printer = getattr(document, "print", None) or getattr(document, "print_")
        printer(writer)
        self.toast(f"Saved {Path(path).name}")

    def export_text(self, kind: str) -> None:
        note = self._require_note()
        if not note:
            return
        self.save_current_note()
        extensions = {"html": ("HTML (*.html)", ".html"),
                      "md": ("Markdown (*.md)", ".md"),
                      "txt": ("Text (*.txt)", ".txt")}
        filter_text, suffix = extensions[kind]
        default = self._safe_filename(note.display_title) + suffix
        path, _ = QFileDialog.getSaveFileName(self, "Export note", default, filter_text)
        if not path:
            return

        if kind == "html":
            body = ContentPipeline.to_portable_html(note.content_html, note.content)
            payload = (f"<!doctype html><html><head><meta charset='utf-8'>"
                       f"<title>{html_mod.escape(note.display_title)}</title></head>"
                       f"<body style=\"font:11pt Segoe UI,Arial,sans-serif;max-width:820px;"
                       f"margin:40px auto;line-height:1.6;color:#131924;\">"
                       f"<h1>{html_mod.escape(note.display_title)}</h1>{body}</body></html>")
        elif kind == "md":
            payload = f"# {note.display_title}\n\n{note.content}\n"
        else:
            payload = f"{note.display_title}\n{'=' * len(note.display_title)}\n\n{note.content}\n"

        Path(path).write_text(payload, encoding="utf-8")
        self.toast(f"Saved {Path(path).name}")

    def copy_plain_text(self) -> None:
        note = self._require_note()
        if not note:
            return
        QApplication.clipboard().setText(
            f"{note.display_title}\n\n{self.editor.toPlainText()}")
        self.toast("Copied to the clipboard")

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]+', "-", name).strip(" .")
        return (cleaned or "note")[:80]

    # ------------------------------------------------------------------
    # Layout, focus mode and window state
    # ------------------------------------------------------------------

    def _sizes(self) -> list[int]:
        return self.splitter.sizes()

    def toggle_sidebar(self) -> None:
        sizes = self._sizes()
        total = sum(sizes)
        if sizes[0] < 40:
            width = 270
            self.splitter.setSizes([width, sizes[1], max(320, total - width - sizes[1])])
        else:
            self.splitter.setSizes([0, sizes[1], total - sizes[1]])
        self._sync_view_toggles()

    def toggle_list_pane(self) -> None:
        sizes = self._sizes()
        total = sum(sizes)
        if sizes[1] < 40:
            width = 340
            self.splitter.setSizes([sizes[0], width, max(320, total - sizes[0] - width)])
        else:
            self.splitter.setSizes([sizes[0], 0, total - sizes[0]])
        self._sync_view_toggles()

    def _sync_view_toggles(self) -> None:
        sizes = self._sizes()
        for button, visible in ((self.sidebar_toggle, sizes[0] >= 40),
                                (self.list_toggle, sizes[1] >= 40)):
            button.blockSignals(True)
            button.setChecked(visible)
            button.blockSignals(False)
        self.collapse_list_button.setIcon(
            Icons.get("chevron_left" if sizes[1] >= 40 else "chevron_right",
                      self.theme["text_muted"], 16))
        if self.focus_mode and (sizes[0] >= 40 or sizes[1] >= 40):
            self.focus_mode = False
            self.focus_toggle.blockSignals(True)
            self.focus_toggle.setChecked(False)
            self.focus_toggle.blockSignals(False)

    def toggle_focus_mode(self) -> None:
        if not self.focus_mode:
            self._last_sizes = self._sizes()
            total = sum(self._last_sizes)
            self.focus_mode = True
            self.splitter.setSizes([0, 0, total])
            self.toast("Focus mode — press Esc or F11 to come back", 4000)
        else:
            self.focus_mode = False
            restored = self._last_sizes if sum(self._last_sizes) > 0 else [270, 340, 780]
            self.splitter.setSizes(restored)
            self.toast("Focus mode off")
        self.focus_toggle.blockSignals(True)
        self.focus_toggle.setChecked(self.focus_mode)
        self.focus_toggle.blockSignals(False)
        self._sync_view_toggles()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:
            return
        if self.current_note is None:
            self.create_new_note()
        self.attach_files(paths)
        event.acceptProposedAction()

    def _restore_geometry(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        sizes = self.settings.value("window/splitter")
        if sizes:
            try:
                parsed = [int(value) for value in sizes]
                if len(parsed) == 3 and sum(parsed) > 200:
                    self.splitter.setSizes(parsed)
                    self._last_sizes = parsed
            except (TypeError, ValueError):
                pass
        self._sync_view_toggles()

    def closeEvent(self, event) -> None:
        self.save_current_note()
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter", self._sizes())
        self.settings.sync()
        self.db.close()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Command palette
    # ------------------------------------------------------------------

    def open_command_palette(self) -> None:
        commands: list[tuple[str, str, Callable]] = [
            ("New note", "Ctrl+N", self.create_new_note),
            ("Toggle theme", "Ctrl+Shift+D", self.toggle_theme),
            ("Settings", "Ctrl+,", self.open_settings),
            ("Focus mode", "F11", self.toggle_focus_mode),
            ("Find & replace", "Ctrl+H", lambda: self.find_bar.activate(True)),
            ("Insert table", "", self.insert_table),
            ("Convert selection to table", "", self.convert_selection_to_table),
            ("Export as PDF", "Ctrl+P", self.export_pdf),
            ("Export as HTML", "", lambda: self.export_text("html")),
            ("Export as Markdown", "", lambda: self.export_text("md")),
            ("Send via email", "Ctrl+E", self.send_note_as_email),
            ("Duplicate note", "Ctrl+D", self.duplicate_current_note),
            ("Pin or unpin note", "Ctrl+Shift+P", self.toggle_pin),
            ("Favorite or unfavorite note", "Ctrl+Shift+S", self.toggle_favorite),
            ("Move to archive", "Ctrl+Shift+A", self.archive_current_note),
            ("New folder", "", self.create_folder_prompt),
            ("New tag", "", self.create_tag_prompt),
            ("Manage tags", "", self.open_tag_manager),
            ("All notes", "", lambda: self.set_scope("all")),
            ("Favorites", "", lambda: self.set_scope("favorites")),
            ("Pinned", "", lambda: self.set_scope("pinned")),
            ("Archive", "", lambda: self.set_scope("archive")),
            ("Back up everything", "", self.backup_data),
            ("Open data folder", "", lambda: self.open_path(self.db.app_data_path)),
        ]
        palette = CommandPalette(self, commands, self.db.list_notes(sort="updated"))
        geometry = self.geometry()
        palette.move(geometry.center().x() - palette.width() // 2, geometry.top() + 90)
        palette.show()
        palette.query.setFocus()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _settings_card(self, title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("InlinePanel")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)

        heading = QLabel(title)
        heading.setObjectName("PaneTitle")
        layout.addWidget(heading)
        if subtitle:
            note = QLabel(subtitle)
            note.setObjectName("Muted")
            note.setWordWrap(True)
            layout.addWidget(note)
        return card, layout

    def open_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Settings")
        dialog.setMinimumSize(600, 680)

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        title = QLabel("Settings")
        title.setObjectName("PaneTitle")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)

        # --- Appearance ---------------------------------------------------
        card, card_layout = self._settings_card(
            "Appearance",
            "Colours are generated from one token set, so both themes stay above the "
            "WCAG AA contrast threshold everywhere — including note text and highlights.")

        mode_row = QHBoxLayout()
        mode_row.setSpacing(7)
        mode_buttons: dict[str, QPushButton] = {}

        def select_mode(mode: str) -> None:
            self.set_theme_mode(mode)
            for key, button in mode_buttons.items():
                button.setProperty("variant", "primary" if key == mode else "default")
                button.style().unpolish(button)
                button.style().polish(button)

        for key, label, icon_name in (("dark", "Dark", "moon"), ("light", "Light", "sun"),
                                      ("system", "Match system", "gear")):
            button = push_button(label, "primary" if self.theme_mode == key else "default",
                                 icon_name, lambda _=False, k=key: select_mode(k))
            button.setFixedHeight(36)
            mode_buttons[key] = button
            mode_row.addWidget(button)
        mode_row.addStretch()
        card_layout.addLayout(mode_row)

        accent_row = QHBoxLayout()
        accent_row.setSpacing(7)
        accent_label = QLabel("Accent")
        accent_label.setObjectName("Muted")
        accent_row.addWidget(accent_label)
        for key in ACCENT_CHOICES:
            pair = ACCENT_CHOICES[key]
            swatch = QToolButton()
            swatch.setFixedSize(28, 28)
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)
            swatch.setToolTip(key.capitalize())
            swatch.setIcon(self._swatch_icon(pair[0] if self.theme.is_dark else pair[1]))
            swatch.setIconSize(QSize(24, 24))
            swatch.clicked.connect(lambda _=False, k=key: self.set_accent(k))
            accent_row.addWidget(swatch)
        accent_row.addStretch()
        card_layout.addLayout(accent_row)

        available = sorted(QFontDatabase.families())

        def font_row(label_text: str, current_family: str, current_size: int,
                     on_family: Callable, on_size: Callable) -> QHBoxLayout:
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(label_text)
            label.setObjectName("Muted")
            label.setFixedWidth(110)
            row.addWidget(label)

            combo = QComboBox()
            combo.addItems(available)
            if current_family in available:
                combo.setCurrentText(current_family)
            combo.setMinimumWidth(210)
            combo.currentTextChanged.connect(on_family)
            row.addWidget(combo)

            spin = QSpinBox()
            spin.setRange(7, 28)
            spin.setValue(current_size)
            spin.setSuffix(" pt")
            spin.valueChanged.connect(on_size)
            row.addWidget(spin)
            row.addStretch()
            return row

        def set_ui_font(family: str) -> None:
            self.ui_font_family = family
            self.settings.setValue("appearance/font", family)
            self.apply_theme()

        def set_ui_size(size: int) -> None:
            self.ui_font_size = size
            self.settings.setValue("appearance/size", size)
            self.apply_theme()

        def set_editor_font(family: str) -> None:
            self.editor_font_family = family
            self.settings.setValue("editor/font", family)
            self._apply_editor_surface()

        def set_editor_size(size: int) -> None:
            self.editor_font_size = size
            self.settings.setValue("editor/size", size)
            self._apply_editor_surface()

        card_layout.addLayout(font_row("Interface font", self.ui_font_family,
                                       self.ui_font_size, set_ui_font, set_ui_size))
        card_layout.addLayout(font_row("Editor font", self.editor_font_family,
                                       self.editor_font_size, set_editor_font, set_editor_size))
        layout.addWidget(card)

        # --- Note list ------------------------------------------------------
        card, card_layout = self._settings_card("Note list")
        row = QHBoxLayout()
        row.setSpacing(7)
        for key, label in (("comfortable", "Comfortable rows"), ("compact", "Compact rows")):
            button = push_button(label, "primary" if self.density == key else "default", "",
                                 lambda _=False, k=key: self.set_density(k))
            row.addWidget(button)
        row.addStretch()
        card_layout.addLayout(row)
        layout.addWidget(card)

        # --- Data -----------------------------------------------------------
        counts = self.db.counts()
        card, card_layout = self._settings_card(
            "Data",
            f"{counts['all']:,} active notes · {counts['archive']:,} archived · "
            f"{counts['words']:,} words. Everything is stored locally.")

        path_label = QLabel(str(self.db.db_path))
        path_label.setObjectName("Faint")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card_layout.addWidget(path_label)

        data_row = QHBoxLayout()
        data_row.setSpacing(7)
        data_row.addWidget(push_button("Open folder", "default", "folder",
                                       lambda: self.open_path(self.db.app_data_path)))
        data_row.addWidget(push_button("Back up…", "primary", "export", self.backup_data))
        data_row.addWidget(push_button("Clean orphaned files", "default", "", self.clean_orphans))
        data_row.addWidget(push_button("Repair database", "default", "", self.repair_database))
        data_row.addStretch()
        card_layout.addLayout(data_row)

        if counts["archive"]:
            empty_row = QHBoxLayout()
            empty_row.addWidget(push_button(
                f"Empty archive ({counts['archive']:,})", "danger", "trash",
                lambda: self.empty_archive(dialog)))
            empty_row.addStretch()
            card_layout.addLayout(empty_row)
        layout.addWidget(card)

        # --- Shortcuts -------------------------------------------------------
        card, card_layout = self._settings_card("Keyboard")
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        shortcuts = [
            ("Ctrl+N", "New note"), ("Ctrl+K", "Command palette"),
            ("Ctrl+F", "Search notes"), ("Ctrl+Shift+F", "Find in note"),
            ("Ctrl+H", "Find & replace"), ("Ctrl+S", "Save now"),
            ("Ctrl+B / I / U", "Bold, italic, underline"), ("Ctrl+Shift+X", "Strikethrough"),
            ("Ctrl+Space", "Clear formatting"), ("Ctrl+Enter", "Toggle checkbox"),
            ("Ctrl+D", "Duplicate note"), ("Ctrl+E", "Email note"),
            ("Ctrl+P", "Export as PDF"), ("Ctrl+1 / Ctrl+2", "Show or hide panels"),
            ("F11", "Focus mode"), ("Ctrl+Shift+D", "Toggle theme"),
        ]
        for index, (keys, description) in enumerate(shortcuts):
            key_label = QLabel(keys)
            key_label.setObjectName("Faint")
            description_label = QLabel(description)
            description_label.setObjectName("Muted")
            grid.addWidget(key_label, index // 2, (index % 2) * 2)
            grid.addWidget(description_label, index // 2, (index % 2) * 2 + 1)
        card_layout.addLayout(grid)
        layout.addWidget(card)

        # --- About ------------------------------------------------------------
        card, card_layout = self._settings_card(
            f"About {APP_NAME} {APP_VERSION}",
            "A single-file, offline-first notes application. No assets, no installer, "
            "no network calls — your notes never leave this machine.")
        layout.addWidget(card)

        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(push_button("Done", "primary", "", dialog.accept))
        outer.addLayout(footer)

        dialog.exec()

    def backup_data(self) -> None:
        default = f"{APP_NAME}-backup-{datetime.now():%Y%m%d-%H%M}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Back up notes", default, "Zip archive (*.zip)")
        if not path:
            return
        self.save_current_note()
        try:
            self.db.backup_to(path)
            self.toast(f"Backed up to {Path(path).name}")
        except Exception as error:
            self.info("Backup failed", str(error))

    def clean_orphans(self) -> None:
        removed = self.db.cleanup_orphaned_attachments()
        self.toast(f"Removed {removed} orphaned file{'s' if removed != 1 else ''}")

    def empty_archive(self, parent_dialog: Optional[QDialog] = None) -> None:
        if not self.confirm("Empty the archive?",
                            "Every archived note and its attachments will be erased permanently.",
                            destructive=True):
            return
        removed = self.db.empty_archive()
        if parent_dialog is not None:
            parent_dialog.accept()
        if self.current_note and self.current_note.is_archived:
            self.current_note = None
            self.editor_stack.setCurrentIndex(0)
        self.refresh_sidebar()
        self.refresh_list()
        self.toast(f"Deleted {removed} archived note{'s' if removed != 1 else ''}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def set_windows_app_id() -> None:
    """Give Windows a stable identity so the taskbar shows our icon and groups correctly."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def install_excepthook() -> None:
    def handler(kind, value, traceback_object) -> None:
        import traceback

        details = "".join(traceback.format_exception(kind, value, traceback_object))
        sys.stderr.write(details)
        try:
            log = Path(os.getenv("APPDATA") or Path.home()) / APP_NAME / "error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} ---\n{details}")
        except Exception:
            pass
        if QApplication.instance() is not None:
            box = QMessageBox()
            box.setWindowTitle(f"{APP_NAME} hit a problem")
            box.setText("Something went wrong, but your notes are safe on disk.")
            box.setDetailedText(details)
            box.setIcon(QMessageBox.Icon.Warning)
            box.exec()

    sys.excepthook = handler


def main() -> int:
    if "--export-icon" in sys.argv:
        # Needs a QGuiApplication for QPixmap; runs headless and exits.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(sys.argv)
        target = Path(sys.argv[sys.argv.index("--export-icon") + 1]) \
            if len(sys.argv) > sys.argv.index("--export-icon") + 1 \
            and not sys.argv[sys.argv.index("--export-icon") + 1].startswith("-") \
            else Path(f"{APP_NAME}.ico")
        write_ico(target)
        for size in (256, 512):
            render_app_icon(size).save(str(target.with_name(f"{target.stem}-{size}.png")), "PNG")
        # A windowed (console-less) frozen build has no stdout, so printing can
        # raise; and destroying QApplication during interpreter shutdown can abort
        # with a non-zero exit code even though the work succeeded. Exit hard.
        try:
            if sys.stdout is not None:
                print(f"Wrote {target} and PNG previews next to it.")
                sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)

    set_windows_app_id()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(APP_VERSION)

    if "Fusion" in QStyleFactory.keys():
        app.setStyle(QStyleFactory.create("Fusion"))

    app.setWindowIcon(app_icon())
    install_excepthook()

    database = NotesDatabase()
    window = NoteCraftWindow(database)
    window.setWindowIcon(app_icon())
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
