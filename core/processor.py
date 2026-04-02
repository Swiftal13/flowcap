"""
FlowCap processor — RIFE GPU interpolation pipeline.

Pipeline:
  1. Probe input video
  2. Extract audio (if present)
  3. Extract frames to disk (PNG via ffmpeg)
  4. Run rife-ncnn-vulkan (GPU via Vulkan) — one or two passes to reach target fps
  5. Encode interpolated frames back to video (ffmpeg)
  6. Mux audio back in
  7. Clean up temp files
"""

import math
import os
import shutil
import tempfile
from pathlib import Path

from core.ffmpeg_utils import (
    probe_video,
    extract_audio,
    mux_audio,
    extract_frames,
    encode_frames,
)
from core.rife_utils import find_rife, pick_model, interpolate_rife

QUALITY_BALANCED = "balanced"
QUALITY_HIGH = "high"


def process_video(
    input_path: str,
    output_fps: float = 60.0,
    quality: str = QUALITY_BALANCED,
    output_path: str | None = None,
    progress_callback=None,
    log_callback=None,
    cancel_check=None,
) -> str:
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    def cancelled() -> bool:
        return cancel_check is not None and cancel_check()

    # ── Output path ───────────────────────────────────────────────────────
    if output_path is None:
        p = Path(input_path)
        output_path = str(p.parent / f"{p.stem}_flowcap.mp4")

    # ── 1. Probe ──────────────────────────────────────────────────────────
    log("Probing input video...")
    info = probe_video(input_path)
    input_fps = info["fps"]
    has_audio = info["has_audio"]
    duration = info["duration"]
    log(
        f"  {info['width']}×{info['height']}  {input_fps:.3f} fps  "
        f"{duration:.2f}s  audio={'yes' if has_audio else 'no'}"
    )

    # ── RIFE pass strategy ────────────────────────────────────────────────
    # Always extract at the original fps and do exactly 1 RIFE pass to double
    # the frame count. FFmpeg then resamples to output_fps.
    # This ensures RIFE only synthesises 1 frame between adjacent originals —
    # the frames are close together so motion estimation is accurate and clean.
    # Doing multiple passes with sub-sampled input creates warping artifacts.
    passes = 1
    extract_fps = input_fps  # extract at original fps, no downsampling

    total_input_frames = max(1, int(round(duration * extract_fps)))
    total_output_frames = max(1, int(round(duration * output_fps)))
    log(f"  Extract at: {extract_fps:.2f} fps   RIFE passes: {passes}   → {output_fps:.0f} fps")
    log(f"  Expected output: ~{total_output_frames} frames @ {output_fps:.0f} fps")

    # ── RIFE binary ───────────────────────────────────────────────────────
    rife_bin, rife_dir = find_rife()
    if not rife_bin:
        raise RuntimeError(
            "rife-ncnn-vulkan not found. Run: python vendor_ffmpeg.py"
        )
    model_name = pick_model(rife_dir)
    model_path = str(Path(rife_dir) / model_name)
    uhd = (quality == QUALITY_HIGH)
    log(f"  RIFE binary: {rife_bin}")
    log(f"  Model: {model_name}   UHD: {uhd}")

    # ── Temp workspace ────────────────────────────────────────────────────
    tmpdir = tempfile.mkdtemp(prefix="flowcap_")
    audio_path = os.path.join(tmpdir, "audio.aac")
    frames_in_dir = os.path.join(tmpdir, "frames_in")
    frames_out_dir = os.path.join(tmpdir, "frames_out")
    video_no_audio = os.path.join(tmpdir, "video_noaudio.mp4")
    os.makedirs(frames_in_dir)
    os.makedirs(frames_out_dir)

    # Progress stage weights: extract=10%, rife=75%, encode=15%
    def _stage_cb(start_frac, end_frac):
        def cb(cur, tot):
            if not progress_callback or tot <= 0:
                return
            frac = cur / tot
            mapped = int((start_frac + frac * (end_frac - start_frac)) * total_output_frames)
            progress_callback(mapped, total_output_frames)
        return cb

    try:
        # ── 2. Extract audio ──────────────────────────────────────────────
        if has_audio:
            log("Extracting audio track...")
            has_audio = extract_audio(input_path, audio_path)

        if cancelled():
            return output_path

        # ── 3. Extract frames ─────────────────────────────────────────────
        log(f"Extracting frames at {extract_fps:.2f} fps...")
        n_frames = extract_frames(
            input_path, frames_in_dir,
            target_fps=extract_fps,
            log_callback=log_callback,
            progress_callback=_stage_cb(0.0, 0.10),
            total_frames=total_input_frames,
            cancel_check=cancel_check,
        )
        if cancelled():
            return output_path
        log(f"  Extracted {n_frames} frames.")

        # ── 4. RIFE interpolation passes ─────────────────────────────────
        current_in = frames_in_dir
        current_fps = extract_fps

        for pass_num in range(passes):
            pass_out = os.path.join(tmpdir, f"rife_pass{pass_num + 1}")
            os.makedirs(pass_out, exist_ok=True)

            pass_start = 0.10 + (pass_num / passes) * 0.75
            pass_end = 0.10 + ((pass_num + 1) / passes) * 0.75

            n_in = len(list(Path(current_in).glob("*.png")))
            expected_out = max(1, 2 * n_in - 1)

            log(f"RIFE pass {pass_num + 1}/{passes}  ({n_in} frames → ~{expected_out} frames)...")
            interpolate_rife(
                input_dir=current_in,
                output_dir=pass_out,
                rife_bin=rife_bin,
                model_path=model_path,
                uhd=uhd,
                log_callback=log_callback,
                progress_callback=_stage_cb(pass_start, pass_end),
                expected_output_frames=expected_out,
                cancel_check=cancel_check,
            )
            if cancelled():
                return output_path

            current_in = pass_out
            current_fps *= 2
            log(f"  Pass {pass_num + 1} done.")

        frames_final_dir = current_in
        # Compute exact frame rate from actual frame count and video duration
        actual_frames = len(list(Path(frames_final_dir).glob("*.png")))
        final_frame_rate = actual_frames / duration if duration > 0 else current_fps

        # ── 5. Encode frames ──────────────────────────────────────────────
        encode_target = video_no_audio if has_audio else output_path
        log(f"Encoding {int(output_fps)} fps video...")
        encode_frames(
            frames_dir=frames_final_dir,
            output_path=encode_target,
            frame_rate=final_frame_rate,
            output_fps=output_fps,
            log_callback=log_callback,
            progress_callback=_stage_cb(0.85, 1.0),
            total_frames=total_output_frames,
            cancel_check=cancel_check,
        )
        if cancelled():
            return output_path

        # ── 6. Mux audio ──────────────────────────────────────────────────
        if has_audio:
            log("Muxing audio...")
            mux_audio(video_no_audio, audio_path, output_path)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log(f"Output: {output_path}")
    return output_path
