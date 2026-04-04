"""
FlowCap processor — RIFE GPU interpolation pipeline.

Pipeline:
  1. Probe input video
  2. Extract audio (if present)
  3. (Optional) Detect scene cuts → split into segments
  4. For each segment: extract frames → RIFE passes → encode to video
  5. Concatenate segments (if multiple)
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
    concat_videos,
    detect_scene_cuts,
)
from core.rife_utils import find_rife, pick_model, interpolate_rife

QUALITY_BALANCED = "balanced"
QUALITY_HIGH = "high"


def process_video(
    input_path: str,
    output_fps: float = 60.0,
    quality: str = QUALITY_BALANCED,
    detect_scenes: bool = False,
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
    passes = 3 if quality == QUALITY_HIGH else 2

    # When input is already above output_fps, extract at output_fps so that
    # RIFE passes double clean evenly-spaced frames and the final encode
    # ratio is always a power-of-2 (e.g. 4× for 2 passes, 8× for 3 passes).
    # This avoids non-integer downsampling ratios (e.g. 400fps→60fps = 6.67×)
    # which cause alternating 6/7 frame gaps and subtle stutter.
    if input_fps > output_fps * 1.05:
        extract_fps = output_fps
    else:
        extract_fps = input_fps

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
    video_no_audio = os.path.join(tmpdir, "video_noaudio.mp4")

    # ── Scene cut detection ───────────────────────────────────────────────
    if detect_scenes:
        log("Detecting scene cuts...")
        cuts = detect_scene_cuts(input_path)
        if cuts:
            log(f"  Found {len(cuts)} cut(s): {[f'{t:.2f}s' for t in cuts]}")
        else:
            log("  No cuts found — processing as single segment.")
    else:
        cuts = []

    boundaries = [0.0] + cuts + [duration]
    segments = [
        (boundaries[i], boundaries[i + 1])
        for i in range(len(boundaries) - 1)
        if boundaries[i + 1] - boundaries[i] > 0.1
    ]
    multi_segment = len(segments) > 1
    if multi_segment:
        log(f"  {len(segments)} segments to process.")

    total_dur = sum(e - s for s, e in segments)

    def _make_progress_cb(seg_start_frac: float, seg_end_frac: float,
                          stage_start: float, stage_end: float):
        def cb(cur, tot):
            if not progress_callback or tot <= 0:
                return
            local = cur / tot
            stage_frac = stage_start + local * (stage_end - stage_start)
            overall = seg_start_frac + stage_frac * (seg_end_frac - seg_start_frac)
            progress_callback(int(overall * total_output_frames), total_output_frames)
        return cb

    try:
        # ── 2. Extract audio ──────────────────────────────────────────────
        if has_audio:
            log("Extracting audio track...")
            has_audio = extract_audio(input_path, audio_path)

        if cancelled():
            return output_path

        segment_videos: list[str] = []
        elapsed_dur = 0.0

        for seg_idx, (seg_start, seg_end) in enumerate(segments):
            seg_dur = seg_end - seg_start
            seg_frac_start = elapsed_dur / total_dur if total_dur > 0 else 0.0
            seg_frac_end = (elapsed_dur + seg_dur) / total_dur if total_dur > 0 else 1.0
            elapsed_dur += seg_dur

            if multi_segment:
                log(f"\n── Segment {seg_idx + 1}/{len(segments)}  "
                    f"({seg_start:.2f}s → {seg_end:.2f}s) ──")

            seg_tmpdir = os.path.join(tmpdir, f"seg{seg_idx}")
            frames_in_dir = os.path.join(seg_tmpdir, "frames_in")
            os.makedirs(frames_in_dir, exist_ok=True)

            seg_input_frames = max(1, int(round(seg_dur * extract_fps)))
            seg_output_frames = max(1, int(round(seg_dur * output_fps)))

            # ── 3. Extract frames ─────────────────────────────────────────
            log(f"Extracting frames at {extract_fps:.2f} fps...")
            n_frames = extract_frames(
                input_path, frames_in_dir,
                target_fps=extract_fps,
                start_time=seg_start if seg_start > 0 else None,
                end_time=seg_end if seg_end < duration else None,
                log_callback=log_callback,
                progress_callback=_make_progress_cb(seg_frac_start, seg_frac_end, 0.0, 0.10),
                total_frames=seg_input_frames,
                cancel_check=cancel_check,
            )
            if cancelled():
                return output_path
            log(f"  Extracted {n_frames} frames.")

            # ── 4. RIFE interpolation passes ──────────────────────────────
            current_in = frames_in_dir
            current_fps = extract_fps

            for pass_num in range(passes):
                pass_out = os.path.join(seg_tmpdir, f"rife_pass{pass_num + 1}")
                os.makedirs(pass_out, exist_ok=True)

                p_start = 0.10 + (pass_num / passes) * 0.75
                p_end = 0.10 + ((pass_num + 1) / passes) * 0.75

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
                    progress_callback=_make_progress_cb(
                        seg_frac_start, seg_frac_end, p_start, p_end
                    ),
                    expected_output_frames=expected_out,
                    cancel_check=cancel_check,
                )
                if cancelled():
                    return output_path

                current_in = pass_out
                current_fps *= 2
                log(f"  Pass {pass_num + 1} done.")

            frames_final_dir = current_in
            actual_frames = len(list(Path(frames_final_dir).glob("*.png")))
            final_frame_rate = actual_frames / seg_dur if seg_dur > 0 else current_fps

            # ── 5. Encode segment ─────────────────────────────────────────
            if multi_segment:
                seg_video_path = os.path.join(seg_tmpdir, "segment.mp4")
            else:
                seg_video_path = video_no_audio if has_audio else output_path

            log(f"Encoding {int(output_fps)} fps video...")
            encode_frames(
                frames_dir=frames_final_dir,
                output_path=seg_video_path,
                frame_rate=final_frame_rate,
                output_fps=output_fps,
                log_callback=log_callback,
                progress_callback=_make_progress_cb(seg_frac_start, seg_frac_end, 0.85, 1.0),
                total_frames=seg_output_frames,
                cancel_check=cancel_check,
            )
            if cancelled():
                return output_path

            segment_videos.append(seg_video_path)

        # ── 6. Concatenate segments ───────────────────────────────────────
        if multi_segment:
            concat_target = video_no_audio if has_audio else output_path
            log(f"Concatenating {len(segment_videos)} segments...")
            concat_videos(segment_videos, concat_target)

        # ── 7. Mux audio ──────────────────────────────────────────────────
        if has_audio:
            log("Muxing audio...")
            mux_audio(video_no_audio, audio_path, output_path)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log(f"Output: {output_path}")
    return output_path
