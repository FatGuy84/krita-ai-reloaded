from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import ClassVar, cast

from krita import Krita
from PyQt5.QtCore import QObject, QRect, Qt
from PyQt5.QtGui import QColor, QFont, QPainter

from .image import Extent, Image
from .model.jobs import Job, JobKind, JobQueue, JobState
from .settings import settings
from .text import sequential_groups, strip_prompt_comments
from .util import client_logger as log
from .util import trim_text

# Krita and Qt get slow and memory hungry beyond this, cells shrink to stay below it
_max_grid_side = 16384
_label_ratio = 0.08
_background = QColor(24, 24, 24)


def _label_painter(grid: Image, label_h: int):
    painter = QPainter(grid._qimage)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    font = QFont()
    font.setPixelSize(max(10, int(label_h * 0.6)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(235, 235, 235))
    return painter


def build_grid(entries: list[tuple[str, Image]]) -> Image:
    """Images in a near-square grid with a name strip above each, at full resolution
    unless that would exceed the maximum size."""
    cols = math.ceil(math.sqrt(len(entries)))
    rows = math.ceil(len(entries) / cols)
    first = entries[0][1].extent
    scale = min(
        1.0,
        _max_grid_side / (cols * first.width),
        _max_grid_side / (rows * first.height * (1 + _label_ratio)),
    )
    cell_w = max(1, int(first.width * scale))
    cell_h = max(1, int(first.height * scale))
    label_h = max(18, int(cell_h * _label_ratio))
    grid = Image.create(Extent(cols * cell_w, rows * (cell_h + label_h)), fill=_background)

    painter = _label_painter(grid, label_h)
    metrics = painter.fontMetrics()
    for i, (name, image) in enumerate(entries):
        x, y = (i % cols) * cell_w, (i // cols) * (cell_h + label_h)
        text = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, cell_w - 8)
        painter.drawText(QRect(x, y, cell_w, label_h), Qt.AlignmentFlag.AlignCenter, text)
        painter.drawImage(QRect(x, y + label_h, cell_w, cell_h), image._qimage)
    painter.end()
    return grid


def _axis_label(option: str):
    return trim_text(" ".join(option.split()), 80)


def xy_row_labels(groups: list[list[str]]) -> list[str]:
    """One label per grid row: the options of every group after the first (which are
    the columns), one line each. Later groups change slower, as in eval_wildcards."""
    rest = groups[1:]
    rows = math.prod(len(g) for g in rest)
    labels = []
    for row in range(rows):
        lines, stride = [], 1
        for options in rest:
            lines.append(_axis_label(options[(row // stride) % len(options)]))
            stride *= len(options)
        labels.append("\n".join(lines))
    return labels


def build_xy_grid(cells: dict[int, Image], groups: list[list[str]]) -> Image:
    """Results of a sequential wildcard batch laid out by combination: the first
    [[...]] group runs along the columns, all later ones down the rows. Cells are keyed
    by batch index, missing ones (cancelled jobs) stay empty."""
    cols = len(groups[0])
    row_labels = xy_row_labels(groups) if len(groups) > 1 else []
    rows = max(1, len(row_labels))
    first = next(iter(cells.values())).extent
    header_ratio = 0.3 if row_labels else 0.0
    scale = min(
        1.0,
        _max_grid_side / (first.width * (cols + header_ratio)),
        _max_grid_side / (first.height * (rows + _label_ratio)),
    )
    cell_w = max(1, int(first.width * scale))
    cell_h = max(1, int(first.height * scale))
    label_h = max(18, int(cell_h * _label_ratio))
    header_w = int(cell_w * header_ratio)
    grid = Image.create(Extent(header_w + cols * cell_w, label_h + rows * cell_h), _background)

    painter = _label_painter(grid, label_h)
    metrics = painter.fontMetrics()
    center = Qt.AlignmentFlag.AlignCenter
    for col, option in enumerate(groups[0]):
        text = metrics.elidedText(_axis_label(option), Qt.TextElideMode.ElideMiddle, cell_w - 8)
        painter.drawText(QRect(header_w + col * cell_w, 0, cell_w, label_h), center, text)
    for row, text in enumerate(row_labels):
        rect = QRect(4, label_h + row * cell_h, header_w - 8, cell_h)
        painter.drawText(rect, int(center | Qt.TextFlag.TextWordWrap), text)
    for index, image in cells.items():
        col, row = index % cols, index // cols
        if row < rows:
            rect = QRect(header_w + col * cell_w, label_h + row * cell_h, cell_w, cell_h)
            painter.drawImage(rect, image._qimage)
    painter.end()
    return grid


_part_split = re.compile(r"[,\n]")


def _prompt_parts(prompt: str):
    parts = (" ".join(p.split()) for p in _part_split.split(strip_prompt_comments(prompt)))
    return [p for p in parts if p]


def variant_labels(prompts: list[str], seeds: list[int]) -> list[str]:
    """What sets each image apart from the rest of its batch: the prompt parts not all
    of them share, and the seed if seeds differ (or nothing else does)."""
    parts = [_prompt_parts(p) for p in prompts]
    common = set.intersection(*(set(p) for p in parts)) if parts else set()
    show_seed = len(set(seeds)) > 1
    labels = []
    for prompt_parts, seed in zip(parts, seeds):
        text = ", ".join(p for p in prompt_parts if p not in common)
        if show_seed or not text:
            text = f"{text} · {seed}" if text else f"seed {seed}"
        labels.append(trim_text(text, 160))
    return labels


def _job_prompt(job: Job):
    return job.params.metadata.get("prompt_eval") or job.params.name


def _xy_cells(jobs: list[Job]):
    """Cells and groups of a sequential wildcard batch run with XY grid on, else None."""
    indices = [job.params.metadata.get("batch_index") for job in jobs]
    if any(i is None for i in indices):
        return None
    groups = sequential_groups(strip_prompt_comments(jobs[0].params.metadata.get("prompt", "")))
    if not groups or max(cast(int, i) for i in indices) >= math.prod(len(g) for g in groups):
        return None
    return {cast(int, i): job.results[0] for i, job in zip(indices, jobs)}, groups


def batch_grid(jobs: list[Job]) -> tuple[Image, str] | None:
    """A grid of a history batch and a name for its document. Batches from an XY grid
    run get their wildcard layout back, others a grid labelled by what differs."""
    jobs = [j for j in jobs if j.kind is JobKind.diffusion and len(j.results) > 0]
    jobs.sort(key=lambda j: j.params.metadata.get("batch_index", 0))
    if not jobs:
        return None
    first = jobs[0].params
    name = trim_text(first.metadata.get("prompt", first.name), 200)
    if (xy := _xy_cells(jobs)) and len(xy[0]) > 1:
        return build_xy_grid(*xy), f"[XY Grid] {name} ({first.seed})"

    images: list[tuple[Job, int]] = []
    for job in jobs:
        count = 1 if job.params.is_layered else len(job.results)
        images += [(job, i) for i in range(count)]
    if len(images) < 2:
        return None
    labels = variant_labels([_job_prompt(j) for j, _ in images], [j.params.seed for j, _ in images])
    entries = []
    for label, (job, i) in zip(labels, images):
        suffix = f" #{i + 1}" if len(job.results) > 1 and not job.params.is_layered else ""
        entries.append((label + suffix, job.results[i]))
    return build_grid(entries), f"[Grid] {name}"


def save_grid(image: Image):
    """Writes the grid to the grid folder as "<date time> grid.png", so it survives
    closing the unsaved document."""
    if not settings.grid_save_folder:
        return
    try:
        folder = Path(settings.grid_save_folder)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        path, n = folder / f"{stamp} grid.png", 2
        while path.exists():
            path, n = folder / f"{stamp} grid {n}.png", n + 1
        image.save(path)
        log.info(f"Saved grid to {path}")
    except Exception as e:
        log.error(f"Failed to save grid to {settings.grid_save_folder}: {e}")


def open_in_new_document(image: Image, name: str):
    """The grid is usually larger than the canvas, a document of its own keeps every
    cell at the resolution it was generated in."""
    w, h = image.extent
    doc = Krita.instance().createDocument(w, h, name, "RGBA", "U8", "", 300.0)
    layer = doc.createNode(name, "paintlayer")
    layer.setPixelData(image.data, 0, 0, w, h)
    doc.rootNode().addChildNode(layer, None)
    doc.refreshProjection()
    if window := Krita.instance().activeWindow():
        window.addView(doc)


class XYGridRun(QObject):
    """Collects the jobs of one sequential wildcard batch as they finish, then opens
    them as an XY grid. Jobs carry their position in the batch as "batch_index"."""

    _active: ClassVar[set[XYGridRun]] = set()

    def __init__(self, jobs: JobQueue, batch_id: str, groups: list[list[str]]):
        super().__init__()
        self._jobs = jobs
        self._batch_id = batch_id
        self._groups = groups
        self._cells: dict[int, Image] = {}
        self._first: Job | None = None
        self._enqueued = False
        jobs.job_finished.connect(self._collect)
        jobs.count_changed.connect(self._check_done)
        XYGridRun._active.add(self)

    def enqueued(self):
        self._enqueued = True
        self._check_done()

    def _ours(self, job: Job):
        return job.kind is JobKind.diffusion and job.params.batch_id == self._batch_id

    def _collect(self, job: Job):
        index = job.params.metadata.get("batch_index")
        if not self._ours(job) or index is None or len(job.results) == 0:
            return
        self._cells[index] = job.results[0]
        if self._first is None or index < self._first.params.metadata["batch_index"]:
            self._first = job
        self._check_done()

    def _check_done(self):
        # Wait while any job of the batch is still running, cancelled ones never arrive
        if not self._enqueued or any(
            self._ours(j) and j.state in (JobState.queued, JobState.executing) for j in self._jobs
        ):
            return
        self._jobs.job_finished.disconnect(self._collect)
        self._jobs.count_changed.disconnect(self._check_done)
        XYGridRun._active.discard(self)
        if len(self._cells) > 1 and self._first is not None:
            grid = build_xy_grid(self._cells, self._groups)
            save_grid(grid)
            params = self._first.params
            prompt = trim_text(params.metadata.get("prompt", params.name), 200)
            open_in_new_document(grid, f"[XY Grid] {prompt} ({params.seed})")
