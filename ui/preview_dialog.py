"""
Before / After video preview dialog for FlowCap.
Side-by-side video playback using QMediaPlayer + QVideoWidget.
"""

import os
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QSlider, QWidget,
)

from core.ffmpeg_utils import probe_video


class StatsBadge(QWidget):
    """Compact stats strip shown below a video pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "background-color: #111; border: 1px solid #1e1e1e; border-radius: 6px;"
        )

        self.setFixedHeight(30)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(16)

        self._fps_lbl = QLabel("—")
        self._res_lbl = QLabel("—")
        self._flow_lbl = QLabel("—")
        for lbl in (self._fps_lbl, self._res_lbl, self._flow_lbl):
            lbl.setStyleSheet("color: #555; font-size: 11px; background: transparent; border: none;")
            layout.addWidget(lbl)
        layout.addStretch()

    def set_stats(self, fps: float, width: int, height: int, flow: bool):
        self._fps_lbl.setText(f"{fps:.3f} fps")
        self._res_lbl.setText(f"{width}×{height}")
        if flow:
            self._flow_lbl.setText("✦ optical flow")
            self._flow_lbl.setStyleSheet("color: #34d399; font-size: 11px; background: transparent; border: none;")
        else:
            self._flow_lbl.setText("no optical flow")
            self._flow_lbl.setStyleSheet("color: #444; font-size: 11px; background: transparent; border: none;")


class VideoPane(QWidget):
    """One labelled video player pane with stats."""

    def __init__(self, label_text: str, has_flow: bool, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Caption row: title + flow badge
        caption_row = QHBoxLayout()
        caption_row.setSpacing(8)
        caption = QLabel(label_text)
        caption.setStyleSheet(
            "color: #c0c0c0; font-size: 12px; font-weight: 600; background: transparent;"
        )
        caption_row.addWidget(caption)
        if has_flow:
            badge = QLabel("RIFE applied")
            badge.setStyleSheet(
                "color: #34d399; font-size: 10px; font-weight: 600;"
                " background-color: #0d2a20; border: 1px solid #1e4d3a;"
                " border-radius: 4px; padding: 0px 5px;"
            )
            badge.setFixedHeight(caption.sizeHint().height())
            caption_row.addWidget(badge)
        caption_row.addStretch()
        layout.addLayout(caption_row)

        self.video_widget = QVideoWidget()
        self.video_widget.setFixedSize(350, 197)  # 16:9
        layout.addWidget(self.video_widget)

        self.stats = StatsBadge()
        layout.addWidget(self.stats)

        self.player = QMediaPlayer()
        self._audio = QAudioOutput()
        self._audio.setVolume(0.0)
        self.player.setAudioOutput(self._audio)
        self.player.setVideoOutput(self.video_widget)

    def load(self, path: str):
        self.player.setSource(QUrl.fromLocalFile(path))

    def play(self):
        self.player.play()

    def pause(self):
        self.player.pause()

    def seek(self, ms: int):
        self.player.setPosition(ms)

    def position(self) -> int:
        return self.player.position()

    def duration(self) -> int:
        return self.player.duration()

    def set_volume(self, vol: float):
        self._audio.setVolume(vol)

    def set_stats_from_probe(self, path: str, has_flow: bool):
        try:
            info = probe_video(path)
            self.stats.set_stats(info["fps"], info["width"], info["height"], has_flow)
        except Exception:
            pass


class PreviewDialog(QDialog):
    def __init__(self, parent, input_path: str, output_path: str):
        super().__init__(parent)
        self.setWindowTitle("Preview")
        self.setModal(True)
        self.setFixedSize(760, 420)
        self._input_path = input_path
        self._output_path = output_path
        self._playing = False
        self._syncing = False

        self._build_ui()
        self._load_videos()
        self._load_stats()

        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(200)
        self._sync_timer.timeout.connect(self._sync_position)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # Video panes
        pane_row = QHBoxLayout()
        pane_row.setSpacing(12)
        self._before = VideoPane("Original", has_flow=False)
        self._after = VideoPane("Converted", has_flow=True)
        self._after.set_volume(0.8)
        pane_row.addWidget(self._before)
        pane_row.addWidget(self._after)
        layout.addLayout(pane_row)

        # Scrub slider
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.setValue(0)
        self._slider.setStyleSheet(
            "QSlider::groove:horizontal { background: #222; height: 4px; border-radius: 2px; }"
            "QSlider::handle:horizontal { background: #5c5ff0; width: 12px; height: 12px;"
            " margin: -4px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #5c5ff0; border-radius: 2px; }"
        )
        self._slider.sliderMoved.connect(self._on_seek)
        layout.addWidget(self._slider)

        # Controls
        controls = QHBoxLayout()
        controls.setSpacing(8)

        self._play_btn = QPushButton("Play")
        self._play_btn.setFixedWidth(80)
        self._play_btn.clicked.connect(self._toggle_play)

        self._loop_btn = QPushButton("Loop: On")
        self._loop_btn.setFixedWidth(80)
        self._loop_btn.setCheckable(True)
        self._loop_btn.setChecked(True)
        self._loop_btn.clicked.connect(self._toggle_loop)

        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.close)

        controls.addWidget(self._play_btn)
        controls.addWidget(self._loop_btn)
        controls.addStretch()
        controls.addWidget(close_btn)
        layout.addLayout(controls)

        # Connect end-of-media for loop
        self._before.player.mediaStatusChanged.connect(self._on_media_status)
        self._after.player.mediaStatusChanged.connect(self._on_media_status)

        # Update slider as "before" plays
        self._before.player.positionChanged.connect(self._on_position_changed)

    def _load_videos(self):
        if os.path.exists(self._input_path):
            self._before.load(self._input_path)
        if os.path.exists(self._output_path):
            self._after.load(self._output_path)

    def _load_stats(self):
        self._before.set_stats_from_probe(self._input_path, has_flow=False)
        self._after.set_stats_from_probe(self._output_path, has_flow=True)

    def _toggle_play(self):
        if self._playing:
            self._before.pause()
            self._after.pause()
            self._play_btn.setText("Play")
            self._sync_timer.stop()
            self._playing = False
        else:
            self._before.play()
            self._after.play()
            self._play_btn.setText("Pause")
            self._sync_timer.start()
            self._playing = True

    def _toggle_loop(self):
        if self._loop_btn.isChecked():
            self._loop_btn.setText("Loop: On")
        else:
            self._loop_btn.setText("Loop: Off")

    def _on_seek(self, value: int):
        dur = self._before.duration()
        if dur > 0:
            ms = int(value / 1000 * dur)
            self._before.seek(ms)
        dur2 = self._after.duration()
        if dur2 > 0:
            ms2 = int(value / 1000 * dur2)
            self._after.seek(ms2)

    def _on_position_changed(self, pos: int):
        if self._syncing:
            return
        dur = self._before.duration()
        if dur > 0:
            self._syncing = True
            self._slider.setValue(int(pos / dur * 1000))
            self._syncing = False

    def _sync_position(self):
        """Keep after-player in sync with before-player."""
        pos_b = self._before.position()
        pos_a = self._after.position()
        drift = abs(pos_b - pos_a)
        if drift > 300:
            self._after.seek(pos_b)

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._loop_btn.isChecked():
                self._before.seek(0)
                self._after.seek(0)
                self._before.play()
                self._after.play()
            else:
                self._playing = False
                self._play_btn.setText("Play")
                self._sync_timer.stop()

    def closeEvent(self, event):
        self._sync_timer.stop()
        self._before.player.stop()
        self._after.player.stop()
        super().closeEvent(event)
