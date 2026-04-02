"""
RIFE (Real-Time Intermediate Flow Estimation) utilities for FlowCap.
Uses the rife-ncnn-vulkan standalone binary — GPU via Vulkan, works on
NVIDIA, AMD, and Intel. Falls back to CPU if no Vulkan-capable GPU.
"""

import os
import subprocess
import threading
import time
from pathlib import Path


def find_rife() -> tuple[str | None, str | None]:
    """
    Return (binary_path, rife_dir).
    rife_dir is the directory containing model subdirectories (e.g. rife-v4.6/).
    Checks vendor/rife/ first (PyInstaller bundle), then local dev path.
    """
    import sys as _sys
    from pathlib import Path as _Path

    suffix = ".exe" if _sys.platform == "win32" else ""

    # PyInstaller bundle
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        rife_dir = _Path(meipass) / "vendor" / "rife"
        binary = rife_dir / f"rife-ncnn-vulkan{suffix}"
        if binary.exists():
            return str(binary), str(rife_dir)

    # Dev / local vendor/rife/
    local = _Path(__file__).parent.parent / "vendor" / "rife"
    binary = local / f"rife-ncnn-vulkan{suffix}"
    if binary.exists():
        return str(binary), str(local)

    return None, None


def pick_model(rife_dir: str) -> str:
    """
    Return the name of the best available model inside rife_dir.
    Prefers newer rife-v4.x models.
    """
    prefer = ["rife-v4.7", "rife-v4.6", "rife-v4", "rife-v3.9", "rife-v3.1"]
    rd = Path(rife_dir)
    for name in prefer:
        if (rd / name).is_dir():
            return name
    for p in sorted(rd.iterdir()):
        if p.is_dir():
            return p.name
    raise RuntimeError(f"No RIFE model found in {rife_dir}")


def interpolate_rife(
    input_dir: str,
    output_dir: str,
    rife_bin: str,
    model_path: str,
    uhd: bool = False,
    log_callback=None,
    progress_callback=None,
    expected_output_frames: int = 0,
    cancel_check=None,
) -> None:
    """
    Run rife-ncnn-vulkan on input_dir → output_dir.
    For N input frames, produces 2N-1 output frames.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    cmd = [
        rife_bin,
        "-i", input_dir,
        "-o", output_dir,
        "-m", model_path,
        "-j", "2:4:4",   # load:proc:save threads
    ]
    if uhd:
        cmd += ["-s"]

    if log_callback:
        log_callback(f"  Model: {Path(model_path).name}   UHD: {'yes' if uhd else 'no'}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    out_path = Path(output_dir)

    def _monitor():
        while proc.poll() is None:
            if cancel_check and cancel_check():
                proc.terminate()
                return
            count = sum(1 for _ in out_path.glob("*.png"))
            if progress_callback and expected_output_frames > 0:
                progress_callback(min(count, expected_output_frames), expected_output_frames)
            time.sleep(0.3)

    monitor = threading.Thread(target=_monitor, daemon=True)
    monitor.start()

    for line in proc.stdout:
        stripped = line.strip()
        if stripped and log_callback:
            log_callback(f"  {stripped}")
        if cancel_check and cancel_check():
            proc.terminate()
            break

    proc.wait()
    monitor.join(timeout=2)

    if cancel_check and cancel_check():
        return

    if proc.returncode not in (0, None):
        raise RuntimeError(
            f"rife-ncnn-vulkan failed (exit {proc.returncode}). "
            "If no Vulkan GPU is available it will use CPU fallback (slower)."
        )
