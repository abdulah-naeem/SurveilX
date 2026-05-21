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
        self.latest_frames = {}
        self.frame_counters = {}
        self._lock = threading.RLock()

    def _capture_loop(self, camera_id, _initial_unused):
        with self._lock:
            if camera_id not in self.latest_frames:
                self.latest_frames[camera_id] = None
            if camera_id not in self.frame_counters:
                self.frame_counters[camera_id] = 0
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
            is_local_file = self.camera_manager._is_file(src) or (isinstance(target, str) and self.camera_manager._is_file(target))
            norm_url = src.strip().lower()
            is_demo = "youtube.com" in norm_url or "youtu.be" in norm_url or "uploads/" in norm_url or "demo" in norm_url

            # Get video properties for time calculation and rate regulation
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not fps or fps <= 0.01 or fps > 100.0:
                fps = 30.0

            frame_count_total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            is_file_like = (frame_count_total > 0) or is_local_file or is_demo

            frames_in_loop = 0
            loop_start = time.time()

            while self.running.get(camera_id, False):
                ret, frame = cap.read()
                if not self.running.get(camera_id, False):
                    break
                
                if not ret:
                    logger.info(f"[VideoCapture] Video stream loop finished for cam={camera_id}, re-initializing loop by reopening")
                    break

                # Successful read — reset read-failure counter
                read_fail_count = 0
                frames_in_loop += 1

                # Calculate video_time of this frame reliably using frames_in_loop / fps for files/demos
                if is_file_like:
                    video_time = frames_in_loop / fps
                else:
                    pos_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                    if pos_msec > 0:
                        video_time = pos_msec / 1000.0
                    else:
                        video_time = frames_in_loop / fps

                with self._lock:
                    next_id = self.frame_counters.get(camera_id, 0) + 1
                    self.frame_counters[camera_id] = next_id
                    self.latest_frames[camera_id] = (frame, next_id, video_time)

                if frame_queue.full():
                    try:
                        frame_queue.get_nowait()
                    except Exception:
                        pass
                try:
                    frame_queue.put_nowait(frame)
                except Exception:
                    pass

                # Regulation of frame rate
                if is_file_like:
                    expected_time = loop_start + (frames_in_loop / fps)
                    now = time.time()
                    sleep_dur = expected_time - now
                    if sleep_dur > 0:
                        time.sleep(sleep_dur)
                    else:
                        time.sleep(0.001)  # Yield CPU control slightly
                else:
                    time.sleep(0.001)  # Non-blocking yield for live cameras

            cap.release()
            if not self.running.get(camera_id, False):
                break
            # Brief pause so FFmpeg TLS teardown can complete before reopening (only for network streams)
            if not is_local_file and not is_demo:
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
            if self.capture_threads.get(camera_id) == threading.current_thread():
                self.capture_threads.pop(camera_id, None)
        logger.info(f"[VideoCapture] Capture stopped for {camera_id}")

    def start_capture(self, camera_id):
        """Begin threaded video capture safely with thread synchronization."""
        with self._lock:
            old_thread = self.capture_threads.get(camera_id)
            if old_thread and old_thread.is_alive():
                if self.running.get(camera_id):
                    logger.info(f"[VideoCapture] Capture thread already active for {camera_id}")
                    return
                logger.info(f"[VideoCapture] Waiting for old capture thread of {camera_id} to exit...")
                self._lock.release()
                try:
                    old_thread.join(timeout=5.0)
                except Exception as join_err:
                    logger.warning(f"[VideoCapture] Error joining old thread: {join_err}")
                finally:
                    self._lock.acquire()
                
                if old_thread.is_alive():
                    logger.warning(f"[VideoCapture] Old capture thread for {camera_id} is still alive. Refusing to spawn duplicate thread to prevent frame shuffling.")
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

    def get_frame(self, camera_id, last_id=None):
        """Fetch the latest frame from buffer (non-blocking).
        If last_id is provided, returns (frame, frame_id, video_time) if a newer frame exists,
        otherwise returns (None, last_id, 0.0). If last_id is None, falls back to
        the traditional queue-based destructive fetch.
        """
        if last_id is not None:
            with self._lock:
                data = self.latest_frames.get(camera_id)
                if not data:
                    return None, last_id, 0.0
                if len(data) == 3:
                    frame, frame_id, video_time = data
                else:
                    frame, frame_id = data
                    video_time = 0.0
                
                if frame_id <= last_id:
                    return None, last_id, video_time
                return frame, frame_id, video_time

        # Fallback to queue-based destructive fetch
        buf = self.frame_buffers.get(camera_id)
        if buf and not buf.empty():
            return buf.get()
        return None

    def get_camera_status(self, camera_id):
        return self.camera_manager.get_camera_status(camera_id)
