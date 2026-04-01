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
    """Return (ffmpeg_path, ffprobe_path) or (None, None) if not found."""
    return shutil.which("ffmpeg"), shutil.which("ffprobe")


def probe_video(input_path: str) -> dict:
    """
    Run ffprobe and return:
      fps, width, height, duration (seconds), has_audio
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found in PATH.")

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
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH.")

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
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH.")

    proc = subprocess.run(
        [ffmpeg, "-y", "-i", input_path, "-vn", "-acodec", "copy", audio_path],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0


def mux_audio(video_path: str, audio_path: str, output_path: str) -> None:
    """Mux video and audio streams into output_path."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH.")

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
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    proc = subprocess.run(
        [ffmpeg, "-y", "-ss", str(time), "-i", input_path,
         "-vframes", "1", "-q:v", "2", thumbnail_path],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and os.path.exists(thumbnail_path)
