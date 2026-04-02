"""
FlowCap main window — PyQt6 UI.
"""

import os
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, QSize, QMimeData, QUrl,
)
from PyQt6.QtGui import QPixmap, QDragEnterEvent, QDropEvent, QIcon, QFont
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QTextEdit, QFileDialog,
    QComboBox, QGroupBox, QSizePolicy, QFrame, QMessageBox,
    QApplication,
)

from core.ffmpeg_utils import find_ffmpeg, probe_video, extract_thumbnail
from core.processor import process_video, QUALITY_BALANCED, QUALITY_HIGH


SUPPORTED_FORMATS = "Video Files (*.mp4 *.mov *.mkv *.avi);;All Files (*)"
OUTPUT_FPS = 60.0


# ── Worker thread ────────────────────────────────────────────────────────────

class ConvertWorker(QObject):
    progress = pyqtSignal(int, int)     # current, total
    log = pyqtSignal(str)
    finished = pyqtSignal(str)          # output path
    error = pyqtSignal(str)

    def __init__(self, input_path: str, quality: str):
        super().__init__()
        self.input_path = input_path
        self.quality = quality
        self._cancelled = False
        self._tmpdir: str | None = None

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._do_convert()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if self._tmpdir and os.path.exists(self._tmpdir):
                try:
                    shutil.rmtree(self._tmpdir)
                except Exception:
                    pass

    def _do_convert(self):
        output_path = process_video(
            input_path=self.input_path,
            output_fps=OUTPUT_FPS,
            quality=self.quality,
            progress_callback=lambda cur, tot: self.progress.emit(cur, tot),
            log_callback=self.log.emit,
            cancel_check=lambda: self._cancelled,
        )
        if not self._cancelled:
            self.finished.emit(output_path)


# ── Drop Zone widget ─────────────────────────────────────────────────────────

class DropZone(QLabel):
    file_dropped = pyqtSignal(str)
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(120)
        self._set_idle_text()

    def _set_idle_text(self):
        self.setText("Drop a video here  ·  or click to browse\nMP4  ·  MOV  ·  MKV  ·  AVI")

    def mousePressEvent(self, event):
        self.clicked.emit()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and self._is_supported(urls[0].toLocalFile()):
                event.acceptProposedAction()
                self.setStyleSheet("border-color: #6366f1; color: #9898ff; background-color: #141420;")
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
        self.setFixedWidth(700)

        self._input_path: str | None = None
        self._worker: ConvertWorker | None = None
        self._thread: QThread | None = None

        self._load_stylesheet()
        self._build_ui()
        self._check_ffmpeg()

    # ── Stylesheet ───────────────────────────────────────────────────────

    def _load_stylesheet(self):
        # sys._MEIPASS is set by PyInstaller at runtime; fall back to __file__ in dev
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
        credit = QLabel('<a href="https://www.youtube.com/channel/UCRVR-SzXYsYZN-6KjAGAn3g" style="color:#333; text-decoration:underline; text-decoration-color:#2a2a2a; font-size:11px;">by Swiftal</a>')
        credit.setOpenExternalLinks(True)
        credit.setObjectName("creditLabel")
        subtitle = QLabel("Convert to 60fps  ·  Optical flow")
        subtitle.setObjectName("subtitleLabel")
        header_row.addWidget(title)
        header_row.addWidget(credit)
        header_row.addStretch()
        header_row.addWidget(subtitle)
        header_row.setAlignment(subtitle, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(header_row)
        layout.addSpacing(20)

        # ── FFmpeg warning (hidden by default) ───────────────────────────
        self._ffmpeg_warn = QLabel()
        self._ffmpeg_warn.setObjectName("errorLabel")
        self._ffmpeg_warn.setWordWrap(True)
        self._ffmpeg_warn.hide()
        layout.addWidget(self._ffmpeg_warn)

        # ── Drop zone — clickable, no separate browse button ─────────────
        layout.addSpacing(12)
        self._drop_zone = DropZone()
        self._drop_zone.file_dropped.connect(self._on_file_selected)
        self._drop_zone.clicked.connect(self._browse_file)
        layout.addWidget(self._drop_zone)
        layout.addSpacing(20)

        # ── File preview — hidden until a file is loaded ─────────────────
        self._preview_widget = QWidget()
        preview_col = QVBoxLayout(self._preview_widget)
        preview_col.setSpacing(8)
        preview_col.setContentsMargins(0, 0, 0, 0)

        self._thumb_frame = QFrame()
        self._thumb_frame.setObjectName("thumbnailFrame")
        self._thumb_frame.setFixedSize(200, 112)
        thumb_layout = QVBoxLayout(self._thumb_frame)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        self._thumb_label = QLabel()
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setFixedSize(200, 112)
        self._thumb_label.setText("—")
        self._thumb_label.setStyleSheet("color: #2a2a2a; font-size: 18px;")
        thumb_layout.addWidget(self._thumb_label)

        self._filename_label = QLabel()
        self._filename_label.setObjectName("filenameLabel")
        self._filename_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._info_label = QLabel()
        self._info_label.setObjectName("infoLabel")
        self._info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        preview_col.addWidget(self._thumb_frame, 0, Qt.AlignmentFlag.AlignHCenter)
        preview_col.addWidget(self._filename_label, 0, Qt.AlignmentFlag.AlignHCenter)
        preview_col.addWidget(self._info_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._preview_widget.hide()
        layout.addWidget(self._preview_widget)
        layout.addSpacing(20)

        # ── Quality + Convert on the same row ────────────────────────────
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
        self._convert_btn.clicked.connect(self._start_conversion)
        action_row.addWidget(self._convert_btn)
        layout.addLayout(action_row)
        layout.addSpacing(14)

        # ── Progress bar ─────────────────────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(4)
        layout.addWidget(self._progress_bar)
        layout.addSpacing(6)

        # ── Status + open folder + details toggle ────────────────────────
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
        self._details_btn.setFixedWidth(72)
        self._details_btn.clicked.connect(self._toggle_log)
        status_row.addWidget(self._details_btn)
        layout.addLayout(status_row)
        layout.addSpacing(8)

        # ── Log console — hidden by default, shown via Details toggle ────
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
                "FFmpeg / ffprobe not found in PATH.\n"
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

    # ── File selection ───────────────────────────────────────────────────

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video", "", SUPPORTED_FORMATS
        )
        if path:
            self._on_file_selected(path)

    def _on_file_selected(self, path: str):
        self._input_path = path
        self._done_label.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setValue(0)

        self._status_label.setText("")
        self._log_message(f"Loaded: {path}")
        self._drop_zone.setText(f"✓  {Path(path).name}")
        self._preview_widget.show()
        self.adjustSize()

        # Probe
        try:
            info = probe_video(path)
            self._filename_label.setText(Path(path).name)
            self._info_label.setText(
                f"{info['width']} × {info['height']}  ·  "
                f"{info['fps']:.3f} fps  ·  "
                f"{info['duration']:.1f}s  ·  "
                f"Audio: {'Yes' if info['has_audio'] else 'No'}"
            )
            self._log_message(
                f"  {info['width']}x{info['height']} @ {info['fps']:.3f} fps, "
                f"{info['duration']:.2f}s"
            )
        except Exception as exc:
            self._filename_label.setText(Path(path).name)
            self._info_label.setText(f"Could not probe: {exc}")
            self._log_message(f"Probe error: {exc}")

        # Thumbnail
        self._load_thumbnail(path)

        # Enable convert if ffmpeg available
        ffmpeg, ffprobe = find_ffmpeg()
        if ffmpeg and ffprobe:
            self._convert_btn.setEnabled(True)

    def _load_thumbnail(self, path: str):
        tmp = tempfile.mktemp(suffix=".jpg")
        try:
            ok = extract_thumbnail(path, tmp, time=0.0)
            if ok:
                pix = QPixmap(tmp)
                pix = pix.scaled(
                    200, 112,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._thumb_label.setPixmap(pix)
                self._thumb_label.setText("")
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ── Conversion ───────────────────────────────────────────────────────

    def _start_conversion(self):
        if not self._input_path:
            return

        # Guard: don't start a second conversion if one is still running
        if self._thread is not None and self._thread.isRunning():
            return

        quality = self._quality_combo.currentData()
        self._convert_btn.setEnabled(False)
        self._done_label.hide()
        self._open_folder_btn.hide()
        self._progress_bar.setValue(0)
        self._status_label.setText("Converting…")
        self._log.clear()
        self._log_message("Starting conversion...")

        self._worker = ConvertWorker(self._input_path, quality)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._log_message)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)

        # Cleanup
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
        self._done_label.setText(f"Saved → {Path(output_path).name}")
        self._done_label.show()
        show_label = "Show in Finder" if sys.platform == "darwin" else "Open Folder"
        self._open_folder_btn.setText(show_label)
        self._open_folder_btn.show()
        self._convert_btn.setEnabled(True)
        self._progress_bar.setValue(self._progress_bar.maximum())
        self._status_label.setText("Done")

    def _on_error(self, msg: str):
        self._log_message(f"ERROR: {msg}")
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
        self.adjustSize()

    def _log_message(self, msg: str):
        self._log.append(msg)
        # Scroll to bottom
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, event):
        if self._worker:
            self._worker.cancel()
        super().closeEvent(event)
