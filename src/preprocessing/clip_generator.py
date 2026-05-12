import cv2
import os
import tempfile
import urllib.request
import urllib.error
import numpy as np
import concurrent.futures
from src.preprocessing.video_preprocessor import VideoPreprocessor


def _load_frame(source: str, width: int, height: int):
    """Load a single frame from a local path OR a Cloudinary/HTTP URL."""
    try:
        if source.startswith("http://") or source.startswith("https://"):
            # Download the JPEG from Cloudinary into memory
            with urllib.request.urlopen(source, timeout=10) as resp:
                raw = resp.read()
            arr = np.frombuffer(raw, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            if not os.path.exists(source):
                return source, None
            frame = cv2.imread(source)

        if frame is not None and (width > 0 and height > 0):
            frame = VideoPreprocessor.resize_with_padding(frame, (width, height))
        return source, frame
    except Exception:
        return source, None


def create_mp4_from_frames(frame_sources: list, output_path: str, fps: float = 10.0) -> bool:
    """Read a list of image sources (local paths OR Cloudinary URLs) and write an MP4.

    Args:
        frame_sources: Ordered list of local file paths or HTTP(S) URLs.
        output_path:   Absolute path for the resulting mp4 video.
        fps:           Frames per second for the output video.
    """
    if not frame_sources:
        return False

    # ── Probe first valid frame to get output dimensions ─────────────────────
    width, height = 0, 0
    for src in frame_sources[:5]:          # only probe the first few to save time
        _, probe = _load_frame(src, 0, 0)  # load at native size
        if probe is not None:
            h, w = probe.shape[:2]
            # Cap at 480p for fast CPU encoding
            if h > 480:
                scale  = 480.0 / float(h)
                w = int(w * scale)
                h = 480
            width, height = VideoPreprocessor.ensure_even_dimensions(w, h)
            break

    if width == 0 or height == 0:
        return False

    # ── Setup VideoWriter ─────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if os.name == "nt":
        fourcc = cv2.VideoWriter_fourcc(*"H264")
        out = cv2.VideoWriter(output_path, cv2.CAP_MSMF, fourcc, fps, (width, height))
    else:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(output_path, cv2.CAP_ANY, fourcc, fps, (width, height))

    if not out.isOpened():
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(output_path, cv2.CAP_ANY, fourcc, fps, (width, height))

    # ── Parallel download / decode of unique sources ──────────────────────────
    unique_sources = list(dict.fromkeys(frame_sources))
    frame_cache: dict = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as executor:
        futures = {executor.submit(_load_frame, src, width, height): src for src in unique_sources}
        for fut in concurrent.futures.as_completed(futures):
            src, frame = fut.result()
            frame_cache[src] = frame

    # ── Write frames in order ─────────────────────────────────────────────────
    frames_written = 0
    for src in frame_sources:
        frame = frame_cache.get(src)
        if frame is not None:
            out.write(frame)
            frames_written += 1

    out.release()
    return frames_written > 0
