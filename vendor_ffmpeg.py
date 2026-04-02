"""
Downloads static FFmpeg + ffprobe + rife-ncnn-vulkan binaries into vendor/.
Run once before building: python vendor_ffmpeg.py

Sources:
  FFmpeg macOS  → evermeet.cx (static builds)
  FFmpeg Windows → gyan.dev   (essentials static build)
  RIFE           → github.com/nihui/rife-ncnn-vulkan/releases (tag 20221029)
"""

import sys
import os
import json
import urllib.request
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
        block = 65536
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


# ── FFmpeg ────────────────────────────────────────────────────────────────────

def download_macos_ffmpeg():
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


def download_windows_ffmpeg():
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


# ── RIFE ──────────────────────────────────────────────────────────────────────

# Pinned release — stable, tested
_RIFE_TAG = "20221029"
_RIFE_BASE = f"https://github.com/nihui/rife-ncnn-vulkan/releases/download/{_RIFE_TAG}"

# Only extract this one model — keeps bundle size ~60MB instead of 430MB
_RIFE_MODEL = "rife-v4.6"


def download_rife():
    """Download rife-ncnn-vulkan binary + rife-v4.6 model into vendor/rife/."""
    rife_dir = VENDOR / "rife"
    rife_dir.mkdir(exist_ok=True)

    suffix = ".exe" if sys.platform == "win32" else ""
    binary = rife_dir / f"rife-ncnn-vulkan{suffix}"

    if binary.exists() and (rife_dir / _RIFE_MODEL).is_dir():
        print("rife-ncnn-vulkan already in vendor/rife/ — skipping.")
        return

    if sys.platform == "darwin":
        platform_key = "macos"
    elif sys.platform == "win32":
        platform_key = "windows"
    else:
        platform_key = "ubuntu"

    url = f"{_RIFE_BASE}/rife-ncnn-vulkan-{_RIFE_TAG}-{platform_key}.zip"
    zip_path = VENDOR / "rife.zip"

    print(f"Downloading rife-ncnn-vulkan ({platform_key}, ~430MB — extracting one model)...")
    _urlretrieve(url, zip_path, "rife")

    print("  Extracting binary and rife-v4.6 model...")
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.namelist():
            parts = Path(member).parts
            name = Path(member).name

            # Binary
            if name in (f"rife-ncnn-vulkan{suffix}", "rife-ncnn-vulkan", "rife-ncnn-vulkan.exe"):
                dest = rife_dir / f"rife-ncnn-vulkan{suffix}"
                with open(dest, "wb") as f:
                    f.write(z.read(member))
                if sys.platform != "win32":
                    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                print(f"  Extracted: {dest}")

            # Only extract the chosen model's files
            elif len(parts) >= 2 and parts[-2] == _RIFE_MODEL:
                model_dir = rife_dir / _RIFE_MODEL
                model_dir.mkdir(exist_ok=True)
                dest = model_dir / name
                with open(dest, "wb") as f:
                    f.write(z.read(member))

    zip_path.unlink()

    # macOS: remove Gatekeeper quarantine so the binary can run
    if sys.platform == "darwin":
        os.system(f'xattr -d com.apple.quarantine "{binary}" 2>/dev/null || true')

    print(f"  Model {_RIFE_MODEL} extracted to {rife_dir / _RIFE_MODEL}")
    print("  Done.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    VENDOR.mkdir(exist_ok=True)

    # ── FFmpeg ────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        ffmpeg_needed = [VENDOR / "ffmpeg", VENDOR / "ffprobe"]
    else:
        ffmpeg_needed = [VENDOR / "ffmpeg.exe", VENDOR / "ffprobe.exe"]

    if all(p.exists() for p in ffmpeg_needed):
        print("FFmpeg binaries already in vendor/ — skipping.")
    else:
        if sys.platform == "darwin":
            download_macos_ffmpeg()
        elif sys.platform == "win32":
            download_windows_ffmpeg()
        else:
            print("Linux: install ffmpeg via your package manager (apt install ffmpeg).")

    # ── RIFE ─────────────────────────────────────────────────────────────
    download_rife()

    print("\nAll binaries ready in vendor/")


if __name__ == "__main__":
    main()
