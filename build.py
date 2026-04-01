"""
FlowCap build script.
Usage:  python build.py

Produces:
  macOS  → dist/FlowCap.app  (drag to /Applications)
  Windows → dist/FlowCap/FlowCap.exe
"""

import sys
import subprocess
import shutil
from pathlib import Path


def main():
    # Check PyInstaller is available
    if not shutil.which("pyinstaller"):
        print("PyInstaller not found. Installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    print("Building FlowCap...")
    subprocess.run(
        ["pyinstaller", "--clean", "--noconfirm", "flowcap.spec"],
        check=True,
    )

    dist = Path("dist")
    if sys.platform == "darwin":
        app = dist / "FlowCap.app"
        if app.exists():
            print(f"\nBuild successful: {app.resolve()}")
            print("To install: drag FlowCap.app to your Applications folder.")
            print("Note: FFmpeg must be installed separately (brew install ffmpeg).")
    else:
        exe = dist / "FlowCap" / "FlowCap.exe"
        if exe.exists():
            print(f"\nBuild successful: {exe.resolve()}")
            print("Note: FFmpeg must be installed separately (winget install ffmpeg).")


if __name__ == "__main__":
    main()
