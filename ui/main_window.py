"""
FlowCap main window — PyQt6 UI.
"""

import os
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, QUrl,
)
from PyQt6.QtGui import QPixmap, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QFileDialog,
    QComboBox, QSizePolicy, QFrame, QMessageBox,
    QApplication,
)

from core.ffmpeg_utils import find_ffmpeg, probe_video, extract_thumbnail
from core.processor import process_video, QUALITY_BALANCED, QUALITY_HIGH


SUPPORTED_FORMATS = "Video Files (*.mp4 *.mov *.mkv *.avi);;All Files (*)"
OUTPUT_FPS = 60.0

# Fixed heights: keyed by log visibility only (preview is inside drop zone now)
_WIN_H = {
    False: 430,   # log hidden
    True:  548,   # log visible
}


# ── Worker thread ────────────────────────────────────────────────────────────

class ConvertWorker(QObject):
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, input_path: str, output_path: str, quality: str):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.quality = quality
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            output_path = process_video(
                input_path=self.input_path,
                output_path=self.output_path,
                output_fps=OUTPUT_FPS,
                quality=self.quality,
                progress_callback=lambda cur, tot: self.progress.emit(cur, tot),
                log_callback=self.log.emit,
                cancel_check=lambda: self._cancelled,
            )
            if not self._cancelled:
                self.finished.emit(output_path)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Drop Zone ────────────────────────────────────────────────────────────────

class DropZone(QWidget):
    file_dropped = pyqtSignal(str)
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(120)

        # ── Idle state ───────────────────────────────────────────────────
        self._idle_label = QLabel(
            "Drop a video here  ·  or click to browse\nMP4  ·  MOV  ·  MKV  ·  AVI"
        )
        self._idle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        self._change_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        info_col = QWidget()
        info_col.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        info_col_layout = QVBoxLayout(info_col)
        info_col_layout.setSpacing(6)
        info_col_layout.setContentsMargins(0, 0, 0, 0)
        info_col_layout.addWidget(self._file_name_lbl)
        info_col_layout.addWidget(self._file_info_lbl)
        info_col_layout.addSpacing(8)
        info_col_layout.addWidget(self._change_hint)

        self._loaded_row = QWidget()
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
            pixmap.scaled(160, 90,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        )

    def set_success(self):
        self.setStyleSheet("#dropZone { border-color: #34d399; }")

    # ── Events ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        self.clicked.emit()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and self._is_supported(urls[0].toLocalFile()):
                event.acceptProposedAction()
                self.setStyleSheet(
                    "#dropZone { border-color: #6366f1; color: #9898ff; background-color: #141420; }"
                )
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if self._is_supported(path):
                self.file_dropped.emit(path)

    @staticmethod
    def _is_supported(path: str) -> bool:
        return Path(path).suffix.lower() in {".mp4", ".mov", ".mkv", ".avi"}


# ── Main Window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FlowCap")

        self._input_path: str | None = None
        self._output_dir: str | None = None
        self._worker: ConvertWorker | None = None
        self._thread: QThread | None = None
        self._converting: bool = False
        self._post_convert: bool = False

        self._load_stylesheet()
        self._build_ui()
        self.setFixedSize(700, _WIN_H[False])
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
        layout = QVBoxLayout(root)
        layout.setContentsMargins(32, 26, 32, 20)
        layout.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        title = QLabel("FlowCap")
        title.setObjectName("titleLabel")
        credit = QLabel(
            '<a href="https://www.youtube.com/channel/UCRVR-SzXYsYZN-6KjAGAn3g" '
            'style="color:#333; text-decoration:underline; text-decoration-color:#2a2a2a; font-size:11px;">'
            'by Swiftal</a>'
        )
        credit.setOpenExternalLinks(True)
        credit.setObjectName("creditLabel")
        subtitle = QLabel("Convert to 60fps  ·  Optical flow")
        subtitle.setObjectName("subtitleLabel")
        header_row.addWidget(title)
        header_row.addWidget(credit)
        header_row.addStretch()
        header_row.addWidget(subtitle)
        header_row.setAlignment(subtitle, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header_row)
        layout.addSpacing(20)

        # ── FFmpeg warning ───────────────────────────────────────────────
        self._ffmpeg_warn = QLabel()
        self._ffmpeg_warn.setObjectName("errorLabel")
        self._ffmpeg_warn.setWordWrap(True)
        self._ffmpeg_warn.hide()
        layout.addWidget(self._ffmpeg_warn)

        # ── Drop zone (contains preview when loaded) ──────────────────────
        layout.addSpacing(12)
        self._drop_zone = DropZone()
        self._drop_zone.file_dropped.connect(self._on_file_selected)
        self._drop_zone.clicked.connect(self._browse_file)
        layout.addWidget(self._drop_zone)
        layout.addSpacing(16)

        # ── Output folder row ─────────────────────────────────────────────
        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        output_lbl = QLabel("Save to")
        output_lbl.setObjectName("sectionLabel")
        self._output_dir_label = QLabel("Same folder as input")
        self._output_dir_label.setObjectName("infoLabel")
        self._output_dir_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._change_output_btn = QPushButton("Change")
        self._change_output_btn.setFixedWidth(80)
        self._change_output_btn.clicked.connect(self._browse_output_dir)
        output_row.addWidget(output_lbl)
        output_row.addWidget(self._output_dir_label)
        output_row.addWidget(self._change_output_btn)
        layout.addLayout(output_row)
        layout.addSpacing(12)

        # ── Quality + Convert ─────────────────────────────────────────────
        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        quality_label = QLabel("Quality")
        quality_label.setObjectName("sectionLabel")
        self._quality_combo = QComboBox()
        self._quality_combo.addItem("Balanced  (4× blend, faster)", QUALITY_BALANCED)
        self._quality_combo.addItem("High Quality  (8× blend, slower)", QUALITY_HIGH)
        action_row.addWidget(quality_label)
        action_row.addWidget(self._quality_combo)
        self._convert_btn = QPushButton("Convert to 60fps")
        self._convert_btn.setObjectName("convertBtn")
        self._convert_btn.setEnabled(False)
        self._convert_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._convert_btn.clicked.connect(self._on_convert_clicked)
        action_row.addWidget(self._convert_btn)
        layout.addLayout(action_row)
        layout.addSpacing(14)

        # ── Progress bar ──────────────────────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(4)
        layout.addWidget(self._progress_bar)
        layout.addSpacing(6)

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
        self._open_folder_btn = QPushButton("Show in Finder")
        self._open_folder_btn.setObjectName("openFolderBtn")
        self._open_folder_btn.hide()
        self._open_folder_btn.clicked.connect(self._open_output_folder)
        status_row.addWidget(self._open_folder_btn)
        self._details_btn = QPushButton("Details ▾")
        self._details_btn.setFixedWidth(82)
        self._details_btn.clicked.connect(self._toggle_log)
        status_row.addWidget(self._details_btn)
        layout.addLayout(status_row)
        layout.addSpacing(8)

        # ── Log console ───────────────────────────────────────────────────
        self._log = QTextEdit()
        self._log.setObjectName("logConsole")
        self._log.setReadOnly(True)
        self._log.setFixedHeight(110)
        self._log.hide()
        layout.addWidget(self._log)

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

    # ── File selection ────────────────────────────────────────────────────

    def _browse_file(self):
        if self._converting:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", SUPPORTED_FORMATS)
        if path:
            self._on_file_selected(path)

    def _on_file_selected(self, path: str):
        if self._converting:
            return
        self._input_path = path
        self._post_convert = False
        self._done_label.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setValue(0)
        self._status_label.setText("")
        self._convert_btn.setText("Convert to 60fps")

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
            self._output_dir_label.setText(Path(folder).name)
            self._output_dir_label.setToolTip(folder)

    def _get_output_path(self) -> str:
        p = Path(self._input_path)
        stem = f"{p.stem}_flowcap.mp4"
        if self._output_dir:
            return str(Path(self._output_dir) / stem)
        return str(p.parent / stem)

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
        self._convert_btn.setText("Convert to 60fps")
        self._convert_btn.setEnabled(True)
        self._status_label.setText("Cancelled")

    def _reset(self):
        self._input_path = None
        self._post_convert = False
        self._drop_zone.set_idle()
        self._done_label.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setValue(0)
        self._status_label.setText("")
        self._convert_btn.setText("Convert to 60fps")
        self._convert_btn.setEnabled(False)

    def _start_conversion(self):
        if not self._input_path:
            return
        if self._thread is not None and self._thread.isRunning():
            return

        quality = self._quality_combo.currentData()
        output_path = self._get_output_path()
        self._converting = True
        self._post_convert = False
        self._convert_btn.setText("Cancel")
        self._convert_btn.setEnabled(True)
        self._done_label.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setValue(0)
        self._status_label.setText("Converting…")
        self._log.clear()
        self._log_message("Starting conversion...")

        self._worker = ConvertWorker(self._input_path, output_path, quality)
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

        self._thread.start()

    def _on_progress(self, current: int, total: int):
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        pct = int(100 * current / total) if total else 0
        self._status_label.setText(f"Converting…  {pct}%")

    def _on_done(self, output_path: str):
        self._output_path = output_path
        self._converting = False
        self._post_convert = True
        self._done_label.setText(f"Saved → {Path(output_path).name}")
        self._done_label.show()
        show_label = "Show in Finder" if sys.platform == "darwin" else "Open Folder"
        self._open_folder_btn.setText(show_label)
        self._open_folder_btn.show()
        self._progress_bar.setValue(self._progress_bar.maximum())
        self._status_label.setText("Done")
        self._convert_btn.setText("Convert another")
        self._convert_btn.setEnabled(True)
        self._drop_zone.set_success()

    def _on_error(self, msg: str):
        self._converting = False
        self._log_message(f"ERROR: {msg}")
        self._convert_btn.setText("Convert to 60fps")
        self._convert_btn.setEnabled(True)
        QMessageBox.critical(self, "Conversion Error", msg)

    def _open_output_folder(self):
        if hasattr(self, "_output_path"):
            folder = str(Path(self._output_path).parent)
            if sys.platform == "darwin":
                os.system(f'open "{folder}"')
            elif sys.platform == "win32":
                os.startfile(folder)
            else:
                os.system(f'xdg-open "{folder}"')

    # ── Helpers ──────────────────────────────────────────────────────────

    def _toggle_log(self):
        if self._log.isVisible():
            self._log.hide()
            self._details_btn.setText("Details ▾")
        else:
            self._log.show()
            self._details_btn.setText("Details ▲")
        self._update_size()

    def _update_size(self):
        self.setFixedSize(700, _WIN_H[self._log.isVisible()])

    def _log_message(self, msg: str):
        self._log.append(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, event):
        if self._worker:
            self._worker.cancel()
        super().closeEvent(event)
