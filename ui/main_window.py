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

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(160)
        self._set_idle_text()

    def _set_idle_text(self):
        self.setText(
            "Drop a video here\n"
            "or click Browse to select a file\n\n"
            "Supported: MP4, MOV, MKV, AVI"
        )

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and self._is_supported(urls[0].toLocalFile()):
                event.acceptProposedAction()
                self.setStyleSheet(
                    "border-color: #a29bfe; color: #a29bfe;"
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
        self.setFixedSize(720, 640)

        self._input_path: str | None = None
        self._worker: ConvertWorker | None = None
        self._thread: QThread | None = None

        self._load_stylesheet()
        self._build_ui()
        self._check_ffmpeg()

    # ── Stylesheet ───────────────────────────────────────────────────────

    def _load_stylesheet(self):
        qss_path = Path(__file__).parent / "styles.qss"
        if qss_path.exists():
            with open(qss_path) as f:
                self.setStyleSheet(f.read())

    # ── UI Construction ──────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 22, 28, 16)
        layout.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────
        header_row = QHBoxLayout()
        title = QLabel("FlowCap")
        title.setObjectName("titleLabel")
        subtitle = QLabel("High-framerate → 60fps  ·  Optical flow")
        subtitle.setObjectName("subtitleLabel")
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(subtitle)
        header_row.setAlignment(subtitle, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(header_row)
        layout.addSpacing(18)

        # ── FFmpeg warning (hidden by default) ───────────────────────────
        self._ffmpeg_warn = QLabel()
        self._ffmpeg_warn.setObjectName("errorLabel")
        self._ffmpeg_warn.setWordWrap(True)
        self._ffmpeg_warn.hide()
        layout.addWidget(self._ffmpeg_warn)

        # ── Drop zone + browse ───────────────────────────────────────────
        self._drop_zone = DropZone()
        self._drop_zone.file_dropped.connect(self._on_file_selected)
        layout.addWidget(self._drop_zone)
        layout.addSpacing(8)

        browse_row = QHBoxLayout()
        browse_btn = QPushButton("Browse file…")
        browse_btn.setFixedWidth(140)
        browse_btn.clicked.connect(self._browse_file)
        browse_row.addWidget(browse_btn)
        browse_row.addStretch()
        layout.addLayout(browse_row)
        layout.addSpacing(16)

        # ── Preview + info ───────────────────────────────────────────────
        preview_row = QHBoxLayout()
        preview_row.setSpacing(16)

        self._thumb_frame = QFrame()
        self._thumb_frame.setObjectName("thumbnailFrame")
        self._thumb_frame.setFixedSize(192, 108)
        thumb_layout = QVBoxLayout(self._thumb_frame)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        self._thumb_label = QLabel()
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setFixedSize(192, 108)
        self._thumb_label.setText("—")
        self._thumb_label.setStyleSheet("color: #2a2a2a; font-size: 18px;")
        thumb_layout.addWidget(self._thumb_label)
        preview_row.addWidget(self._thumb_frame)

        self._info_label = QLabel("No file loaded")
        self._info_label.setObjectName("infoLabel")
        self._info_label.setWordWrap(True)
        self._info_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._info_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        preview_row.addWidget(self._info_label)
        layout.addLayout(preview_row)
        layout.addSpacing(20)

        # ── Settings + convert row ───────────────────────────────────────
        action_row = QHBoxLayout()
        action_row.setSpacing(10)

        quality_label = QLabel("Quality")
        quality_label.setStyleSheet("color: #444; font-size: 11px;")
        self._quality_combo = QComboBox()
        self._quality_combo.addItem("Balanced  (4× blend, faster)", QUALITY_BALANCED)
        self._quality_combo.addItem("High Quality  (8× blend, slower)", QUALITY_HIGH)
        action_row.addWidget(quality_label)
        action_row.addWidget(self._quality_combo)
        action_row.addStretch()

        self._convert_btn = QPushButton("Convert to 60fps")
        self._convert_btn.setObjectName("convertBtn")
        self._convert_btn.setEnabled(False)
        self._convert_btn.setFixedWidth(160)
        self._convert_btn.clicked.connect(self._start_conversion)
        action_row.addWidget(self._convert_btn)
        layout.addLayout(action_row)
        layout.addSpacing(12)

        # ── Progress bar + status ────────────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(4)
        layout.addWidget(self._progress_bar)

        status_row = QHBoxLayout()
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #444; font-size: 11px;")
        status_row.addWidget(self._progress_label)
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
        layout.addLayout(status_row)

        # ── Log ──────────────────────────────────────────────────────────
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(0, 8, 0, 0)
        self._log = QTextEdit()
        self._log.setObjectName("logConsole")
        self._log.setReadOnly(True)
        self._log.setFixedHeight(140)
        log_layout.addWidget(self._log)
        layout.addWidget(log_group)

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

        self._log_message(f"Loaded: {path}")
        self._drop_zone.setText(f"Selected:\n{Path(path).name}")

        # Probe
        try:
            info = probe_video(path)
            self._info_label.setText(
                f"File: {Path(path).name}\n"
                f"Resolution: {info['width']} × {info['height']}\n"
                f"Input FPS: {info['fps']:.3f}\n"
                f"Duration: {info['duration']:.2f} s\n"
                f"Audio: {'Yes' if info['has_audio'] else 'No'}"
            )
            self._log_message(
                f"  {info['width']}x{info['height']} @ {info['fps']:.3f} fps, "
                f"{info['duration']:.2f}s"
            )
        except Exception as exc:
            self._info_label.setText(f"Could not probe file:\n{exc}")
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
                    213, 120,
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
        self._progress_label.setText("")
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
        self._progress_label.setText(f"{pct}%  ·  frame {current} / {total}")

    def _on_done(self, output_path: str):
        self._output_path = output_path
        self._done_label.setText(f"Saved → {Path(output_path).name}")
        self._done_label.show()
        show_label = "Show in Finder" if sys.platform == "darwin" else "Open Folder"
        self._open_folder_btn.setText(show_label)
        self._open_folder_btn.show()
        self._convert_btn.setEnabled(True)
        self._progress_bar.setValue(self._progress_bar.maximum())
        self._progress_label.setText("Done")

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

    def _log_message(self, msg: str):
        self._log.append(msg)
        # Scroll to bottom
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, event):
        if self._worker:
            self._worker.cancel()
        super().closeEvent(event)
