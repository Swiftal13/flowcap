"""
FlowCap processor — orchestrates the interpolation pipeline.

Uses FFmpeg's minterpolate filter instead of hand-rolled optical flow.
No OpenCV or numpy required here anymore.
"""

import os
import shutil
import tempfile
from pathlib import Path

from core.ffmpeg_utils import (
    probe_video,
    interpolate_video,
    extract_audio,
    mux_audio,
)

QUALITY_BALANCED = "balanced"
QUALITY_HIGH = "high"


def process_video(
    input_path: str,
    output_fps: float = 60.0,
    quality: str = QUALITY_BALANCED,
    progress_callback=None,
    log_callback=None,
    cancel_check=None,
) -> str:
    """
    Full pipeline:
      1. Probe input
      2. Extract audio (if present)
      3. Interpolate video to output_fps via FFmpeg minterpolate
      4. Mux audio back in
      5. Clean up temp files

    Returns the path to the final output file.
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    # ── 1. Probe ─────────────────────────────────────────────────────────
    log("Probing input video...")
    info = probe_video(input_path)
    input_fps = info["fps"]
    has_audio = info["has_audio"]
    duration = info["duration"]

    log(
        f"  {info['width']}×{info['height']}  "
        f"{input_fps:.3f} fps  "
        f"{duration:.2f}s  "
        f"audio={'yes' if has_audio else 'no'}"
    )

    if abs(input_fps - output_fps) < 0.1:
        log(f"  Note: input is already ~{input_fps:.1f} fps — re-encoding at exactly {output_fps:.0f} fps.")

    total_output_frames = max(1, int(round(duration * output_fps)))
    log(f"  Expected output: ~{total_output_frames} frames @ {output_fps:.0f} fps")

    # ── 2. Temp workspace ─────────────────────────────────────────────────
    tmpdir = tempfile.mkdtemp(prefix="flowcap_")
    audio_path = os.path.join(tmpdir, "audio.aac")
    video_interp_path = os.path.join(tmpdir, "video_interp.mp4")

    output_path = _output_path(input_path)

    try:
        # ── 3. Extract audio ──────────────────────────────────────────────
        if has_audio:
            log("Extracting audio track...")
            has_audio = extract_audio(input_path, audio_path)
            log("  Audio extracted." if has_audio else "  No audio found — continuing without it.")

        if cancel_check and cancel_check():
            return output_path

        # ── 4. Interpolate video ──────────────────────────────────────────
        log(f"Starting motion-interpolation to {output_fps:.0f} fps...")
        interp_target = video_interp_path if has_audio else output_path

        interpolate_video(
            input_path=input_path,
            output_path=interp_target,
            input_fps=input_fps,
            output_fps=output_fps,
            quality=quality,
            log_callback=log_callback,
            progress_callback=progress_callback,
            total_output_frames=total_output_frames,
            cancel_check=cancel_check,
        )

        if cancel_check and cancel_check():
            return output_path

        # ── 5. Mux audio ──────────────────────────────────────────────────
        if has_audio:
            log("Muxing audio into final file...")
            mux_audio(video_interp_path, audio_path, output_path)
            log("  Done.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log(f"Output: {output_path}")
    return output_path


def _output_path(input_path: str) -> str:
    p = Path(input_path)
    return str(p.parent / f"{p.stem}_flowcap.mp4")
