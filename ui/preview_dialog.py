"""
Before / After preview dialog for FlowCap.
"""

import os
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
)

from core.ffmpeg_utils import extract_thumbnail, probe_video


class PreviewDialog(QDialog):
    def __init__(self, parent, input_path: str, output_path: str):
        super().__init__(parent)
        self.setWindowTitle("Before / After")
        self.setModal(True)
        self.setFixedSize(760, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # Title row
        title = QLabel("Before / After Preview")
        title.setStyleSheet("font-size: 14px; font-weight: 600; color: #e0e0e0;")
        layout.addWidget(title)

        # Thumbnail row
        thumb_row = QHBoxLayout()
        thumb_row.setSpacing(20)

        self._before_lbl = self._make_thumb_widget("Original")
        self._after_lbl = self._make_thumb_widget("Converted")

        thumb_row.addWidget(self._before_lbl["frame"])
        thumb_row.addWidget(self._after_lbl["frame"])
        layout.addLayout(thumb_row)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.close)
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        # Load thumbnails
        self._load_thumbnails(input_path, output_path)

    def _make_thumb_widget(self, label_text: str) -> dict:
        frame = QLabel()
        frame.setFixedSize(340, 220)
        frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        frame.setStyleSheet(
            "background-color: #111; border: 1px solid #222; border-radius: 6px; color: #555;"
        )
        frame.setText("Loading…")

        caption = QLabel(label_text)
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setStyleSheet("color: #666; font-size: 11px;")

        wrapper_layout = QVBoxLayout()
        wrapper_layout.setSpacing(6)
        wrapper_layout.addWidget(frame)
        wrapper_layout.addWidget(caption)

        from PyQt6.QtWidgets import QWidget
        wrapper = QWidget()
        wrapper.setStyleSheet("background: transparent;")
        wrapper.setLayout(wrapper_layout)

        return {"frame": wrapper, "img": frame}

    def _load_thumbnails(self, input_path: str, output_path: str):
        try:
            info = probe_video(input_path)
            t = info["duration"] * 0.25
        except Exception:
            t = 0.0

        self._extract_into(input_path, t, self._before_lbl["img"])
        self._extract_into(output_path, t, self._after_lbl["img"])

    def _extract_into(self, video_path: str, time: float, label: QLabel):
        if not os.path.exists(video_path):
            label.setText("Not found")
            return
        tmp = tempfile.mktemp(suffix=".jpg")
        try:
            ok = extract_thumbnail(video_path, tmp, time=time)
            if ok:
                pix = QPixmap(tmp)
                if not pix.isNull():
                    label.setPixmap(
                        pix.scaled(
                            340, 220,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                    label.setText("")
                    return
            label.setText("Preview unavailable")
        except Exception:
            label.setText("Preview unavailable")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
