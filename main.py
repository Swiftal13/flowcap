"""
FlowCap — Entry point.
Converts high-framerate videos to smooth 60fps using optical flow frame blending.
"""

import sys
import os

# Ensure the project root is on the path (important for PyInstaller)
sys.path.insert(0, os.path.dirname(__file__))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

from ui.main_window import MainWindow


def main():
    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("FlowCap")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("FlowCap")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
