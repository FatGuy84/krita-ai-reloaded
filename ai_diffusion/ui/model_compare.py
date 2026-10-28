from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

from PyQt5.QtCore import QObject, QRect, Qt
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..backend.client import ClientModels, resolve_arch
from ..backend.lora_manager import arch_for_base_model, load_cached_loras
from ..backend.resources import Arch
from ..image import Bounds, Extent, Image
from ..localization import translate as _
from ..model.jobs import Job, JobKind, JobState
from ..model.model import DocumentModel
from ..model.root import root
from ..text import pattern_lora
from . import theme
from .generate_sets import GenerateSetBar

_ARCH_ANY = "__any__"


def short_name(checkpoint: str) -> str:
    return Path(checkpoint.replace("\\", "/")).stem


def _lora_key(name: str) -> str:
    return short_name(name).lower()


class LoraArchCheck:
    """Decides which of the user's LoRAs can run on a checkpoint of a given arch.

    Lora Manager's cached base model is preferred; LoRAs it doesn't know are assumed
    to fit the arch of the style's own checkpoint, since that's what they were picked for.
    Sampling LoRAs added by the workflow (LCM, Hyper, ...) are not the user's and always kept.
    """

    def __init__(self, user_loras: list[str], original_arch: Arch, lora_manager_url: str):
        self._user = {_lora_key(n) for n in user_loras}
        self._original = original_arch
        self._known: dict[str, Arch] = {}
        for info in load_cached_loras(lora_manager_url) or []:
            if arch_name := arch_for_base_model(info.base_model):
                self._known[info.name.lower()] = Arch[arch_name]

    def fits(self, lora: str, arch: Arch) -> bool:
        key = _lora_key(lora)
        if key not in self._user:
            return True
        return Arch.is_compatible(self._known.get(key, self._original), arch)

    def dropped(self, arch: Arch) -> list[str]:
        return sorted(n for n in self._user if not self.fits(n, arch))


class _CompareRun(QObject):
    """Collects the results of one comparison as the jobs finish, then adds them as
    layers and/or a labelled grid. Outlives the dialog, so it parents itself to the model."""

    _active: set[_CompareRun] = set()

    def __init__(
        self,
        model: DocumentModel,
        checkpoints: list[str],
        seed: int | None,
        as_layers: bool,
        make_grid: bool,
    ):
        super().__init__(model)
        self._model = model
        self._order = checkpoints
        self._pending = set(checkpoints)
        self._seed = seed
        self._as_layers = as_layers
        self._make_grid = make_grid
        self._started = datetime.now(timezone.utc)
        self._results: dict[str, Job] = {}
        model.jobs.job_finished.connect(self._collect)
        model.jobs.count_changed.connect(self._check_abandoned)
        _CompareRun._active.add(self)

    def _matches(self, job: Job):
        checkpoint = job.params.metadata.get("checkpoint", "")
        return (
            job.kind is JobKind.diffusion
            and checkpoint in self._pending
            and job.timestamp >= self._started
            and (self._seed is None or job.params.seed == self._seed)
            and len(job.results) > 0
        )

    def _collect(self, job: Job):
        if not self._matches(job):
            return
        checkpoint = job.params.metadata["checkpoint"]
        self._pending.discard(checkpoint)
        self._results[checkpoint] = job
        if not self._pending:
            self._finish()

    def _check_abandoned(self):
        # Jobs got cancelled: build from what arrived, once nothing is left to wait for
        jobs = self._model.jobs
        if self._results and jobs.count(JobState.queued) + jobs.count(JobState.executing) == 0:
            self._finish()

    def _finish(self):
        jobs = self._model.jobs
        jobs.job_finished.disconnect(self._collect)
        jobs.count_changed.disconnect(self._check_abandoned)
        _CompareRun._active.discard(self)
        ordered = [(c, self._results[c]) for c in self._order if c in self._results]
        if self._as_layers:
            layers = self._model.layers
            for checkpoint, job in ordered:
                image = job.results[0]
                bounds = Bounds(*job.params.bounds.offset, *image.extent)
                layers.create(
                    f"[Model] {short_name(checkpoint)} ({job.params.seed})", image, bounds
                )
        if self._make_grid and len(ordered) > 1:
            grid = build_grid([(short_name(c), j.results[0]) for c, j in ordered], self._model)
            seed = f" (seed {self._seed})" if self._seed is not None else ""
            self._model.layers.create(f"[Model comparison]{seed}", grid, Bounds(0, 0, *grid.extent))
        self.setParent(None)


def build_grid(entries: list[tuple[str, Image]], model: DocumentModel) -> Image:
    """Images in a near-square grid with a name strip above each, scaled down so the
    whole grid fits the canvas."""
    cols = math.ceil(math.sqrt(len(entries)))
    rows = math.ceil(len(entries) / cols)
    first = entries[0][1].extent
    canvas = model.document.extent
    label_ratio = 0.08
    scale = min(
        1.0,
        canvas.width / (cols * first.width),
        canvas.height / (rows * first.height * (1 + label_ratio)),
    )
    cell_w = max(1, int(first.width * scale))
    cell_h = max(1, int(first.height * scale))
    label_h = max(18, int(cell_h * label_ratio))
    grid = Image.create(Extent(cols * cell_w, rows * (cell_h + label_h)), fill=QColor(24, 24, 24))

    painter = QPainter(grid._qimage)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    font = QFont()
    font.setPixelSize(max(10, int(label_h * 0.6)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(235, 235, 235))
    metrics = painter.fontMetrics()
    for i, (name, image) in enumerate(entries):
        x, y = (i % cols) * cell_w, (i // cols) * (cell_h + label_h)
        text = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, cell_w - 8)
        painter.drawText(QRect(x, y, cell_w, label_h), Qt.AlignmentFlag.AlignCenter, text)
        painter.drawImage(QRect(x, y + label_h, cell_w, cell_h), image._qimage)
    painter.end()
    return grid


class ModelCompareDialog(QDialog):
    """Generate the current prompt once per selected checkpoint, all other style
    settings unchanged, so the model is the only variable."""

    _last_selection: list[str] = []
    _last_grid = True
    _last_layers = False

    def __init__(self, model: DocumentModel, models: ClientModels, parent: QWidget | None = None):
        super().__init__(parent)
        self._model = model
        self._models = models
        self.setWindowTitle(_("Compare Models"))
        self._check = self._lora_check()

        self._search = QLineEdit(self)
        self._search.setPlaceholderText(_("Search checkpoints…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        self._arch_combo = QComboBox(self)
        self._arch_combo.addItem(_("Any architecture"), _ARCH_ANY)
        archs = sorted({info.arch for info in models.checkpoints.values()}, key=lambda a: a.text)
        for arch in archs:
            self._arch_combo.addItem(arch.text, arch.name)
        self._arch_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self._search, 1)
        filter_row.addWidget(self._arch_combo)

        self._list = QListWidget(self)
        current = set(model.style.checkpoints)
        previous = set(self._last_selection)
        for id in sorted(models.checkpoints, key=lambda c: short_name(c).lower()):
            info = models.checkpoints[id]
            item = QListWidgetItem(f"{short_name(id)}  ·  {info.arch.text}", self._list)
            item.setData(Qt.ItemDataRole.UserRole, id)
            item.setToolTip(id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = id in previous if previous else id in current
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._list.itemChanged.connect(self._update)

        select_all = QPushButton(_("All shown"), self)
        select_all.clicked.connect(lambda: self._check_shown(True))
        select_none = QPushButton(_("None"), self)
        select_none.clicked.connect(lambda: self._check_shown(False))
        self._set_bar = GenerateSetBar(
            "checkpoint_sets", self._checked_stems, self._apply_set, self
        )
        select_row = QHBoxLayout()
        select_row.addWidget(select_all)
        select_row.addWidget(select_none)
        select_row.addStretch()

        self._fixed = QCheckBox(_("Fixed seed"), self)
        self._fixed.setChecked(True)
        self._fixed.setToolTip(
            _("Same seed for every model, so the model is the only difference. Off: random seeds.")
        )
        self._seed = QSpinBox(self)
        self._seed.setRange(0, 2**31 - 1)
        self._seed.setValue(int(model.seed) % (2**31))
        self._fixed.toggled.connect(self._seed.setEnabled)
        seed_row = QHBoxLayout()
        seed_row.addWidget(self._fixed)
        seed_row.addWidget(self._seed, 1)

        self._grid = QCheckBox(_("Add a labelled grid layer when done"), self)
        self._grid.setChecked(self._last_grid)
        self._layers = QCheckBox(_("Add each result as a layer"), self)
        self._layers.setChecked(self._last_layers)
        self._layers.setToolTip(
            _("Results always go to the history; this also puts them on the canvas")
        )

        self._summary = QLabel(self)
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(f"color: {theme.grey};")

        self._button = QPushButton(self)
        self._button.clicked.connect(self._generate)
        close = QPushButton(_("Close"), self)
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(close)
        buttons.addWidget(self._button)

        layout = QVBoxLayout(self)
        layout.addLayout(filter_row)
        layout.addWidget(self._list, 1)
        layout.addLayout(select_row)
        layout.addWidget(self._set_bar)
        layout.addLayout(seed_row)
        layout.addWidget(self._grid)
        layout.addWidget(self._layers)
        layout.addWidget(self._summary)
        layout.addLayout(buttons)
        self.setMinimumSize(420, 520)
        self._update()

    def _items(self):
        return [self._list.item(i) for i in range(self._list.count())]

    def _apply_filter(self):
        search = self._search.text().strip().lower()
        arch = self._arch_combo.currentData()
        for item in self._items():
            id = item.data(Qt.ItemDataRole.UserRole)
            info = self._models.checkpoints[id]
            visible = search in id.lower() and arch in (_ARCH_ANY, info.arch.name)
            item.setHidden(not visible)

    def _check_shown(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._list.blockSignals(True)
        for item in self._items():
            if not item.isHidden():
                item.setCheckState(state)
        self._list.blockSignals(False)
        self._update()

    def checked(self) -> list[str]:
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self._items()
            if item.checkState() == Qt.CheckState.Checked
        ]

    def _checked_stems(self):
        return [short_name(c) for c in self.checked()]

    def _apply_set(self, names: list[str]):
        # sets are shared with the checkpoint browser, which keys them by file stem
        wanted = {n.lower() for n in names}
        self._search.clear()
        self._arch_combo.setCurrentIndex(0)
        self._list.blockSignals(True)
        for item in self._items():
            hit = short_name(item.data(Qt.ItemDataRole.UserRole)).lower() in wanted
            item.setCheckState(Qt.CheckState.Checked if hit else Qt.CheckState.Unchecked)
        self._list.blockSignals(False)
        self._update()

    def _lora_check(self):
        model = self._model
        style = model.active_style
        user_loras = [str(l["name"]) for l in style.loras if l.get("enabled", True)]
        user_loras += [m[1] for m in pattern_lora.finditer(model.regions.positive)]
        client = root.connection.client_if_connected
        url = client.url if client else ""
        return LoraArchCheck(user_loras, resolve_arch(style, self._models), url)

    def _update(self):
        n = len(self.checked())
        self._button.setText(_("Generate {n} images").format(n=n))
        self._button.setEnabled(n > 0)
        check = self._check
        skipped = []
        for id in self.checked():
            if dropped := check.dropped(self._models.arch_of(id)):
                skipped.append(f"{short_name(id)}: {', '.join(dropped)}")
        text = _("{n} checkpoints selected").format(n=n)
        if skipped:
            text += "\n" + _("LoRAs skipped (other architecture):") + "\n" + "\n".join(skipped)
        self._summary.setText(text)

    def _generate(self):
        ids = self.checked()
        if not ids:
            return
        model = self._model
        ModelCompareDialog._last_selection = ids
        ModelCompareDialog._last_grid = self._grid.isChecked()
        ModelCompareDialog._last_layers = self._layers.isChecked()
        style = model.active_style
        check = self._check
        mismatched = [i for i in ids if check.dropped(self._models.arch_of(i))]
        if mismatched:
            names = "\n".join(short_name(i) for i in mismatched)
            answer = QMessageBox.question(
                self,
                _("Compare Models"),
                _("Some LoRAs don't fit these checkpoints and will be left out for them:")
                + f"\n\n{names}\n\n"
                + _("Continue?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        original = (list(style.checkpoints), style.architecture, model.batch_count)

        def use(checkpoint: str):
            def apply():
                arch = self._models.arch_of(checkpoint)
                style.checkpoints = [checkpoint]
                # a pinned arch that doesn't fit would force the wrong pipeline
                if not Arch.is_compatible(original[1], arch):
                    style.architecture = Arch.auto
                model.batch_count = 1
                model.lora_filter = lambda lora: check.fits(lora, arch)

            return apply

        def restore():
            style.checkpoints, style.architecture, model.batch_count = original
            model.lora_filter = None

        seed = self._seed.value() if self._fixed.isChecked() else None
        _CompareRun(model, ids, seed, self._layers.isChecked(), self._grid.isChecked())
        if seed is not None:
            model.generate_across([use(i) for i in ids], seed, restore)
        else:
            original_fixed = model.fixed_seed
            model.fixed_seed = False
            try:
                for i in ids:
                    use(i)()
                    model.generate()
            finally:
                restore()
                model.fixed_seed = original_fixed
        self.close()
