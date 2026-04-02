"""
FFmpeg utility functions for FlowCap.
"""

import re
import subprocess
import json
import os
import shutil
import math


def find_ffmpeg() -> tuple[str | None, str | None]:
    """
    Return (ffmpeg_path, ffprobe_path).
    Checks the PyInstaller bundle (sys._MEIPASS/vendor/) first,
    then falls back to PATH so dev mode still works.
    """
    import sys as _sys
    from pathlib import Path as _Path

    # Inside a PyInstaller bundle, binaries are extracted to _MEIPASS
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        vendor = _Path(meipass) / "vendor"
        suffix = ".exe" if _sys.platform == "win32" else ""
        ff = vendor / f"ffmpeg{suffix}"
        fp = vendor / f"ffprobe{suffix}"
        if ff.exists() and fp.exists():
            return str(ff), str(fp)

    # Dev / system install fallback
    return shutil.which("ffmpeg"), shutil.which("ffprobe")


def probe_video(input_path: str) -> dict:
    """
    Run ffprobe and return:
      fps, width, height, duration (seconds), has_audio
    """
    _, ffprobe = find_ffmpeg()
    if not ffprobe:
        raise RuntimeError("ffprobe not found.")

    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    video_stream = None
    has_audio = False
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream
        elif stream.get("codec_type") == "audio":
            has_audio = True

    if video_stream is None:
        raise ValueError("No video stream found in input file.")

    num, den = video_stream.get("r_frame_rate", "60/1").split("/")
    fps = float(num) / float(den)

    duration_str = (
        video_stream.get("duration")
        or data.get("format", {}).get("duration")
        or "0"
    )

    return {
        "fps": fps,
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "duration": float(duration_str),
        "has_audio": has_audio,
    }


def interpolate_video(
    input_path: str,
    output_path: str,
    input_fps: float,
    output_fps: float = 60.0,
    quality: str = "balanced",
    log_callback=None,
    progress_callback=None,
    total_output_frames: int = 0,
    cancel_check=None,
) -> None:
    """
    Produce a butter-smooth output using the "oversample → temporal blend → downsample" technique.

    Why a single minterpolate to 60fps isn't enough:
      Each output frame is just one synthesised instant — motion still strobes at 60fps.

    What we do instead:
      1. minterpolate UP to a high intermediate fps (intermediate_fps = output_fps × blend_factor)
         This synthesises motion-compensated frames at very fine time intervals.
      2. tmix — average `blend_factor` consecutive intermediate frames into one.
         Each output frame now represents a smeared window of continuous motion
         → natural, film-quality motion blur that makes motion feel fluid.
      3. fps filter — drop to output_fps. Because each kept frame already encodes
         a blend of `blend_factor` intermediate frames, motion looks like it was
         shot at intermediate_fps even though the file is output_fps.

    Quality presets:
      balanced → intermediate = output_fps × 4  (e.g. 60 → 240fps intermediate)
                 me=epzs, mc_mode=aobmc, vsbmc=1
      high     → intermediate = output_fps × 8  (e.g. 60 → 480fps intermediate)
                 me=umh (wider search, better on fast/complex motion)

    The blend_factor controls how much temporal window each output frame covers:
      4× → each 60fps frame blends 4 × (1/240s) = 1/60s of motion  ← natural
      8× → same window, finer intermediate sampling                  ← smoother
    """
    ffmpeg, _ = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found.")

    if quality == "high":
        blend_factor = 8
        me = "umh"
    else:
        blend_factor = 4
        me = "epzs"

    # Intermediate fps must be a whole number for tmix to divide cleanly
    intermediate_fps = int(output_fps) * blend_factor

    # Ensure intermediate_fps is always strictly above input_fps so minterpolate
    # is actually synthesising new frames (not just dropping).
    if intermediate_fps <= input_fps:
        intermediate_fps = int(math.ceil(input_fps / output_fps) + blend_factor) * int(output_fps)

    # Equal weights for all blended frames — uniform temporal average.
    weights = " ".join(["1"] * blend_factor)

    vf = (
        # Step 1 — synthesise motion-compensated frames at high rate
        f"minterpolate=fps={intermediate_fps}:"
        f"mi_mode=mci:"
        f"mc_mode=aobmc:"
        f"me={me}:"
        f"vsbmc=1:"
        f"scd=fdiff,"
        # Step 2 — blend blend_factor frames into one (temporal motion blur)
        f"tmix=frames={blend_factor}:weights='{weights}',"
        # Step 3 — drop to output fps (each remaining frame is already blended)
        f"fps={int(output_fps)}"
    )

    cmd = [
        ffmpeg, "-y",
        "-i", input_path,
        "-an",
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        "-movflags", "+faststart",
        output_path,
    ]

    if log_callback:
        log_callback(
            f"Oversample → blend → downsample  "
            f"[{quality}  |  {intermediate_fps}fps intermediate  |  "
            f"{blend_factor}× blend  |  me={me}]"
        )
        log_callback(f"  vf: {vf}")

    # Stream stderr live for progress
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=0,
    )

    # FFmpeg's frame= counter tracks output frames (after the full filter chain),
    # so it goes 0 → total_output_frames directly.
    frame_re = re.compile(r"frame=\s*(\d+)")
    buf = ""
    last_logged_pct = -1

    while True:
        ch = proc.stderr.read(1)
        if not ch:
            break

        if cancel_check and cancel_check():
            proc.terminate()
            proc.wait()
            return

        if ch in ("\r", "\n"):
            m = frame_re.search(buf)
            if m:
                current = int(m.group(1))
                if progress_callback and total_output_frames > 0:
                    progress_callback(
                        min(current, total_output_frames),
                        total_output_frames,
                    )
                if log_callback and total_output_frames > 0:
                    pct = int(100 * current / total_output_frames)
                    if pct >= last_logged_pct + 5:
                        last_logged_pct = pct
                        log_callback(f"  {pct}%  (frame {current} / {total_output_frames})")
            buf = ""
        else:
            buf += ch

    proc.wait()

    if proc.returncode not in (0, None):
        raise RuntimeError(
            f"FFmpeg failed (exit {proc.returncode}).\n"
            "Ensure FFmpeg ≥ 4.0 is installed: brew install ffmpeg"
        )

    if log_callback:
        log_callback("  Done.")


def extract_audio(input_path: str, audio_path: str) -> bool:
    """Copy audio stream to audio_path. Returns True on success."""
    ffmpeg, _ = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found.")

    proc = subprocess.run(
        [ffmpeg, "-y", "-i", input_path, "-vn", "-acodec", "copy", audio_path],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0


def mux_audio(video_path: str, audio_path: str, output_path: str) -> None:
    """Mux video and audio streams into output_path."""
    ffmpeg, _ = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found.")

    proc = subprocess.run(
        [
            ffmpeg, "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "copy",
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg mux failed:\n{proc.stderr[-800:]}")


def extract_thumbnail(input_path: str, thumbnail_path: str, time: float = 0.0) -> bool:
    """Extract a single JPEG frame at `time` seconds."""
    ffmpeg, _ = find_ffmpeg()
    if not ffmpeg:
        return False
    proc = subprocess.run(
        [ffmpeg, "-y", "-ss", str(time), "-i", input_path,
         "-vframes", "1", "-q:v", "2", thumbnail_path],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and os.path.exists(thumbnail_path)


def extract_frames(
    input_path: str,
    output_dir: str,
    log_callback=None,
    progress_callback=None,
    total_frames: int = 0,
    cancel_check=None,
) -> int:
    """
    Extract every frame from input_path as PNG images into output_dir.
    Returns the number of frames extracted.
    """
    ffmpeg, _ = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found.")

    pattern = os.path.join(output_dir, "%08d.png")
    cmd = [
        ffmpeg, "-y",
        "-i", input_path,
        "-vsync", "0",
        pattern,
    ]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=0,
    )

    frame_re = re.compile(r"frame=\s*(\d+)")
    buf = ""
    last_logged_pct = -1

    while True:
        ch = proc.stderr.read(1)
        if not ch:
            break
        if cancel_check and cancel_check():
            proc.terminate()
            proc.wait()
            return 0
        if ch in ("\r", "\n"):
            m = frame_re.search(buf)
            if m and total_frames > 0:
                current = int(m.group(1))
                if progress_callback:
                    progress_callback(min(current, total_frames), total_frames)
                if log_callback:
                    pct = int(100 * current / total_frames)
                    if pct >= last_logged_pct + 10:
                        last_logged_pct = pct
                        log_callback(f"  Extracting frames: {pct}%")
            buf = ""
        else:
            buf += ch

    proc.wait()
    if proc.returncode not in (0, None):
        raise RuntimeError(f"FFmpeg frame extraction failed (exit {proc.returncode})")

    count = len(list(Path(output_dir).glob("*.png")))
    if log_callback:
        log_callback(f"  Extracted {count} frames.")
    return count


def encode_frames(
    frames_dir: str,
    output_path: str,
    frame_rate: float,
    output_fps: float,
    log_callback=None,
    progress_callback=None,
    total_frames: int = 0,
    cancel_check=None,
) -> None:
    """
    Encode a directory of PNG frames to video.
    frame_rate: effective fps of the image sequence (2× input fps after RIFE)
    output_fps: desired output fps (e.g. 60)
    """
    from pathlib import Path as _Path

    ffmpeg, _ = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found.")

    frames = sorted(
        _Path(frames_dir).glob("*.png"),
        key=lambda f: int(f.stem) if f.stem.isdigit() else int(f.stem.lstrip("0") or "0")
    )
    if not frames:
        raise RuntimeError(f"No PNG frames found in {frames_dir}")

    # Build concat list with explicit frame durations for accurate fps conversion
    concat_file = _Path(frames_dir) / "_concat.txt"
    duration = 1.0 / frame_rate
    with open(concat_file, "w") as f:
        for frame in frames:
            f.write(f"file '{frame.as_posix()}'\n")
            f.write(f"duration {duration:.10f}\n")
        # Concat demuxer requires a trailing entry without duration
        f.write(f"file '{frames[-1].as_posix()}'\n")

    cmd = [
        ffmpeg, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-vf", f"fps={int(output_fps)}",
        "-c:v", "libx264",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        "-movflags", "+faststart",
        output_path,
    ]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=0,
    )

    frame_re = re.compile(r"frame=\s*(\d+)")
    buf = ""
    last_logged_pct = -1

    while True:
        ch = proc.stderr.read(1)
        if not ch:
            break
        if cancel_check and cancel_check():
            proc.terminate()
            proc.wait()
            return
        if ch in ("\r", "\n"):
            m = frame_re.search(buf)
            if m and total_frames > 0:
                current = int(m.group(1))
                if progress_callback:
                    progress_callback(min(current, total_frames), total_frames)
                if log_callback:
                    pct = int(100 * current / total_frames)
                    if pct >= last_logged_pct + 5:
                        last_logged_pct = pct
                        log_callback(f"  Encoding: {pct}%  (frame {current} / {total_frames})")
            buf = ""
        else:
            buf += ch

    proc.wait()
    if proc.returncode not in (0, None):
        raise RuntimeError(f"FFmpeg encoding failed (exit {proc.returncode})")


# Keep Path import available for extract_frames return
from pathlib import Path
