from __future__ import annotations

import asyncio
import re
from pathlib import Path

from PyQt5.QtCore import QEvent, QObject, QPoint, Qt
from PyQt5.QtGui import QPixmap, QTextCursor
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

from .. import eventloop
from ..backend.lora_manager import fetch_preview_bytes, load_cached_loras
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
from ..util import ensure, trim_text
from . import theme
from .lora_picker import _extract_video_frame, _is_video_url

_min_strength, _max_strength = -2.0, 3.0
_preview_size = 256
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


def toggle_free_group(text: str, index: int, group: str):
    """For a LoRA without a block: puts the trigger group right after the tag, or takes
    it out of the prompt if it is in there."""
    if (match := nth_lora(text, index)) is None:
        return text
    if has_trigger_group(text, group):
        return remove_trigger_group(text, group)
    rest = text[match.end() :]
    if rest.lstrip(" ").startswith(","):  # keep the tag's own comma: "<lora>, group, rest"
        return text[: match.end()] + ", " + group + rest
    if rest.strip() and not rest.lstrip(" ").startswith("\n"):
        rest = ", " + rest.lstrip(" ")
    return text[: match.end()] + " " + group + rest


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


def _known_loras() -> tuple[dict[str, list[str]], dict[str, str]]:
    """Trigger groups and preview urls per LoRA. Triggers come from the Lora Manager list
    the LoRA browser caches, else the trigger words set for the file in the style editor.
    An expired cache still counts: trigger words rarely change, and the popup must not
    lose them just because the browser was not opened for a few hours."""
    triggers: dict[str, list[str]] = {}
    previews: dict[str, str] = {}
    for file in root.files.loras:
        if words := file.meta("lora_triggers", ""):
            triggers[lora_key(file.id)] = [words]
    if client := root.connection.client_if_connected:
        for lora in load_cached_loras(client.url, any_age=True) or []:
            key = lora_key(lora.name)
            if groups := [g.strip() for g in lora.trigger_words if g.strip()]:
                triggers[key] = groups
            if lora.preview_url:
                previews[key] = lora.preview_url
    return triggers, previews


_preview_pixmaps: dict[str, QPixmap | None] = {}  # url -> scaled preview, None = failed


class _PreviewHover(QObject):
    """Shows the LoRA's preview image next to its name while the mouse is over it."""

    def __init__(self, label: QLabel, url: str):
        super().__init__(label)
        self._label = label
        self._url = url
        self._hovered = False
        self._popup: QLabel | None = None
        label.installEventFilter(self)

    def eventFilter(self, a0, a1):
        if a1 is not None and a1.type() == QEvent.Type.Enter:
            self._hovered = True
            if self._url in _preview_pixmaps:
                self._show()
            else:
                _preview_pixmaps[self._url] = None  # one fetch at a time
                eventloop.run(self._fetch())
        elif a1 is not None and a1.type() in (QEvent.Type.Leave, QEvent.Type.Hide):
            self._hovered = False
            self._hide()
        return False

    async def _fetch(self):
        pixmap = None
        if client := root.connection.client_if_connected:
            data = await fetch_preview_bytes(client._requests, self._url)
            if data and _is_video_url(self._url):
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, _extract_video_frame, data)
            if data:
                pixmap = QPixmap()
                pixmap.loadFromData(data)
                if pixmap.isNull():
                    pixmap = None
                else:
                    pixmap = pixmap.scaled(
                        _preview_size,
                        _preview_size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
        _preview_pixmaps[self._url] = pixmap
        try:
            if self._hovered:
                self._show()
        except RuntimeError:
            pass  # popup closed before the fetch finished

    def _show(self):
        pixmap = _preview_pixmaps.get(self._url)
        if pixmap is None:
            return
        popup = self._popup
        if popup is None:
            popup = self._popup = QLabel(self._label, Qt.WindowType.ToolTip)
            popup.setStyleSheet(f"border: 1px solid {theme.line}; background: black;")
            popup.setPixmap(pixmap)
            popup.adjustSize()
        # beside the menu, so it covers neither the rows nor the prompt
        menu = ensure(self._label.window())
        y = self._label.mapToGlobal(QPoint(0, 0)).y() - popup.height() // 2
        x = menu.mapToGlobal(QPoint(0, 0)).x() - popup.width() - 4
        if x < 0:
            x = menu.mapToGlobal(QPoint(menu.width(), 0)).x() + 4
        popup.move(x, max(0, y))
        popup.show()

    def _hide(self):
        if self._popup is not None:
            self._popup.hide()


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
        triggers, previews = _known_loras() if matches else ({}, {})
        if not matches:
            empty = QLabel(_("No LoRAs in the prompt"), content)
            empty.setStyleSheet(f"font-style: italic; color: {theme.grey};")
            layout.addWidget(empty, 0, 0)
        for index, match in enumerate(matches):
            key = lora_key(match[1])
            self._add_row(layout, index, match, text, triggers.get(key, []), previews.get(key))
        self._container_layout.addWidget(content)
        self._content = content

    def _add_row(
        self,
        layout: QGridLayout,
        index: int,
        match: re.Match[str],
        text: str,
        groups: list[str],
        preview: str | None,
    ):
        parent = layout.parentWidget()
        row = 2 * index  # the row below holds the trigger groups
        close = lora_block_end(text, match)
        name = QLabel(trim_text(Path(match[1].replace("\\", "/")).stem, 40), parent)
        name.setToolTip(match[0] if close is None else text[match.start() : close] + lora_end)
        if preview:
            _PreviewHover(name, preview)
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
        if groups:
            content = text if close is None else text[match.end() : close]
            self._add_trigger_chips(layout, row + 1, index, content, groups, close is None)

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
        self,
        layout: QGridLayout,
        row: int,
        index: int,
        content: str,
        groups: list[str],
        free: bool,
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
            toggle = toggle_free_group if free else toggle_group
            chip.clicked.connect(lambda _checked, g=group, t=toggle: self._edit(t, index, g))
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
