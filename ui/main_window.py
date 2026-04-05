"""
FlowCap main window — PyQt6 UI.
"""

import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, QUrl, QTimer,
)
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtGui import QPixmap, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QFileDialog,
    QComboBox, QSizePolicy, QCheckBox, QListWidget,
    QMessageBox, QApplication,
)

from core.ffmpeg_utils import find_ffmpeg, probe_video, extract_thumbnail
from core.processor import process_video, QUALITY_BALANCED, QUALITY_HIGH


SUPPORTED_FORMATS = "Video Files (*.mp4 *.mov *.mkv *.avi);;All Files (*)"
SUPPORTED_EXTS = {".mp4", ".mov", ".mkv", ".avi"}

# Fixed window heights: (queue_visible, log_visible)
_WIN_H = {
    (False, False): 490,
    (True,  False): 590,
    (False, True):  608,
    (True,  True):  708,
}


# ── Worker thread ────────────────────────────────────────────────────────────

class ConvertWorker(QObject):
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        input_path: str,
        output_path: str,
        quality: str,
        output_fps: float,
        detect_scenes: bool,
    ):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.quality = quality
        self.output_fps = output_fps
        self.detect_scenes = detect_scenes
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            output_path = process_video(
                input_path=self.input_path,
                output_path=self.output_path,
                output_fps=self.output_fps,
                quality=self.quality,
                detect_scenes=self.detect_scenes,
                progress_callback=lambda cur, tot: self.progress.emit(cur, tot),
                log_callback=self.log.emit,
                cancel_check=lambda: self._cancelled,
            )
            if not self._cancelled:
                self.finished.emit(output_path)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Log Drawer Handle ────────────────────────────────────────────────────────

class LogDrawerHandle(QWidget):
    """
    Full-width clickable strip at the bottom of the window.
    Shows a centered chevron + 'Details' label — click to expand/collapse the log.
    """
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("logHandle")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(22)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self._chevron = QLabel("▾")
        self._chevron.setObjectName("logHandleChevron")
        self._label = QLabel("Details")
        self._label.setObjectName("logHandleLabel")

        layout.addStretch()
        layout.addWidget(self._chevron)
        layout.addWidget(self._label)
        layout.addStretch()

    def set_open(self, open: bool):
        self._chevron.setText("▲" if open else "▾")

    def mousePressEvent(self, event):
        self.clicked.emit()


# ── Output Folder Picker ─────────────────────────────────────────────────────

class OutputFolderPicker(QWidget):
    """
    Pill-shaped clickable widget that shows the current output folder.
    The whole pill is the button — no separate Change button needed.
    """
    clicked = pyqtSignal()

    _DEFAULT = "Same folder as input"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("outputPicker")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 10, 0)
        layout.setSpacing(8)

        self._folder_icon = QLabel("⌂")
        self._folder_icon.setObjectName("pickerIcon")
        self._folder_icon.setFixedWidth(14)

        self._path_lbl = QLabel(self._DEFAULT)
        self._path_lbl.setObjectName("pickerPath")
        self._path_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._edit_hint = QLabel("change")
        self._edit_hint.setObjectName("pickerHint")

        layout.addWidget(self._folder_icon)
        layout.addWidget(self._path_lbl)
        layout.addWidget(self._edit_hint)

    def set_path(self, folder: str | None):
        if folder:
            self._path_lbl.setText(Path(folder).name)
            self._path_lbl.setToolTip(folder)
        else:
            self._path_lbl.setText(self._DEFAULT)
            self._path_lbl.setToolTip("")

    def mousePressEvent(self, event):
        self.clicked.emit()


# ── Drop Zone ────────────────────────────────────────────────────────────────

class DropZone(QWidget):
    files_dropped = pyqtSignal(list)   # list[str]
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(120)

        # ── Idle state ───────────────────────────────────────────────────
        self._idle_label = QLabel(
            "Drop a video here  ·  or click to browse\nMP4  ·  MOV  ·  MKV  ·  AVI"
        )
        self._idle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_label.setStyleSheet(
            "color: #888; font-size: 13px; background: transparent; border: none;"
        )
        self._idle_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # ── Loaded state ─────────────────────────────────────────────────
        self._thumb_lbl = QLabel()
        self._thumb_lbl.setFixedSize(160, 90)
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_lbl.setObjectName("thumbnailFrame")
        self._thumb_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._file_name_lbl = QLabel()
        self._file_name_lbl.setObjectName("filenameLabel")
        self._file_name_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._file_info_lbl = QLabel()
        self._file_info_lbl.setObjectName("infoLabel")
        self._file_info_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._change_hint = QLabel("Click to change file")
        self._change_hint.setObjectName("infoLabel")
        self._change_hint.setStyleSheet("background: transparent; border: none;")
        self._change_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        info_col = QWidget()
        info_col.setStyleSheet("background: transparent;")
        info_col.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        info_col_layout = QVBoxLayout(info_col)
        info_col_layout.setSpacing(6)
        info_col_layout.setContentsMargins(0, 0, 0, 0)
        info_col_layout.addWidget(self._file_name_lbl)
        info_col_layout.addWidget(self._file_info_lbl)
        info_col_layout.addSpacing(8)
        info_col_layout.addWidget(self._change_hint)

        self._loaded_row = QWidget()
        self._loaded_row.setStyleSheet("background: transparent;")
        self._loaded_row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        loaded_layout = QHBoxLayout(self._loaded_row)
        loaded_layout.setSpacing(20)
        loaded_layout.setContentsMargins(0, 0, 0, 0)
        loaded_layout.addWidget(self._thumb_lbl)
        loaded_layout.addWidget(info_col, 1)
        self._loaded_row.hide()

        main = QVBoxLayout(self)
        main.setContentsMargins(20, 16, 20, 16)
        main.addWidget(self._idle_label, 0, Qt.AlignmentFlag.AlignCenter)
        main.addWidget(self._loaded_row, 0, Qt.AlignmentFlag.AlignCenter)

    # ── State ────────────────────────────────────────────────────────────

    def set_idle(self):
        self._loaded_row.hide()
        self._idle_label.show()
        self.setStyleSheet("")

    def set_loaded(self, name: str, info: str):
        self._file_name_lbl.setText(name)
        self._file_info_lbl.setText(info)
        self._idle_label.hide()
        self._loaded_row.show()

    def set_thumbnail(self, pixmap: QPixmap):
        self._thumb_lbl.setPixmap(
            pixmap.scaled(
                160, 90,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_success(self):
        pass  # no extra colour change — done label is sufficient signal

    # ── Events ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        self.clicked.emit()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            supported = [
                u.toLocalFile() for u in event.mimeData().urls()
                if Path(u.toLocalFile()).suffix.lower() in SUPPORTED_EXTS
            ]
            if supported:
                event.acceptProposedAction()
                self.setStyleSheet(
                    "#dropZone { border-color: #6366f1; color: #9898ff;"
                    " background-color: #141420; }"
                )
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        paths = [
            u.toLocalFile() for u in event.mimeData().urls()
            if Path(u.toLocalFile()).suffix.lower() in SUPPORTED_EXTS
        ]
        if paths:
            self.files_dropped.emit(paths)


# ── Main Window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FlowCap")

        self._input_path: str | None = None
        self._output_path: str | None = None
        self._output_dir: str | None = None
        self._worker: ConvertWorker | None = None
        self._thread: QThread | None = None
        self._converting: bool = False
        self._post_convert: bool = False
        self._queue: list[str] = []
        self._convert_start_time: float = 0.0

        self._load_stylesheet()
        self._build_ui()
        self.setFixedSize(700, _WIN_H[(False, False)])
        self._check_ffmpeg()

    # ── Stylesheet ───────────────────────────────────────────────────────

    def _load_stylesheet(self):
        import sys as _sys
        base = Path(getattr(_sys, "_MEIPASS", Path(__file__).parent.parent))
        qss_path = base / "ui" / "styles.qss"
        if not qss_path.exists():
            qss_path = Path(__file__).parent / "styles.qss"
        if qss_path.exists():
            with open(qss_path) as f:
                self.setStyleSheet(f.read())

    # ── UI Construction ──────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        # Outer layout — zero margins so the log handle can go edge-to-edge
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Inner content with normal side margins
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 26, 32, 0)
        layout.setSpacing(0)
        outer.addWidget(content)

        # ── Header ───────────────────────────────────────────────────────
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        title = QLabel("FlowCap")
        title.setObjectName("titleLabel")
        credit = QLabel(
            '<a href="https://www.youtube.com/channel/UCRVR-SzXYsYZN-6KjAGAn3g" '
            'style="color:#333; text-decoration:underline; text-decoration-color:#2a2a2a;'
            ' font-size:11px;">by Swiftal</a>'
        )
        credit.setOpenExternalLinks(True)
        credit.setObjectName("creditLabel")
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(credit)
        header_row.setAlignment(credit, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header_row)
        layout.addSpacing(24)

        # ── FFmpeg warning ───────────────────────────────────────────────
        self._ffmpeg_warn = QLabel()
        self._ffmpeg_warn.setObjectName("errorLabel")
        self._ffmpeg_warn.setWordWrap(True)
        self._ffmpeg_warn.hide()
        layout.addWidget(self._ffmpeg_warn)

        # ── Drop zone ────────────────────────────────────────────────────
        self._drop_zone = DropZone()
        self._drop_zone.files_dropped.connect(self._on_files_dropped)
        self._drop_zone.clicked.connect(self._browse_file)
        layout.addWidget(self._drop_zone)
        layout.addSpacing(20)

        # ── Queue list (hidden when empty) ───────────────────────────────
        queue_header = QHBoxLayout()
        self._queue_label = QLabel("Queue")
        self._queue_label.setObjectName("sectionLabel")
        self._clear_queue_btn = QPushButton("Clear")
        self._clear_queue_btn.setFixedWidth(52)
        self._clear_queue_btn.clicked.connect(self._clear_queue)
        queue_header.addWidget(self._queue_label)
        queue_header.addStretch()
        queue_header.addWidget(self._clear_queue_btn)

        self._queue_header_widget = QWidget()
        self._queue_header_widget.setStyleSheet("background: transparent;")
        self._queue_header_widget.setLayout(queue_header)
        self._queue_header_widget.hide()
        layout.addWidget(self._queue_header_widget)

        self._queue_list = QListWidget()
        self._queue_list.setObjectName("queueList")
        self._queue_list.setFixedHeight(72)
        self._queue_list.hide()
        layout.addWidget(self._queue_list)
        layout.addSpacing(10)

        # ── Output folder picker ──────────────────────────────────────────
        self._output_picker = OutputFolderPicker()
        self._output_picker.clicked.connect(self._browse_output_dir)
        layout.addWidget(self._output_picker)
        layout.addSpacing(18)

        # ── Options row ───────────────────────────────────────────────────
        options_row = QHBoxLayout()
        options_row.setSpacing(10)

        quality_label = QLabel("Quality")
        quality_label.setObjectName("sectionLabel")
        self._quality_combo = QComboBox()
        self._quality_combo.addItem("Balanced  (2 passes)", QUALITY_BALANCED)
        self._quality_combo.addItem("High Quality  (3 passes)", QUALITY_HIGH)
        self._quality_combo.setFixedWidth(170)

        fps_label = QLabel("FPS")
        fps_label.setObjectName("sectionLabel")
        self._fps_combo = QComboBox()
        for fps_val in (30, 60, 120, 240):
            self._fps_combo.addItem(f"{fps_val}", float(fps_val))
        self._fps_combo.setCurrentIndex(1)  # default 60
        self._fps_combo.setFixedWidth(72)
        self._fps_combo.currentIndexChanged.connect(self._on_fps_changed)

        self._scene_check = QCheckBox("Scene cuts")
        self._scene_check.setObjectName("sceneCheck")
        self._scene_check.setToolTip(
            "Detect hard scene cuts and avoid interpolating across them"
        )

        options_row.addWidget(quality_label)
        options_row.addWidget(self._quality_combo)
        options_row.addSpacing(8)
        options_row.addWidget(fps_label)
        options_row.addWidget(self._fps_combo)
        options_row.addSpacing(8)
        options_row.addWidget(self._scene_check)
        options_row.addStretch()
        layout.addLayout(options_row)
        layout.addSpacing(16)

        # ── Convert button ────────────────────────────────────────────────
        self._convert_btn = QPushButton("Convert to 60fps")
        self._convert_btn.setObjectName("convertBtn")
        self._convert_btn.setEnabled(False)
        self._convert_btn.clicked.connect(self._on_convert_clicked)
        layout.addWidget(self._convert_btn)
        layout.addSpacing(16)

        # ── Progress bar ──────────────────────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(4)
        layout.addWidget(self._progress_bar)
        layout.addSpacing(10)

        # ── Status row ────────────────────────────────────────────────────
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._status_label = QLabel("")
        self._status_label.setObjectName("sectionLabel")
        status_row.addWidget(self._status_label)
        status_row.addStretch()

        self._done_label = QLabel()
        self._done_label.setObjectName("doneLabel")
        self._done_label.hide()
        status_row.addWidget(self._done_label)

        self._preview_btn = QPushButton("Preview")
        self._preview_btn.setObjectName("openFolderBtn")
        self._preview_btn.hide()
        self._preview_btn.clicked.connect(self._show_preview)
        status_row.addWidget(self._preview_btn)

        self._open_folder_btn = QPushButton("Show in Finder")
        self._open_folder_btn.setObjectName("openFolderBtn")
        self._open_folder_btn.hide()
        self._open_folder_btn.clicked.connect(self._open_output_folder)
        status_row.addWidget(self._open_folder_btn)

        layout.addLayout(status_row)
        layout.addSpacing(12)

        # ── Log drawer handle + console (edge-to-edge, in outer layout) ──
        self._log_handle = LogDrawerHandle()
        self._log_handle.clicked.connect(self._toggle_log)
        outer.addWidget(self._log_handle)

        self._log = QTextEdit()
        self._log.setObjectName("logConsole")
        self._log.setReadOnly(True)
        self._log.setFixedHeight(110)
        self._log.hide()
        outer.addWidget(self._log)

    # ── FFmpeg check ─────────────────────────────────────────────────────

    def _check_ffmpeg(self):
        ffmpeg, ffprobe = find_ffmpeg()
        if not ffmpeg or not ffprobe:
            msg = (
                "FFmpeg / ffprobe not found.\n"
                "Install via: brew install ffmpeg  (macOS)  |  "
                "winget install ffmpeg  (Windows)  |  "
                "apt install ffmpeg  (Ubuntu)\n"
                "Then restart FlowCap."
            )
            self._ffmpeg_warn.setText(msg)
            self._ffmpeg_warn.show()
            self._convert_btn.setEnabled(False)
            self._log_message("ERROR: FFmpeg not found. Install it and restart.")
        else:
            self._log_message(f"FFmpeg found: {ffmpeg}")
            self._log_message(f"ffprobe found: {ffprobe}")

    # ── FPS selection ─────────────────────────────────────────────────────

    def _selected_fps(self) -> float:
        return float(self._fps_combo.currentData())

    def _on_fps_changed(self):
        if not self._converting and not self._post_convert:
            self._set_convert_btn(
                f"Convert to {int(self._selected_fps())}fps",
                enabled=self._input_path is not None,
            )

    # ── File selection ────────────────────────────────────────────────────

    def _browse_file(self):
        if self._converting:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Video(s)", "", SUPPORTED_FORMATS
        )
        if paths:
            self._on_files_dropped(paths)

    def _on_files_dropped(self, paths: list[str]):
        if not paths:
            return
        if self._converting:
            # Add all to queue
            for p in paths:
                if p not in self._queue:
                    self._queue.append(p)
            self._update_queue_display()
            return

        # Set first as current file
        self._on_file_selected(paths[0])

        # Rest go to queue
        for p in paths[1:]:
            if p not in self._queue:
                self._queue.append(p)
        self._update_queue_display()

    def _on_file_selected(self, path: str):
        self._input_path = path
        self._post_convert = False
        self._done_label.hide()
        self._preview_btn.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setValue(0)
        self._status_label.setText("")
        self._set_convert_btn(f"Convert to {int(self._selected_fps())}fps", enabled=False)

        name = Path(path).name
        self._drop_zone.set_loaded(name, "Probing…")
        self._log_message(f"Loaded: {path}")

        try:
            info = probe_video(path)
            info_str = (
                f"{info['width']} × {info['height']}  ·  "
                f"{info['fps']:.3f} fps  ·  "
                f"{info['duration']:.1f}s  ·  "
                f"Audio: {'Yes' if info['has_audio'] else 'No'}"
            )
            self._drop_zone.set_loaded(name, info_str)
            self._log_message(
                f"  {info['width']}x{info['height']} @ {info['fps']:.3f} fps, "
                f"{info['duration']:.2f}s"
            )
        except Exception as exc:
            self._drop_zone.set_loaded(name, f"Could not probe: {exc}")
            self._log_message(f"Probe error: {exc}")

        self._load_thumbnail(path)

        ffmpeg, ffprobe = find_ffmpeg()
        if ffmpeg and ffprobe:
            self._convert_btn.setEnabled(True)

    def _load_thumbnail(self, path: str):
        tmp = tempfile.mktemp(suffix=".jpg")
        try:
            ok = extract_thumbnail(path, tmp, time=0.0)
            if ok:
                pix = QPixmap(tmp)
                if not pix.isNull():
                    self._drop_zone.set_thumbnail(pix)
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _browse_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose Output Folder", "")
        if folder:
            self._output_dir = folder
        else:
            self._output_dir = None
        self._output_picker.set_path(self._output_dir)

    def _get_output_path(self) -> str:
        p = Path(self._input_path)
        stem = f"{p.stem}_flowcap.mp4"
        if self._output_dir:
            return str(Path(self._output_dir) / stem)
        return str(p.parent / stem)

    # ── Queue ─────────────────────────────────────────────────────────────

    def _update_queue_display(self):
        has_queue = bool(self._queue)
        self._queue_list.setVisible(has_queue)
        self._queue_header_widget.setVisible(has_queue)
        if has_queue:
            self._queue_label.setText(f"Queue  ({len(self._queue)} file{'s' if len(self._queue) != 1 else ''})")
            self._queue_list.clear()
            for p in self._queue:
                self._queue_list.addItem(Path(p).name)
        self._update_size()

    def _clear_queue(self):
        self._queue.clear()
        self._update_queue_display()

    def _start_next_in_queue(self):
        if self._queue:
            next_file = self._queue.pop(0)
            self._update_queue_display()
            self._on_file_selected(next_file)
            QTimer.singleShot(200, self._start_conversion)

    # ── Conversion ───────────────────────────────────────────────────────

    def _on_convert_clicked(self):
        if self._converting:
            self._cancel_conversion()
        elif self._post_convert:
            self._reset()
        else:
            self._start_conversion()

    def _cancel_conversion(self):
        if self._worker:
            self._worker.cancel()
        self._converting = False
        self._set_convert_btn(f"Convert to {int(self._selected_fps())}fps")
        self._status_label.setText("Cancelled")

    def _reset(self):
        self._input_path = None
        self._post_convert = False
        self._drop_zone.set_idle()
        self._done_label.hide()
        self._preview_btn.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setValue(0)
        self._status_label.setText("")
        self._set_convert_btn(f"Convert to {int(self._selected_fps())}fps", enabled=False)

    def _start_conversion(self):
        if not self._input_path:
            return
        if self._thread is not None and self._thread.isRunning():
            return

        quality = self._quality_combo.currentData()
        output_fps = self._selected_fps()
        detect_scenes = self._scene_check.isChecked()
        output_path = self._get_output_path()

        self._converting = True
        self._post_convert = False
        self._convert_btn.setText("Cancel")
        self._convert_btn.setObjectName("cancelBtn")
        self._convert_btn.setStyle(self._convert_btn.style())
        self._convert_btn.setEnabled(True)
        self._done_label.hide()
        self._preview_btn.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setMaximum(0)   # indeterminate / pulsing
        self._progress_bar.setValue(0)
        self._status_label.setText("Starting…")
        self._log.clear()
        self._log_message("Starting conversion...")
        self._convert_start_time = time.time()

        self._worker = ConvertWorker(
            self._input_path, output_path, quality, output_fps, detect_scenes
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._log_message)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)

        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(lambda: setattr(self, "_thread", None))
        self._thread.finished.connect(self._on_thread_done)

        self._thread.start()

    def _on_progress(self, current: int, total: int):
        # Switch from indeterminate to determinate on first real frame
        if self._progress_bar.maximum() == 0:
            self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        pct = int(100 * current / total) if total else 0

        elapsed = time.time() - self._convert_start_time
        if current > 0 and elapsed > 3:
            rate = current / elapsed
            remaining_sec = (total - current) / rate
            eta = self._format_eta(remaining_sec)
            self._status_label.setText(f"Converting…  {pct}%  ·  {eta} left")
        else:
            self._status_label.setText(f"Converting…  {pct}%")

    def _format_eta(self, seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"

    def _on_done(self, output_path: str):
        self._output_path = output_path
        self._converting = False
        self._post_convert = True
        self._done_label.setText(f"Saved → {Path(output_path).name}")
        self._done_label.show()

        show_label = "Show in Finder" if sys.platform == "darwin" else "Open Folder"
        self._open_folder_btn.setText(show_label)
        self._open_folder_btn.show()
        self._preview_btn.show()

        self._progress_bar.setValue(self._progress_bar.maximum())
        self._status_label.setText("Done")
        self._set_convert_btn("Convert another")
        self._drop_zone.set_success()

    def _on_thread_done(self):
        """Called once the conversion thread has fully exited. Safe to start next."""
        if self._queue and self._post_convert:
            QTimer.singleShot(300, self._start_next_in_queue)

    def _on_error(self, msg: str):
        self._converting = False
        self._log_message(f"ERROR: {msg}")
        self._set_convert_btn(f"Convert to {int(self._selected_fps())}fps")
        QMessageBox.critical(self, "Conversion Error", msg)

    # ── Output actions ────────────────────────────────────────────────────

    def _open_output_folder(self):
        if not self._output_path:
            return
        folder = str(Path(self._output_path).parent)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _show_preview(self):
        if not self._input_path or not self._output_path:
            return
        from ui.preview_dialog import PreviewDialog
        dlg = PreviewDialog(self, self._input_path, self._output_path)
        dlg.exec()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _set_convert_btn(self, text: str, enabled: bool = True):
        """Set convert button text and restore its blue convertBtn style."""
        self._convert_btn.setObjectName("convertBtn")
        self._convert_btn.setStyle(self._convert_btn.style())
        self._convert_btn.setText(text)
        self._convert_btn.setEnabled(enabled)

    def _toggle_log(self):
        visible = not self._log.isVisible()
        self._log.setVisible(visible)
        self._log_handle.set_open(visible)
        self._update_size()

    def _update_size(self):
        key = (bool(self._queue), self._log.isVisible())
        self.setFixedSize(700, _WIN_H[key])

    def _log_message(self, msg: str):
        self._log.append(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, event):
        if self._worker:
            self._worker.cancel()
        super().closeEvent(event)
