from __future__ import annotations

import re
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ..backend.lora_manager import load_cached_loras
from ..localization import translate as _
from ..model.root import root
from ..settings import settings
from ..text import (
    add_trigger_group,
    has_trigger_group,
    lora_block_end,
    lora_end,
    pattern_lora,
    remove_trigger_group,
    str_index_to_char16_index,
)
from ..util import trim_text
from . import theme

_min_strength, _max_strength = -2.0, 3.0
_slider_scale = 100  # slider works in hundredths
_chips_per_row = 4


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


def lora_key(name: str):
    return Path(name.replace("\\", "/")).stem.lower()


def remove_lora(text: str, index: int):
    """Removes the tag with its trigger block, and a separating comma and space."""
    if (match := nth_lora(text, index)) is None:
        return text
    close = lora_block_end(text, match)
    end = match.end() if close is None else close + len(lora_end)
    tail = re.match(r"\s*,?\s*", text[end:])
    return text[: match.start()] + text[end + (tail.end() if tail else 0) :]


def toggle_group(text: str, index: int, group: str):
    """Adds the trigger group to the LoRA's block, or takes it out if it is in there."""
    if (match := nth_lora(text, index)) is None:
        return text
    if (close := lora_block_end(text, match)) is None:
        return text
    content = text[match.end() : close]
    if has_trigger_group(content, group):
        content = remove_trigger_group(content, group)
        content = content.rstrip(" ,") if content.strip(" ,") else ""
    else:
        content = add_trigger_group(content, group)
    return text[: match.end()] + content + text[close:]


def wrap_lora(text: str, index: int, groups: list[str]):
    """Gives a LoRA without a block one: its trigger groups found in the prompt (outside
    other LoRAs' blocks) move into <lora:...> groups</lora> right after the tag."""
    if (match := nth_lora(text, index)) is None or lora_block_end(text, match) is not None:
        return text
    # hide this tag and the other blocks behind placeholders while looking for triggers;
    # they end in '>' so a trigger right after the tag still counts as a whole part
    hidden: list[str] = []
    parts, pos, own = [], 0, ""
    for i, tag in enumerate(pattern_lora.finditer(text)):
        close = lora_block_end(text, tag)
        if i == index:
            end = tag.end()
        elif close is not None:
            end = close + len(lora_end)
        else:
            continue
        placeholder = f"<\x00{len(hidden)}>"
        own = placeholder if i == index else own
        parts += [text[pos : tag.start()], placeholder]
        hidden.append(text[tag.start() : end])
        pos = end
    masked = "".join(parts) + text[pos:]

    found = [g for g in groups if has_trigger_group(masked, g)]
    for group in found:
        masked = remove_trigger_group(masked, group)
    at = masked.index(own) + len(own)
    rest = masked[at:]
    if rest.strip() and not rest.lstrip(" ").startswith((",", "\n", "|", "]]")):
        rest = ", " + rest.lstrip(" ")
    block = (" " + ", ".join(found) if found else "") + lora_end
    masked = masked[:at] + block + rest
    for i, original in enumerate(hidden):
        masked = masked.replace(f"<\x00{i}>", original, 1)
    return masked


def _known_triggers() -> dict[str, list[str]]:
    """Trigger groups per LoRA, from the Lora Manager list the LoRA browser caches, else
    the trigger words set for the file in the style editor."""
    result: dict[str, list[str]] = {}
    for file in root.files.loras:
        if triggers := file.meta("lora_triggers", ""):
            result[lora_key(file.id)] = [triggers]
    if client := root.connection.client_if_connected:
        for lora in load_cached_loras(client.url) or []:
            if groups := [g.strip() for g in lora.trigger_words if g.strip()]:
                result[lora_key(lora.name)] = groups
    return result


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
            self._content.deleteLater()  # may be running the clicked button's signal
        content = QWidget(self._container)
        layout = QGridLayout(content)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setColumnMinimumWidth(1, 160)
        text = self._prompt.toPlainText()
        matches = list(pattern_lora.finditer(text))
        triggers = _known_triggers() if settings.lora_trigger_blocks and matches else {}
        if not matches:
            empty = QLabel(_("No LoRAs in the prompt"), content)
            empty.setStyleSheet(f"font-style: italic; color: {theme.grey};")
            layout.addWidget(empty, 0, 0)
        for index, match in enumerate(matches):
            groups = triggers.get(lora_key(match[1]), [])
            self._add_row(layout, index, match, text, groups)
        self._container_layout.addWidget(content)
        self._content = content

    def _add_row(
        self, layout: QGridLayout, index: int, match: re.Match[str], text: str, groups: list[str]
    ):
        parent = layout.parentWidget()
        row = 2 * index  # the row below holds the trigger groups
        close = lora_block_end(text, match)
        name = QLabel(trim_text(Path(match[1].replace("\\", "/")).stem, 40), parent)
        name.setToolTip(match[0] if close is None else text[match.start() : close] + lora_end)
        layout.addWidget(name, row, 0)

        remove = QToolButton(parent)
        remove.setIcon(theme.icon("remove"))
        remove.setAutoRaise(True)
        remove.setToolTip(_("Remove this LoRA and its trigger words from the prompt"))
        remove.clicked.connect(lambda: self._edit(remove_lora, index))
        layout.addWidget(remove, row, 4)

        if settings.lora_trigger_blocks and close is None:
            wrap = QToolButton(parent)
            wrap.setIcon(theme.icon("link"))
            wrap.setAutoRaise(True)
            wrap.setToolTip(
                _("Bind trigger words to this LoRA")
                + ":\n<lora:...> "
                + _("trigger words")
                + lora_end
                + "\n"
                + _("Its trigger words found in the prompt move into the block")
            )
            wrap.clicked.connect(lambda: self._edit(wrap_lora, index, groups))
            layout.addWidget(wrap, row, 3)
        elif close is not None and groups:
            self._add_trigger_chips(layout, row + 1, index, text[match.end() : close], groups)

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
        spin.valueChanged.connect(lambda v: self._set_strength(index, v))
        layout.addWidget(slider, row, 1)
        layout.addWidget(spin, row, 2)

    def _add_trigger_chips(
        self, layout: QGridLayout, row: int, index: int, content: str, groups: list[str]
    ):
        chips = QWidget(layout.parentWidget())
        chip_layout = QGridLayout(chips)
        chip_layout.setContentsMargins(12, 0, 0, 4)
        chip_layout.setHorizontalSpacing(4)
        chip_layout.setVerticalSpacing(2)
        # the default checked look is barely visible in most themes: active chips get the
        # highlight border and a check mark, inactive ones go grey and dashed
        chips.setStyleSheet(
            "QToolButton { padding: 1px 6px; border-radius: 3px;"
            f" color: {theme.grey}; border: 1px dashed {theme.grey}; background: transparent; }}"
            f" QToolButton:checked {{ color: palette(text); border: 1px solid {theme.active};"
            f" background: {theme.line}; }}"
            f" QToolButton:hover {{ border-color: {theme.highlight}; }}"
        )
        for i, group in enumerate(groups):
            active = has_trigger_group(content, group)
            chip = QToolButton(chips)
            chip.setText(("✓ " if active else "") + trim_text(group, 32))
            chip.setToolTip(
                group
                + "\n"
                + (
                    _("In the prompt, click to remove")
                    if active
                    else _("Click to add to the prompt")
                )
            )
            chip.setCheckable(True)
            chip.setChecked(active)
            chip.clicked.connect(lambda _checked, g=group: self._edit(toggle_group, index, g))
            chip_layout.addWidget(chip, i // _chips_per_row, i % _chips_per_row)
        row_layout = QHBoxLayout()
        row_layout.addWidget(chips)
        row_layout.addStretch()
        layout.addLayout(row_layout, row, 0, 1, 5)

    def _apply(self, new_text: str, key: int | None = None):
        """Writes the change into the prompt as one undo step, replacing only the part that
        differs so the cursor and scroll position stay. Edits with the same `key` (one
        slider being dragged) merge into one step."""
        text = self._prompt.toPlainText()
        start, n = 0, min(len(text), len(new_text))
        while start < n and text[start] == new_text[start]:
            start += 1
        end, new_end = len(text), len(new_text)
        while end > start and new_end > start and text[end - 1] == new_text[new_end - 1]:
            end, new_end = end - 1, new_end - 1
        cursor = QTextCursor(self._prompt.document())
        if key is not None and key == self._last_edited:
            cursor.joinPreviousEditBlock()
        else:
            cursor.beginEditBlock()
        cursor.setPosition(str_index_to_char16_index(text, start))
        cursor.setPosition(str_index_to_char16_index(text, end), QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_text[start:new_end])
        cursor.endEditBlock()
        self._last_edited = key

    def _set_strength(self, index: int, value: float):
        text = self._prompt.toPlainText()
        if (match := nth_lora(text, index)) and parse_strength(match[2]) != value:
            start, end = match.span()
            self._apply(text[:start] + with_strength(match, value) + text[end:], index)

    def _edit(self, change, *args):
        """Applies a change that alters the rows (remove, bind, trigger toggle)."""
        new_text = change(self._prompt.toPlainText(), *args)
        if new_text != self._prompt.toPlainText():
            self._apply(new_text)
        self._rebuild()
        self.adjustSize()
