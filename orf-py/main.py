#!/usr/bin/env python3
"""ORF Explorer - Desktop Qt version for browsing Olympus RAW (.orf) files."""

import sys
import os
import struct
import subprocess
import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QDir, QObject, Signal, QRunnable, QThreadPool, QPoint
from PySide6.QtGui import QPixmap, QIcon, QPalette, QColor, QStandardItemModel, QStandardItem, QPainter
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QTreeView, QListView,
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox, QMenu,
    QFileSystemModel, QAbstractItemView, QMessageBox, QDialog, QDialogButtonBox,
    QProgressBar, QGraphicsView, QGraphicsScene, QFormLayout, QLineEdit, QFileDialog
)

# --- ORF Preview Extractor (ported & improved from Go version) ---

def _scan_jpeg(data: bytes) -> Optional[bytes]:
    """Find a likely valid embedded JPEG segment (SOI ... EOI)."""
    allowed_first_markers = {
        0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF,
        0xDB, 0xC0, 0xC1, 0xC2, 0xC4, 0xDA, 0xFE,
    }
    best: Optional[bytes] = None
    search_pos = 0
    while True:
        start = data.find(b"\xff\xd8\xff", search_pos)
        if start == -1:
            break
        marker = data[start + 3] if start + 3 < len(data) else 0
        search_pos = start + 3
        if marker not in allowed_first_markers:
            continue
        end = data.find(b"\xff\xd9", start + 2)
        if end == -1:
            continue
        candidate = data[start : end + 2]
        if len(candidate) < 4096:
            continue
        # Prefer segments with JPEG metadata markers.
        if b"JFIF" in candidate[:64] or b"Exif" in candidate[:64]:
            return candidate
        if best is None or len(candidate) > len(best):
            best = candidate
    return best


def _type_size(field_type: int) -> int:
    sizes = {
        1: 1, 2: 1, 6: 1, 7: 1,
        3: 2, 8: 2,
        4: 4, 9: 4, 11: 4, 13: 4,   # 13 = IFD pointer (common in Olympus)
        5: 8, 10: 8, 12: 8,
    }
    return sizes.get(field_type, 0)


def _values_as_offsets(data: bytes, order: str, field_type: int, count: int, value: int, base_offset: int = 0) -> list[int]:
    size = _type_size(field_type)
    if size == 0 or count == 0:
        return []
    total = count * size
    if total <= 4:
        raw = struct.pack(order + "I", value)[:total]
    else:
        start = value
        end = start + total
        if start < 0 or end > len(data):
            return []
        raw = data[start:end]

    offsets: list[int] = []
    for i in range(count):
        pos = i * size
        if field_type in (3, 8):
            offsets.append(struct.unpack(order + "H", raw[pos:pos+2])[0])
        elif field_type in (4, 9, 13):
            offsets.append(struct.unpack(order + "I", raw[pos:pos+4])[0])
    return offsets


def _walk_ifd(data: bytes, order: str, offset: int, seen: set, depth: int = 0) -> Optional[bytes]:
    if depth > 16 or offset in seen or offset + 2 > len(data):
        return None
    seen.add(offset)

    base = offset
    count = struct.unpack(order + "H", data[base:base+2])[0]
    entry_start = base + 2
    entry_end = entry_start + count * 12
    if entry_end + 4 > len(data):
        return None

    jpeg_offset = jpeg_length = 0
    child_offsets: list[int] = []

    for pos in range(entry_start, entry_end, 12):
        tag, field_type, field_count, value = struct.unpack(order + "HHI I", data[pos:pos+12])

        if tag == 0x0201:          # JPEGInterchangeFormat
            jpeg_offset = value
        elif tag == 0x0202:        # JPEGInterchangeFormatLength
            jpeg_length = value
        elif tag in (0x014A, 0x8769):  # SubIFD, ExifIFD
            child_offsets.extend(_values_as_offsets(data, order, field_type, field_count, value))

    # Found direct JPEG pointer?
    if jpeg_offset > 0 and jpeg_length > 0:
        start = jpeg_offset
        end = start + jpeg_length
        if 0 <= start < end <= len(data) and data[start:start+2] == b"\xff\xd8":
            return data[start:end]

    # Next IFD
    next_ifd = struct.unpack(order + "I", data[entry_end:entry_end+4])[0]
    if next_ifd:
        child_offsets.append(next_ifd)

    for child in child_offsets:
        if child and child not in seen:
            jpeg = _walk_ifd(data, order, child, seen, depth + 1)
            if jpeg:
                return jpeg

    return None


def extract_raw_preview(path: str) -> Optional[bytes]:
    """Extract embedded JPEG preview from RAW files (ORF/NEF-like TIFF containers)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None

    if len(data) < 8:
        return _scan_jpeg(data)

    # Byte order
    if data[:2] == b"II":
        order = "<"
    elif data[:2] == b"MM":
        order = ">"
    else:
        return _scan_jpeg(data)

    # Primary IFD offsets
    first_ifd = struct.unpack(order + "I", data[4:8])[0]
    candidates = [first_ifd]
    if len(data) > 8:
        second = struct.unpack(order + "I", data[8:12])[0] if len(data) >= 12 else 0
        if second:
            candidates.append(second)

    seen: set[int] = set()
    for off in candidates:
        if off and off < len(data):
            jpeg = _walk_ifd(data, order, off, seen)
            if jpeg:
                return jpeg

    # Fallback: scan for JPEG markers
    return _scan_jpeg(data)


class ThumbnailSignals(QObject):
    finished = Signal(str, bytes, int)  # path, image bytes, load token


class ThumbnailTask(QRunnable):
    def __init__(self, path: str, token: int):
        super().__init__()
        self.path = path
        self.token = token
        self.signals = ThumbnailSignals()

    def run(self):
        ext = Path(self.path).suffix.lower()
        data = b""
        try:
            if ext in (".orf", ".nef"):
                preview = extract_raw_preview(self.path)
                if preview:
                    data = preview
            elif ext in (".jpg", ".jpeg"):
                with open(self.path, "rb") as f:
                    data = f.read()
        except Exception:
            data = b""
        self.signals.finished.emit(self.path, data, self.token)


class ZoomPanImageView(QGraphicsView):
    zoomChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pix_item = None
        self._panning = False
        self._last_pos = QPoint()
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setBackgroundBrush(QColor("#0f172a"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)

    def set_pixmap(self, pixmap: QPixmap):
        self._scene.clear()
        self._pix_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._pix_item.boundingRect())
        self.fit_to_window()

    def fit_to_window(self):
        if self._pix_item is None:
            return
        self.resetTransform()
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self._emit_zoom()

    def wheelEvent(self, event):
        if self._pix_item is None:
            return
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.scale(factor, factor)
        self._emit_zoom()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._pix_item is not None:
            self._panning = True
            self._last_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.pos() - self._last_pos
            self._last_pos = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _emit_zoom(self):
        scale = self.transform().m11()
        percent = max(1, int(round(scale * 100)))
        self.zoomChanged.emit(percent)


# --- Qt Application ---

class ORFExplorer(QMainWindow):
    def __init__(self, root: str):
        super().__init__()
        self.setWindowTitle("ORF Explorer")
        self.resize(1280, 800)

        self.root = Path(root).resolve()
        self.current_path = self.root
        self.preview_size = "big"
        self.mode = "preview"  # or "table"
        self.current_drive = str(self.root.drive) or str(self.root.anchor)
        self.thumb_pool = QThreadPool.globalInstance()
        self.current_load_token = 0
        self.items_by_path: dict[str, QStandardItem] = {}
        self.thumb_total = 0
        self.thumb_done = 0
        self.openers_config_path = Path(__file__).resolve().parent / "opener_settings.json"
        self.openers = self._load_openers()

        self._setup_ui()
        self._apply_dark_theme()
        self._load_initial()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # --- Left panel: Drive selector + Folder tree ---
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)

        # Drive selector
        drive_row = QHBoxLayout()
        drive_row.addWidget(QLabel("Drive:"))
        self.drive_combo = QComboBox()
        drives = [d.absoluteFilePath() for d in QDir.drives()]
        self.drive_combo.addItems(drives)
        # Pre-select current drive
        current_drive_path = self.current_drive + "\\" if not self.current_drive.endswith("\\") else self.current_drive
        idx = self.drive_combo.findText(current_drive_path, Qt.MatchFixedString)
        if idx >= 0:
            self.drive_combo.setCurrentIndex(idx)
        self.drive_combo.currentTextChanged.connect(self._on_drive_changed)
        drive_row.addWidget(self.drive_combo, 1)
        left_layout.addLayout(drive_row)

        # Folder tree (starts from selected drive root)
        self.tree_model = QFileSystemModel()
        self.tree_model.setFilter(QDir.Dirs | QDir.NoDotAndDotDot)
        self.tree_model.setRootPath(self.drive_combo.currentText())

        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setRootIndex(self.tree_model.index(self.drive_combo.currentText()))
        self.tree.setRootIsDecorated(True)
        self.tree.setItemsExpandable(True)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.setColumnHidden(1, True)
        self.tree.setColumnHidden(2, True)
        self.tree.setColumnHidden(3, True)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(16)
        self.tree.clicked.connect(self._on_folder_selected)
        left_layout.addWidget(self.tree, 1)

        splitter.addWidget(left_panel)

        # --- Right: Main content ---
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 16, 16, 16)
        right_layout.setSpacing(12)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Current folder:"))
        self.path_label = QLabel("/")
        self.path_label.setStyleSheet("color: #93c5fd; font-size: 13px;")
        toolbar.addWidget(self.path_label, 1)

        toolbar.addStretch()

        self.btn_table = QPushButton("Table")
        self.btn_preview = QPushButton("Preview")
        self.btn_table.clicked.connect(lambda: self._set_mode("table"))
        self.btn_preview.clicked.connect(lambda: self._set_mode("preview"))
        toolbar.addWidget(self.btn_table)
        toolbar.addWidget(self.btn_preview)

        toolbar.addWidget(QLabel("Size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItems(["Small", "Big", "Large"])
        self.size_combo.setCurrentText("Big")
        self.size_combo.currentTextChanged.connect(self._on_size_changed)
        toolbar.addWidget(self.size_combo)

        self.btn_open = QPushButton("Open")
        self.btn_open.clicked.connect(self._open_selected_in_external)
        toolbar.addWidget(self.btn_open)
        self.btn_open_settings = QPushButton("Open settings")
        self.btn_open_settings.clicked.connect(self._show_open_settings_dialog)
        toolbar.addWidget(self.btn_open_settings)

        right_layout.addLayout(toolbar)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        right_layout.addWidget(self.progress)

        # Content area
        self.content = QListView()
        self.content.setViewMode(QListView.IconMode)
        self.content.setResizeMode(QListView.Adjust)
        self.content.setMovement(QListView.Static)
        self.content.setSpacing(12)
        self.content.setUniformItemSizes(False)
        self.content.setWordWrap(True)
        self.content.setTextElideMode(Qt.ElideMiddle)
        self.content.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.content.doubleClicked.connect(self._on_item_double_clicked)
        right_layout.addWidget(self.content, 1)

        splitter.addWidget(right)
        splitter.setSizes([320, 960])

        # Status bar hint
        self.statusBar().showMessage("Ready")

    def _apply_dark_theme(self):
        app = QApplication.instance()
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#111827"))
        palette.setColor(QPalette.WindowText, QColor("#e5e7eb"))
        palette.setColor(QPalette.Base, QColor("#0f172a"))
        palette.setColor(QPalette.AlternateBase, QColor("#1f2937"))
        palette.setColor(QPalette.Text, QColor("#f9fafb"))
        palette.setColor(QPalette.Button, QColor("#1f2937"))
        palette.setColor(QPalette.ButtonText, QColor("#f9fafb"))
        palette.setColor(QPalette.Highlight, QColor("#3b82f6"))
        palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        app.setPalette(palette)

        # Extra stylesheet for modern look
        app.setStyleSheet("""
            QTreeView, QListView {
                background: #0f172a;
                border: 1px solid #253044;
                border-radius: 8px;
                padding: 4px;
            }
            QTreeView::item:hover, QListView::item:hover {
                background: #1f2937;
                border-radius: 6px;
            }
            QListView::item {
                color: #e5e7eb;
                padding: 6px;
            }
            QPushButton {
                background: #1f2937;
                border: 1px solid #374151;
                border-radius: 8px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                border-color: #60a5fa;
            }
            QComboBox {
                background: #1f2937;
                border: 1px solid #374151;
                border-radius: 6px;
                padding: 4px 8px;
            }
        """)

    def _load_initial(self):
        self._set_mode("preview")
        self.tree.expandToDepth(1)
        # Select initial folder if it exists on current drive
        init_index = self.tree_model.index(str(self.root))
        if init_index.isValid():
            self.tree.setCurrentIndex(init_index)
            parent = init_index.parent()
            while parent.isValid():
                self.tree.expand(parent)
                parent = parent.parent()
        self._load_files(self.root)

    def _on_drive_changed(self, drive: str):
        """Switch the tree root to the selected drive."""
        if not drive:
            return
        self.current_drive = drive
        self.tree_model.setRootPath(drive)
        new_root_index = self.tree_model.index(drive)
        self.tree.setRootIndex(new_root_index)
        self.tree.expand(new_root_index)
        # Load the drive root in the file list
        self._load_files(Path(drive))

    def _on_folder_selected(self, index):
        path = self.tree_model.filePath(index)
        self._load_files(Path(path))

    def _load_files(self, folder: Path):
        self.current_path = folder
        self.current_load_token += 1
        load_token = self.current_load_token
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("Preparing folder...")
        try:
            rel = str(folder.relative_to(self.root))
            self.path_label.setText(rel or "/")
        except ValueError:
            # Different drive or outside original root
            self.path_label.setText(str(folder))

        model = QStandardItemModel()
        self.content.setModel(model)
        self.items_by_path = {}

        try:
            entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            QMessageBox.warning(self, "Error", "Cannot read folder")
            self.progress.setVisible(False)
            return

        icon_size = self._get_icon_size()
        thumb_targets: list[str] = []

        for entry in entries:
            item = QStandardItem(entry.name)
            item.setData(str(entry), Qt.UserRole)

            if entry.is_dir():
                item.setIcon(QIcon.fromTheme("folder", QIcon(":/qt-project.org/styles/commonstyle/images/diropen-32.png")))
                item.setData("folder", Qt.UserRole + 1)
            else:
                ext = entry.suffix.lower()
                if ext in (".orf", ".nef", ".jpg", ".jpeg"):
                    item.setData("preview", Qt.UserRole + 1)
                    item.setIcon(QIcon.fromTheme("image", QIcon()))
                    thumb_targets.append(str(entry))
                else:
                    item.setData("file", Qt.UserRole + 1)
                    item.setIcon(QIcon.fromTheme("text", QIcon()))

            model.appendRow(item)
            self.items_by_path[str(entry)] = item

        self.content.setIconSize(icon_size)
        self.thumb_total = len(thumb_targets)
        self.thumb_done = 0
        if self.thumb_total == 0:
            self.progress.setVisible(False)
        else:
            self.progress.setVisible(True)
            self.progress.setRange(0, self.thumb_total)
            self.progress.setValue(0)
            self.progress.setFormat("Preparing previews %v/%m")
            for path_str in thumb_targets:
                self._queue_thumbnail(path_str, load_token)

    def _queue_thumbnail(self, path: str, token: int):
        task = ThumbnailTask(path, token)
        task.signals.finished.connect(self._on_thumbnail_ready)
        self.thumb_pool.start(task)

    def _on_thumbnail_ready(self, path: str, data: bytes, token: int):
        if token != self.current_load_token:
            return
        self.thumb_done += 1
        if self.thumb_total > 0:
            self.progress.setValue(min(self.thumb_done, self.thumb_total))
            if self.thumb_done >= self.thumb_total:
                self.progress.setVisible(False)
        item = self.items_by_path.get(path)
        if item is None or not data:
            return
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        pix = pix.scaled(self._get_icon_size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        item.setIcon(QIcon(pix))

    def _get_icon_size(self) -> QSize:
        sizes = {"small": (120, 90), "big": (180, 135), "large": (260, 195)}
        return QSize(*sizes.get(self.preview_size, (180, 135)))

    def _make_thumbnail(self, path: Path) -> Optional[QPixmap]:
        if path.suffix.lower() in (".orf", ".nef"):
            jpeg_bytes = extract_raw_preview(str(path))
            if not jpeg_bytes:
                return None
            pix = QPixmap()
            if pix.loadFromData(jpeg_bytes, "JPG"):
                return pix.scaled(self._get_icon_size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            return None
        else:
            pix = QPixmap(str(path))
            if not pix.isNull():
                return pix.scaled(self._get_icon_size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return None

    def _on_size_changed(self, text: str):
        self.preview_size = text.lower()
        self._load_files(self.current_path)

    def _set_mode(self, mode: str):
        self.mode = mode
        if mode == "table":
            self.content.setViewMode(QListView.ListMode)
            self.content.setIconSize(QSize(24, 24))
            self.content.setGridSize(QSize())
        else:
            self.content.setViewMode(QListView.IconMode)
            icon_size = self._get_icon_size()
            self.content.setIconSize(icon_size)
            # Reserve room for file/folder names under preview icons.
            self.content.setGridSize(QSize(icon_size.width() + 36, icon_size.height() + 54))

    def _on_item_double_clicked(self, index):
        item = self.content.model().itemFromIndex(index)
        if not item:
            return
        path_str = item.data(Qt.UserRole)
        kind = item.data(Qt.UserRole + 1)

        if kind == "folder":
            folder = Path(path_str)
            # select in tree
            idx = self.tree_model.index(path_str)
            self.tree.setCurrentIndex(idx)
            self._load_files(folder)
        elif kind in ("preview", "file"):
            self._show_preview(Path(path_str))

    def _show_preview(self, path: Path):
        dialog = QDialog(self)
        dialog.setWindowTitle(path.name)
        dialog.resize(1100, 800)

        layout = QVBoxLayout(dialog)
        top = QHBoxLayout()
        title = QLabel(path.name)
        top.addWidget(title, 1)
        btn_fit = QPushButton("Fit to window")
        top.addWidget(btn_fit)
        zoom_label = QLabel("100%")
        zoom_label.setMinimumWidth(64)
        zoom_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(zoom_label)
        btn_open = QPushButton("Open")
        btn_open.clicked.connect(lambda: self._open_in_external(path))
        top.addWidget(btn_open)
        layout.addLayout(top)

        view = ZoomPanImageView()
        btn_fit.clicked.connect(view.fit_to_window)
        view.zoomChanged.connect(lambda pct: zoom_label.setText(f"{pct}%"))

        if path.suffix.lower() in (".orf", ".nef"):
            jpeg = extract_raw_preview(str(path))
            if jpeg:
                pix = QPixmap()
                pix.loadFromData(jpeg, "JPG")
                view.set_pixmap(pix)
            else:
                msg = QLabel("No preview available")
                msg.setAlignment(Qt.AlignCenter)
                layout.addWidget(msg, 1)
                btns = QDialogButtonBox(QDialogButtonBox.Close)
                btns.rejected.connect(dialog.reject)
                layout.addWidget(btns)
                dialog.exec()
                return
        else:
            pix = QPixmap(str(path))
            if not pix.isNull():
                view.set_pixmap(pix)
            else:
                msg = QLabel("Cannot load image")
                msg.setAlignment(Qt.AlignCenter)
                layout.addWidget(msg, 1)
                btns = QDialogButtonBox(QDialogButtonBox.Close)
                btns.rejected.connect(dialog.reject)
                layout.addWidget(btns)
                dialog.exec()
                return

        layout.addWidget(view, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dialog.reject)
        layout.addWidget(btns)

        dialog.exec()

    def _default_openers(self) -> dict[str, str]:
        return {
            ".orf": "",
            ".nef": "",
            ".jpg": "",
            ".jpeg": "",
            ".png": "",
            ".tif": "",
            ".tiff": "",
            ".psd": "",
            "*": "",
        }

    def _load_openers(self) -> dict[str, str]:
        openers = self._default_openers()
        if self.openers_config_path.exists():
            try:
                loaded = json.loads(self.openers_config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key, value in loaded.items():
                        if isinstance(key, str) and isinstance(value, str):
                            openers[key.lower()] = value.strip()
            except Exception:
                pass
        return openers

    def _save_openers(self):
        try:
            self.openers_config_path.write_text(
                json.dumps(self.openers, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as err:
            QMessageBox.warning(self, "Open settings", f"Failed to save settings: {err}")

    def _browse_program_for(self, line_edit: QLineEdit):
        chosen, _ = QFileDialog.getOpenFileName(self, "Select program", "", "Executables (*.exe);;All files (*)")
        if chosen:
            line_edit.setText(chosen)

    def _show_open_settings_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Open settings")
        dialog.resize(780, 380)
        layout = QVBoxLayout(dialog)

        info = QLabel("Leave field empty to use default system app for this file type.")
        layout.addWidget(info)

        form = QFormLayout()
        rows: dict[str, QLineEdit] = {}
        labels = [
            (".orf", "ORF files"),
            (".nef", "NEF files"),
            (".jpg", "JPG files"),
            (".jpeg", "JPEG files"),
            (".png", "PNG files"),
            (".tif", "TIF files"),
            (".tiff", "TIFF files"),
            (".psd", "PSD files"),
            ("*", "Other files (fallback)"),
        ]
        for key, title in labels:
            row = QHBoxLayout()
            edit = QLineEdit(self.openers.get(key, ""))
            edit.setPlaceholderText("Path to program (.exe) or leave empty")
            browse = QPushButton("Browse")
            browse.clicked.connect(lambda _=False, e=edit: self._browse_program_for(e))
            row.addWidget(edit, 1)
            row.addWidget(browse)
            wrap = QWidget()
            wrap.setLayout(row)
            form.addRow(title, wrap)
            rows[key] = edit
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        reset_btn = buttons.addButton("Reset to defaults", QDialogButtonBox.ResetRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        def on_reset():
            defaults = self._default_openers()
            for key, edit in rows.items():
                edit.setText(defaults.get(key, ""))
        reset_btn.clicked.connect(on_reset)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return

        for key, edit in rows.items():
            self.openers[key] = edit.text().strip()
        self._save_openers()

    def _program_for_path(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in self.openers and self.openers[ext]:
            return self.openers[ext]
        return self.openers.get("*", "")

    def _open_selected_in_external(self):
        index = self.content.currentIndex()
        if not index.isValid():
            QMessageBox.information(self, "Open", "Select a file first.")
            return
        item = self.content.model().itemFromIndex(index)
        if not item:
            return
        path = Path(item.data(Qt.UserRole))
        if path.is_dir():
            QMessageBox.information(self, "Open", "Select a file (not a folder).")
            return
        self._open_in_external(path)

    def _open_in_external(self, path: Path):
        exe = self._program_for_path(path)
        try:
            if exe:
                if not Path(exe).exists():
                    QMessageBox.warning(self, "Open", f"Program not found:\n{exe}")
                    return
                subprocess.Popen([exe, str(path)])
                return
            os.startfile(str(path))
        except Exception as err:
            QMessageBox.warning(self, "Open", f"Failed to open file:\n{err}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.getcwd(), help="Root folder to browse")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("ORF Explorer")

    window = ORFExplorer(args.root)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
