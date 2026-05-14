# module1/src/video_capture/video_capture.py
import cv2
import logging
import os
import threading
import queue
import time

# ── Suppress FFmpeg's verbose TLS / socket / partial-file warnings ─────────────
# AV_LOG_FATAL = 8  (only show fatal errors, nothing below)
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "8")
# Also suppress via cv2 directly if supported
try:
    cv2.setLogLevel(0)           # 0 = silent in OpenCV >= 4.5
except Exception:
    pass

logger = logging.getLogger(__name__)


class VideoCapture:
    """Handles concurrent capture from multiple cameras (file / device / network stream)."""

    # Log reconnect messages at most once every this many consecutive failures
    _LOG_EVERY_N_FAILS = 5

    def __init__(self, camera_manager, buffer_size=2):
        self.camera_manager = camera_manager
        self.buffer_size = buffer_size
        self.frame_buffers = {}
        self.capture_threads = {}
        self.running = {}
        self._lock = threading.RLock()

    def _capture_loop(self, camera_id, _initial_unused):
        with self._lock:
            frame_queue = self.frame_buffers.get(camera_id)
            if not frame_queue:
                frame_queue = queue.Queue(maxsize=self.buffer_size)
                self.frame_buffers[camera_id] = frame_queue
            self.running[camera_id] = True
        try:
            target = self.camera_manager.resolve_target(camera_id)
        except Exception:
            target = None
        logger.info(f"[VideoCapture] Started capture for {camera_id}")

        fail_count = 0          # consecutive open-failures
        read_fail_count = 0     # consecutive read-failures (for throttled logging)

        while self.running.get(camera_id, False):
            try:
                target, api_pref = self.camera_manager.get_video_capture_args(camera_id)
            except Exception:
                self.stop_capture(camera_id)
                with self._lock:
                    self.running[camera_id] = False
                    self.capture_threads.pop(camera_id, None)
                break
            cap = None
            try:
                if api_pref is not None:
                    cap = cv2.VideoCapture(target, api_pref)
                elif isinstance(target, int):
                    for be in [getattr(cv2, 'CAP_DSHOW', None),
                               getattr(cv2, 'CAP_MSMF', None), 0]:
                        if be is None:
                            continue
                        cap = cv2.VideoCapture(target, be) if be else cv2.VideoCapture(target)
                        if cap.isOpened():
                            break
                else:
                    cap = cv2.VideoCapture(target)
            except Exception:
                cap = None

            if not cap or not cap.isOpened():
                # Exponential backoff: 1s → 2s → 4s → … capped at 30s
                wait = min(30.0, 2 ** min(fail_count, 4))
                if fail_count % self._LOG_EVERY_N_FAILS == 0:
                    logger.warning(
                        f"[VideoCapture] Cannot open stream for cam={camera_id} "
                        f"(attempt {fail_count+1}), retry in {wait:.0f}s"
                    )
                time.sleep(wait)
                fail_count += 1
                try:
                    target = self.camera_manager.resolve_target(camera_id)
                except Exception:
                    self.stop_capture(camera_id)
                    with self._lock:
                        self.running[camera_id] = False
                        self.capture_threads.pop(camera_id, None)
                    break
                continue

            # Successful open — reset open-failure counter
            fail_count = 0
            read_fail_count = 0

            src = self.camera_manager.get_source(camera_id) or ""
            is_local_file = self.camera_manager._is_file(src)

            while self.running.get(camera_id, False):
                ret, frame = cap.read()
                if not self.running.get(camera_id, False):
                    break
                if not ret:
                    if is_local_file:
                        # Seamless loop for local video files: cleanly reopen file handle to guarantee pristine Linux FFmpeg demuxer seek state
                        logger.info(f"[VideoCapture] Local video file stream loop finished for cam={camera_id}, re-initializing loop")
                        break

                    read_fail_count += 1
                    if read_fail_count == 1 or read_fail_count % self._LOG_EVERY_N_FAILS == 0:
                        logger.warning(
                            f"[VideoCapture] Read failed for cam={camera_id} "
                            f"(consecutive={read_fail_count}), reopening stream"
                        )
                    break   # exits inner loop → outer loop reopens

                # Successful read — reset read-failure counter
                read_fail_count = 0

                if frame_queue.full():
                    try:
                        frame_queue.get_nowait()
                    except Exception:
                        pass
                try:
                    frame_queue.put_nowait(frame)
                except Exception:
                    pass
                time.sleep(0.03)   # approx 30 fps

            cap.release()
            if not self.running.get(camera_id, False):
                break
            # Brief pause so FFmpeg TLS teardown can complete before reopening
            time.sleep(0.5)

            try:
                target = self.camera_manager.resolve_target(camera_id)
            except Exception:
                self.stop_capture(camera_id)
                with self._lock:
                    self.running[camera_id] = False
                    self.capture_threads.pop(camera_id, None)
                break

        with self._lock:
            self.running[camera_id] = False
            self.capture_threads.pop(camera_id, None)
        logger.info(f"[VideoCapture] Capture stopped for {camera_id}")

    def start_capture(self, camera_id):
        """Begin threaded video capture safely with thread synchronization."""
        with self._lock:
            if self.running.get(camera_id):
                logger.info(f"[VideoCapture] Capture thread already active for {camera_id}")
                return
            if str(camera_id) not in self.camera_manager.camera_sources:
                return
            self.running[camera_id] = True
            if camera_id not in self.frame_buffers:
                self.frame_buffers[camera_id] = queue.Queue(maxsize=self.buffer_size)
            try:
                self.camera_manager.connect_camera(camera_id)
            except Exception:
                pass
            thread = threading.Thread(
                target=self._capture_loop,
                args=(camera_id, None),
                daemon=True,
            )
            self.capture_threads[camera_id] = thread
            thread.start()

    def stop_capture(self, camera_id):
        """Stop capture and disconnect camera safely with thread synchronization."""
        with self._lock:
            if not self.running.get(camera_id, False):
                return
            self.running[camera_id] = False
            try:
                self.camera_manager.disconnect_camera(camera_id)
            except Exception:
                pass
            self.capture_threads.pop(camera_id, None)

    def get_frame(self, camera_id):
        """Fetch the latest frame from buffer (non-blocking)."""
        buf = self.frame_buffers.get(camera_id)
        if buf and not buf.empty():
            return buf.get()
        return None

    def get_camera_status(self, camera_id):
        return self.camera_manager.get_camera_status(camera_id)
