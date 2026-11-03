from __future__ import annotations

import re
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ..localization import translate as _
from ..text import pattern_lora, str_index_to_char16_index
from ..util import trim_text
from . import theme

_min_strength, _max_strength = -2.0, 3.0
_slider_scale = 100  # slider works in hundredths


def format_strength(value: float):
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def parse_strength(text: str | None) -> float | None:
    """Strength written in a <lora:name:strength> tag. 1.0 when it is left out, None
    when it is something other than a number (a wildcard like [[0.6|1.0]])."""
    if not text:
        return 1.0
    try:
        return float(text)
    except ValueError:
        return None


def nth_lora(text: str, index: int) -> re.Match[str] | None:
    return next((m for i, m in enumerate(pattern_lora.finditer(text)) if i == index), None)


def with_strength(match: re.Match[str], value: float):
    return f"<lora:{match[1]}:{format_strength(value)}>"


class LoraStrengthPopup(QMenu):
    """Lists every <lora:...> tag of a prompt with a slider for its strength. Edits go
    into the prompt text through the cursor, so Ctrl+Z undoes them one by one."""

    def __init__(self, prompt: QPlainTextEdit, parent: QWidget | None = None):
        super().__init__(parent)
        self._prompt = prompt
        # QWidgetAction ignores a new default widget while the menu shows it, so the
        # rows get swapped inside a container that stays
        self._container = QWidget(self)
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._content: QWidget | None = None
        self._last_edited: int | None = None  # dragging one slider is one undo step
        action = QWidgetAction(self)
        action.setDefaultWidget(self._container)
        self.addAction(action)
        self.aboutToShow.connect(self._rebuild)

    def _rebuild(self):
        self._last_edited = None
        if self._content is not None:
            self._container_layout.removeWidget(self._content)
            self._content.hide()
            self._content.deleteLater()  # may be running the remove button's click
        content = QWidget(self._container)
        layout = QGridLayout(content)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setColumnMinimumWidth(1, 160)
        matches = list(pattern_lora.finditer(self._prompt.toPlainText()))
        if not matches:
            empty = QLabel(_("No LoRAs in the prompt"), content)
            empty.setStyleSheet(f"font-style: italic; color: {theme.grey};")
            layout.addWidget(empty, 0, 0)
        for row, match in enumerate(matches):
            self._add_row(layout, row, match)
        self._container_layout.addWidget(content)
        self._content = content

    def _add_row(self, layout: QGridLayout, row: int, match: re.Match[str]):
        parent = layout.parentWidget()
        name = QLabel(trim_text(Path(match[1].replace("\\", "/")).stem, 40), parent)
        name.setToolTip(match[0])
        layout.addWidget(name, row, 0)

        remove = QToolButton(parent)
        remove.setIcon(theme.icon("remove"))
        remove.setAutoRaise(True)
        remove.setToolTip(_("Remove this LoRA from the prompt"))
        remove.clicked.connect(lambda: self._remove(row))
        layout.addWidget(remove, row, 3)

        strength = parse_strength(match[2])
        if strength is None:
            fixed = QLabel(match[2], parent)
            fixed.setToolTip(_("Strength is a wildcard, edit it in the prompt"))
            fixed.setStyleSheet(f"color: {theme.grey};")
            layout.addWidget(fixed, row, 1, 1, 2)
            return

        slider = QSlider(Qt.Orientation.Horizontal, parent)
        slider.setRange(int(_min_strength * _slider_scale), int(_max_strength * _slider_scale))
        slider.setSingleStep(5)
        slider.setPageStep(10)
        spin = QDoubleSpinBox(parent)
        spin.setRange(_min_strength, _max_strength)
        spin.setSingleStep(0.05)
        spin.setDecimals(2)
        spin.setFixedWidth(64)
        slider.setValue(round(strength * _slider_scale))
        spin.setValue(strength)
        slider.valueChanged.connect(lambda v: spin.setValue(v / _slider_scale))
        spin.valueChanged.connect(lambda v: slider.setValue(round(v * _slider_scale)))
        spin.valueChanged.connect(lambda v: self._set_strength(row, v))
        layout.addWidget(slider, row, 1)
        layout.addWidget(spin, row, 2)

    def _replace(self, index: int, replacement: str):
        text = self._prompt.toPlainText()
        if (match := nth_lora(text, index)) is None:
            return
        start, end = match.span()
        if not replacement:  # take a separating comma and space along
            tail = re.match(r"\s*,?\s*", text[end:])
            end += tail.end() if tail else 0
        cursor = QTextCursor(self._prompt.document())
        if index == self._last_edited:
            cursor.joinPreviousEditBlock()
        else:
            cursor.beginEditBlock()
        cursor.setPosition(str_index_to_char16_index(text, start))
        cursor.setPosition(str_index_to_char16_index(text, end), QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement)
        cursor.endEditBlock()
        self._last_edited = index

    def _set_strength(self, index: int, value: float):
        if match := nth_lora(self._prompt.toPlainText(), index):
            if parse_strength(match[2]) != value:
                self._replace(index, with_strength(match, value))

    def _remove(self, index: int):
        self._replace(index, "")
        self._rebuild()
        self.adjustSize()
