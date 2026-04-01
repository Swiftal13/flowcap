"""
Downloads static FFmpeg + ffprobe binaries into vendor/ for bundling.
Run once before building: python vendor_ffmpeg.py

Sources:
  macOS  → evermeet.cx (static builds, no deps)
  Windows → gyan.dev   (essentials static build)
"""

import sys
import os
import platform
import urllib.request
import urllib.error
import ssl
import zipfile
import stat
from pathlib import Path

# macOS Python.framework ships without system CA certs — bypass for downloads
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

def _urlretrieve(url: str, dest: Path, label: str):
    """Download url to dest with a progress indicator."""
    def hook(count, block_size, total):
        if total > 0:
            pct = min(100, int(count * block_size * 100 / total))
            print(f"\r  {label}: {pct}%", end="", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=_ssl_ctx) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        block = 8192
        count = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(block)
                if not chunk:
                    break
                f.write(chunk)
                count += 1
                hook(count, block, total)
    print()


VENDOR = Path(__file__).parent / "vendor"


def download_macos():
    base = "https://evermeet.cx/ffmpeg"
    for binary in ("ffmpeg", "ffprobe"):
        url = f"{base}/getrelease/{binary}/zip"
        zip_path = VENDOR / f"{binary}.zip"
        print(f"Downloading {binary}...")
        _urlretrieve(url, zip_path, binary)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(VENDOR)
        zip_path.unlink()
        binary_path = VENDOR / binary
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  Extracted: {binary_path}")


def download_windows():
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    zip_path = VENDOR / "ffmpeg-windows.zip"
    print("Downloading FFmpeg for Windows (~80MB)...")
    _urlretrieve(url, zip_path, "ffmpeg")
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.namelist():
            name = Path(member).name
            if name in ("ffmpeg.exe", "ffprobe.exe"):
                dest = VENDOR / name
                with open(dest, "wb") as f:
                    f.write(z.open(member).read())
                print(f"  Extracted: {dest}")
    zip_path.unlink()


def main():
    VENDOR.mkdir(exist_ok=True)

    # Check if already present
    if sys.platform == "darwin":
        needed = [VENDOR / "ffmpeg", VENDOR / "ffprobe"]
    else:
        needed = [VENDOR / "ffmpeg.exe", VENDOR / "ffprobe.exe"]

    if all(p.exists() for p in needed):
        print("FFmpeg binaries already in vendor/ — nothing to do.")
        print("  Delete vendor/ and re-run to force a fresh download.")
        return

    if sys.platform == "darwin":
        download_macos()
    elif sys.platform == "win32":
        download_windows()
    else:
        print("Linux: install ffmpeg via your package manager.")
        print("  sudo apt install ffmpeg  (Debian/Ubuntu)")
        print("  sudo dnf install ffmpeg  (Fedora)")
        return

    print("\nDone. FFmpeg binaries ready in vendor/")


if __name__ == "__main__":
    main()
