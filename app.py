# ---------------- Authentication (simple in-memory) ----------------
from typing import Optional, Dict, Set, Any
from fastapi import Request, Response, Depends  # early import for type usage below
from fastapi import FastAPI, HTTPException
import secrets

SESSIONS: Dict[str, Dict[str, str]] = {}

app = FastAPI(title="SurveilX Web")

@app.on_event("startup")
async def startup_event():
    import torch
    import logging
    logger = logging.getLogger("uvicorn.info")
    logger.info(f"--- GPU DIAGNOSTICS ---")
    logger.info(f"Torch Version: {torch.__version__}")
    logger.info(f"CUDA Available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        logger.info(f"CUDA Device Count: {torch.cuda.device_count()}")
        logger.info(f"CUDA Device Name: {torch.cuda.get_device_name(0)}")
    else:
        logger.warning("CUDA IS NOT AVAILABLE. Running on CPU.")
    logger.info(f"-----------------------")
    asyncio.create_task(cleanup_worker())

async def cleanup_worker():
    """Background task that runs periodically to delete old frames from DB, Cloudinary, and Chroma."""
    import asyncio
    import os
    import time
    from config.settings import settings
    from src.storage import cloudinary_uploader
    from src.vector_store import chroma_store
    
    # Wait a bit before first run to not interfere with startup
    await asyncio.sleep(60)
    
    while True:
        try:
            retention_str = db_manager.get_setting("retention_days", "7")
            try:
                days = float(retention_str)
            except ValueError:
                days = 7.0
                
            assets = await asyncio.get_running_loop().run_in_executor(
                None, db_manager.get_and_delete_old_frames, days
            )
            
            if assets:
                logger.info(f"[Cleanup] Found {len(assets)} old frames. Deleting...")
                
                # We can batch delete from Chroma
                frame_ids = [a["frame_id"] for a in assets if a.get("frame_id")]
                if frame_ids and hasattr(chroma_store, 'delete_frames'):
                    try:
                        # Assuming delete_frames exists, otherwise we delete one by one
                        chroma_store.delete_frames(frame_ids)
                    except Exception as e:
                        logger.warning(f"Failed to delete from chroma: {e}")
                
                # Cloudinary cleanup
                for a in assets:
                    cdn_url = a.get("cloudinary_url")
                    if cdn_url and cdn_url.startswith("http"):
                        # Extract public_id from url
                        # e.g. https://res.cloudinary.com/.../surveilx/frame_xyz.jpg
                        try:
                            # Simple heuristic: take the path after upload/vXXX/
                            parts = cdn_url.split('/')
                            # Find index of upload
                            upload_idx = -1
                            for i, p in enumerate(parts):
                                if p == "upload":
                                    upload_idx = i
                                    break
                            if upload_idx != -1 and upload_idx + 2 < len(parts):
                                # Skip upload/ and version/
                                public_id_with_ext = "/".join(parts[upload_idx+2:])
                                public_id = os.path.splitext(public_id_with_ext)[0]
                                await asyncio.get_running_loop().run_in_executor(
                                    None, cloudinary_uploader.delete_asset, public_id
                                )
                        except Exception as e:
                            logger.warning(f"Could not extract public_id from {cdn_url}: {e}")
                            
                    # Local file cleanup
                    file_path = a.get("file_path")
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass
                            
                logger.info("[Cleanup] Finished deleting old frames.")
                
        except Exception as e:
            logger.error(f"[Cleanup] Error in cleanup worker: {e}")
            
        # Run once every 24 hours
        await asyncio.sleep(86400)

def extract_token(request: Request) -> Optional[str]:
    # 1) Authorization: Bearer <token>
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    # 2) Cookie 'auth'
    token = request.cookies.get("auth")
    if token:
        return token
    # 3) Query param 'token' (useful for EventSource which can't set headers)
    q = request.query_params.get("token")
    if q:
        return q
    return None

def require_any_role(request: Request) -> str:
    token = extract_token(request)
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return SESSIONS[token]["role"]

def require_admin(request: Request) -> str:
    role = require_any_role(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    return role

@app.post("/auth/login")
async def auth_login(payload: Dict[str, str], response: Response):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    expected_role = (payload.get("role") or "").strip()  # optional, used to force page-specific login
    # Fetch user from DB
    try:
        user = db_manager.get_user_by_username(username)
    except Exception:
        user = None
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Verify password
    try:
        ok = db_manager._pwd.verify(password, user.password_hash)
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Enforce role-specific login page if client sent an expected role
    if expected_role and user.role != expected_role:
        raise HTTPException(status_code=403, detail="Role mismatch for this login page")
    role = user.role
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": username, "role": role}
    # Also set HttpOnly cookie for convenience
    response.set_cookie(key="auth", value=token, httponly=True, secure=False, samesite="lax")
    return {"token": token, "role": role}

async def perform_logout_reset():
    """Restarts all cameras and resets demo timelines for a fresh start."""
    try:
        if detector:
            detector.reset_all_demos()
        
        # Capture instances are managed by video_capture (VideoCaptureManager)
        active_cids = list(video_capture.running.keys())
        for cid in active_cids:
            if video_capture.running.get(cid):
                try:
                    video_capture.stop_capture(cid)
                    video_capture.start_capture(cid)
                except Exception:
                    pass
        logger.info("System-wide Demo Reset: Triggered by logout.")
    except Exception as e:
        logger.error(f"Failed to reset during logout: {e}")

@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = extract_token(request)
    if token and token in SESSIONS:
        SESSIONS.pop(token, None)
    response.delete_cookie("auth")
    await perform_logout_reset()
    return {"ok": True}

@app.get("/auth/logout")
async def auth_logout_get(request: Request):
    token = extract_token(request)
    if token and token in SESSIONS:
        SESSIONS.pop(token, None)
    resp = RedirectResponse(url="/static/login-user.html", status_code=302)
    resp.delete_cookie("auth")
    await perform_logout_reset()
    return resp

@app.head("/auth/me")
@app.get("/auth/me")
async def auth_me(request: Request):
    token = extract_token(request)
    if token and token in SESSIONS:
        return {"username": SESSIONS[token]["username"], "role": SESSIONS[token]["role"]}
    raise HTTPException(status_code=401, detail="Unauthorized")
# module1/app.py
import asyncio
import logging
import os
import time
import shutil
import io
import tempfile

# ─ Suppress FFmpeg TLS/socket noise BEFORE cv2 is imported ────────────────────
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "8")   # AV_LOG_FATAL only
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

try:
    import psutil  # optional — used for CPU/RAM stats in health endpoint
except ImportError:
    psutil = None

from typing import Dict, List, Optional, Set, Any
from datetime import datetime, timedelta
from src.utils.time_utils import utcnow, utc_iso, parse_utc

import cv2
try:
    cv2.setLogLevel(0)   # 0 = LOG_LEVEL_SILENT (OpenCV ≥ 4.5)
except Exception:
    pass
import numpy as np

from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from src.video_capture.camera_manager import CameraManager
from src.video_capture.video_capture import VideoCapture
from src.metadata.db_manager import DatabaseManager
from src.metadata.extractor import MetadataExtractor
from src.vector_store import chroma_store
from src.vector_store.clip_embedder import embed_image_bgr, embed_text, init_clip
from src.detection.violence_detector import ViolenceDetector
from src.storage.cloudinary_uploader import (
    upload_frame_bytes as _cloudinary_upload_bytes,
    upload_video_bytes as _cloudinary_upload_video,
    is_enabled as _cloudinary_enabled,
)


# Camera infrastructure shared by endpoints
cam_manager = CameraManager(settings.CAMERA_SOURCES)
video_capture = VideoCapture(cam_manager)
db_manager = DatabaseManager()

extractors: Dict[str, MetadataExtractor] = {}
embed_tasks: Dict[str, asyncio.Task] = {}
BACKGROUND_TASKS: Set[asyncio.Task] = set()
CAM_EMBED_FPS: Dict[str, float] = {}
VIEWERS: Dict[str, int] = {}

# Violence detector (CNN-LSTM Fusion) — initialized in startup_event
detector: Optional[ViolenceDetector] = None

# Latest detection per camera for the dashboard
DETECTIONS: Dict[str, Dict[str, object]] = {}

# In-memory disabled users registry
DISABLED_USERS: Set[str] = set()

# Simple log broadcaster
class LogBroadcaster(logging.Handler):
    def __init__(self):
        super().__init__()
        self.queues: List[asyncio.Queue] = []

    def add_listener(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.queues.append(q)
        return q

    def remove_listener(self, q: asyncio.Queue):
        try:
            self.queues.remove(q)
        except ValueError:
            pass

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        for q in list(self.queues):
            # put_nowait; ignore full queues
            try:
                q.put_nowait(msg)
            except Exception:
                pass

broadcaster = LogBroadcaster()
formatter = logging.Formatter("[%(levelname)s] %(asctime)s %(name)s: %(message)s")
broadcaster.setFormatter(formatter)
class GuiLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        msg = record.getMessage()
        # Only show our app's 'frame <file> saved' logs in GUI
        if name.startswith("web") and msg.startswith("frame "):
            return True
        return False

        
broadcaster.addFilter(GuiLogFilter())
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Ensure terminal stays mostly quiet (WARNING+) for general logs
_console_warn = logging.StreamHandler()
_console_warn.setLevel(logging.WARNING)
_console_warn.setFormatter(formatter)
root_logger.addHandler(_console_warn)

# Add an INFO console handler only for uvicorn.* so 'running on ...' shows
class _UvicornOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Allow select uvicorn status lines; hide 'Uvicorn running on' and all access logs
        if not record.name.startswith("uvicorn.error"):
            return False
        m = record.getMessage()
        return (
            ("Started server process" in m)
            or ("Application startup complete" in m)
        )

_console_uvicorn_info = logging.StreamHandler()
_console_uvicorn_info.setLevel(logging.INFO)
_console_uvicorn_info.setFormatter(formatter)
_console_uvicorn_info.addFilter(_UvicornOnly())
root_logger.addHandler(_console_uvicorn_info)

# Module logger for web app
logger = logging.getLogger("web")
logger.setLevel(logging.INFO)
# Send to GUI only; keep out of terminal
logger.addHandler(broadcaster)
logger.propagate = False

# Tame noisy third-party loggers# Allow uvicorn logs to propagate so GUI can see them
for ln in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
    lg = logging.getLogger(ln)
    lg.propagate = True

for noisy in [
    "asyncio",
    "httpx",
    "sqlalchemy.engine",
]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Mount static web folder
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if not os.path.exists(WEB_DIR):
    os.makedirs(WEB_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
# Frames are stored on Cloudinary CDN — no local /processed mount needed

# ── Shared I/O thread pool — all cloud calls go through this instead of ad-hoc threads ──
# 8 workers: 3 parallel per-camera frame ops × max 3 cameras = 9 max concurrent, minus some
from concurrent.futures import ThreadPoolExecutor as _TPE
_IO_POOL = _TPE(max_workers=64, thread_name_prefix="surveilx-io")

# ── High-Priority Admin Pool: Reserved for UI-driven CRUD (Cameras, Users, Settings) ──
# This ensures that admin changes are processed immediately, even if the IO_POOL is saturated with frame uploads.
_ADMIN_POOL = _TPE(max_workers=16, thread_name_prefix="surveilx-admin")

def _run_in_pool(fn, *args):
    """Submit fn(*args) to the shared I/O pool; returns a concurrent.futures.Future."""
    return _IO_POOL.submit(fn, *args)

def _run_admin(fn, *args):
    """Submit fn(*args) to the high-priority admin pool."""
    return _ADMIN_POOL.submit(fn, *args)

def _refresh_cameras_from_db():
    """Load cameras from DB, update CameraManager sources, and align running capture/tasks."""
    try:
        cams = db_manager.list_cameras()
    except Exception:
        cams = []
    sources = {str(c.id): c.source_url for c in cams}
    cam_manager.update_sources(sources)
    # update embed fps map
    for c in cams:
        try:
            CAM_EMBED_FPS[str(c.id)] = float(getattr(c, 'embed_fps', 1) or 1)
        except Exception:
            CAM_EMBED_FPS[str(c.id)] = 1.0
    # Prewarm: auto-start captures for all enabled cameras so the dashboard loads fast
    active_ids = set(str(c.id) for c in cams if getattr(c, 'enabled', True))
    for cid in sorted(active_ids):
        try:
            if not video_capture.running.get(cid):
                video_capture.start_capture(cid)
                logger.info(f"Started camera {cid} (prewarm)")
            if cid not in extractors:
                extractors[cid] = MetadataExtractor(cid)
            if cid not in embed_tasks:
                embed_tasks[cid] = asyncio.create_task(capture_worker(cid))
        except Exception:
            logger.warning(f"Failed to start camera {cid}")
        # embeddings remain lazy; do not create embed_tasks here
    # Stop captures for cameras removed from DB
    existing_ids = set(str(c.id) for c in cams)
    for cid, running in list(video_capture.running.items()):
        if running and cid not in existing_ids:
            try:
                video_capture.stop_capture(cid)
            except Exception:
                pass
            if cid in embed_tasks:
                try:
                    embed_tasks[cid].cancel()
                except Exception:
                    pass
                embed_tasks.pop(cid, None)
            extractors.pop(cid, None)

@app.on_event("startup")
async def startup_event():
    # Seed default users if they don't exist
    try:
        db_manager.ensure_default_users()
    except Exception:
        pass
    
    # Preload model — ViolenceDetector now handles missing checkpoints gracefully
    # (demo-only mode) so we ALWAYS get a working detector object
    global detector
    logger.info("Loading ViolenceDetector model...")
    try:
        detector = ViolenceDetector(checkpoint_path=settings.VIOLENCE_CKPT_PATH)
        if detector.model:
            logger.info("ViolenceDetector: model + demo script loaded.")
        else:
            logger.warning("ViolenceDetector: demo-only mode (model checkpoint failed to load).")
    except Exception as e:
        logger.error(f"ViolenceDetector completely failed to initialize: {e}")
        detector = None

    # Preload CLIP model (for offline/online support)
    logger.info("Loading CLIP model...")
    try:
        init_clip()
        logger.info("CLIP model loaded.")
    except Exception as e:
        logger.error(f"Failed to load CLIP model: {e}")

    # Explicitly clear sessions on startup (User Request)
    SESSIONS.clear()
    logger.info("Session storage initialized (all previous sessions cleared).")

    # Initialize cameras from DB
    _refresh_cameras_from_db()

@app.on_event("shutdown")
async def shutdown_event():
    # Stop all cameras
    for cid in cam_manager.discover_cameras():
        try:
            video_capture.stop_capture(cid)
            logger.info(f"Stopped camera {cid}")
        except Exception:
            pass
    # Cancel workers
    for cid, task in list(embed_tasks.items()):
        try:
            task.cancel()
        except Exception:
            pass
    embed_tasks.clear()

async def capture_worker(camera_id: str):
    """Per-camera worker: runs at capture FPS, parallelises Cloudinary/DB/Chroma ops."""
    frame_count = 0
    last_tick = 0.0
    last_store_ts = 0.0
    loop = asyncio.get_running_loop()   # Cache once per worker lifetime

    # ─ Settings cache: avoid hitting Neon on every frame ────────────────────
    _v_threshold     = 0.5
    _v_threshold_ts  = 0.0
    _SETTINGS_TTL    = 5.0   # re-read settings at most every 5 seconds
    try:
        task = asyncio.current_task()
        if task:
            BACKGROUND_TASKS.add(task)
    except Exception:
        pass

    if camera_id not in extractors:
        extractors[camera_id] = MetadataExtractor(camera_id)
    while True:
        try:
            now = time.time()
            # throttle ~15 Hz (for smoother context video playback)
            if (now - last_tick) < 0.066:
                await asyncio.sleep(0.01)
                continue
            frame = video_capture.get_frame(camera_id)
            last_tick = now
            if frame is None:
                await asyncio.sleep(0.01)
                continue
            detection = getattr(extractors[camera_id], 'last_detection', {})
            if detector:
                last_yolo_ts = getattr(extractors[camera_id], 'last_yolo_ts', 0)
                source_url = cam_manager.get_source(camera_id)
                # If it's a demo patch video, bypass the throttle (Real-time scripting)
                is_patch = hasattr(detector, 'demo_results') and detector._normalize_url(source_url) in detector.demo_results

                if (now - last_yolo_ts) >= 0.05 or is_patch:
                    extractors[camera_id].last_yolo_ts = now

                    # ─ Refresh settings cache if stale (TTL = 5s) ─────────────────
                    if (now - _v_threshold_ts) >= _SETTINGS_TTL:
                        try:
                            _v_threshold = float(db_manager.get_setting("violence_threshold", "0.5") or 0.5)
                        except Exception:
                            _v_threshold = 0.5
                        _v_threshold_ts = now

                    # ─ Bind source_url as default arg to avoid lambda closure bug ───
                    _src = source_url  # local copy
                    try:
                        detection = await loop.run_in_executor(
                            _IO_POOL,
                            lambda cam=camera_id, frm=frame, thr=_v_threshold, src=_src: \
                                detector.predict(cam, frame_bgr=frm, confidence_threshold=thr, source_url=src)
                        )
                        extractors[camera_id].last_detection = detection
                    except Exception as _det_err:
                        logger.warning(f"[{camera_id}] Detection error: {_det_err}")
            
            # cache latest detection for UI
            try:
                # Store all required information for the UI to display probabilities
                DETECTIONS[str(camera_id)] = {
                    "label": detection.get("label"),
                    "score": detection.get("score"),
                    "class_probs": detection.get("class_probs"),
                    "ts": utc_iso(),
                    "is_alert": detection.get("is_alert", False)
                }
            except Exception:
                pass
            # Time-based sampling using global detection_fps
            try:
                fps = float(db_manager.get_setting("detection_fps", "15.0"))
            except ValueError:
                fps = 15.0
            do_store = False
            if fps > 0:
                interval = 1.0 / max(0.1, fps)
                if (now - last_store_ts) >= interval:
                    do_store = True
            if do_store:
                ts = utcnow()
                ts_str = ts.strftime('%Y%m%d_%H%M%S')
                filename = f"{camera_id}_{ts_str}_{frame_count}.jpg"
                import uuid as _uuid
                short_id = _uuid.uuid4().hex[:6]
                chroma_id = f"{camera_id}:{ts_str}:{frame_count}_{short_id}"

                # ─ Capture frame snapshot for background threads (avoid closure over mutable) ─
                _frame_snap = frame.copy()

                # ─ Extract metadata (sync, cheap) ────────────────────────────────
                md_extra = {"frame_index": frame_count}
                if detection:
                    md_extra.update({
                        "violence_label": detection.get("label"),
                        "violence_score": detection.get("score"),
                        "class_probs": detection.get("class_probs"),
                    })
                try:
                    md = extractors[camera_id].extract(_frame_snap, extra=md_extra)
                except Exception:
                    md = {"timestamp": ts, "resolution": "", "metadata_json": {}, "camera_location": None}

                pk_val = None
                try:
                    pk_val = int(camera_id)
                except ValueError:
                    pass
                loc = md.get("camera_location") or settings.CAMERA_LOCATIONS.get(camera_id)

                # Op-C: Decide if we embed this frame (sync check)
                try:
                    cam_embed_fps = float(db_manager.get_setting("embed_fps", "1.0"))
                except ValueError:
                    cam_embed_fps = 1.0
                embed_interval = 1.0 / max(0.1, cam_embed_fps)
                do_embed = False
                last_embed = getattr(extractors[camera_id], 'last_embed_ts', 0)
                if (now - last_embed) >= embed_interval:
                    do_embed = True
                    extractors[camera_id].last_embed_ts = now

                # Fire and forget storage task to keep capture loop moving at 15 FPS
                async def _task_wrapper(_f=_frame_snap, _ts=ts, _tss=ts_str, _fc=frame_count, _cid=chroma_id, _md=md, _det=detection, _pk=pk_val, _lo=loc, _de=do_embed):
                    try:
                        # ─ 1. Encode JPEG in-process (CPU-bound, fast) ────────────────────────
                        try:
                            import cv2 as _cv2
                            h, w = _f.shape[:2]
                            scale = min(1.0, 720.0 / float(h)) if h > 0 else 1.0
                            small = _cv2.resize(_f, (int(w * scale), int(h * scale)))
                            ok, buf = _cv2.imencode('.jpg', small, [int(_cv2.IMWRITE_JPEG_QUALITY), 60])
                            jpg_bytes = buf.tobytes() if ok else None
                        except Exception:
                            jpg_bytes = None

                        if not jpg_bytes or not _cloudinary_enabled():
                            return

                        # ─ 2. Fire all 3 cloud ops ──────────────────────────────────────
                        # Op-A: Cloudinary upload
                        cdn_url = await loop.run_in_executor(_IO_POOL, _cloudinary_upload_bytes, jpg_bytes, f"surveilx/{camera_id}/{_cid.replace(':', '_')}")
                        
                        # Op-B: DB insert
                        def _op_db():
                            db_manager.insert_frame_pipeline(
                                camera_id=camera_id,
                                camera_pk=_pk,
                                frame_id=_cid,
                                timestamp=_md["timestamp"],
                                camera_location=_lo,
                                resolution=_md.get("resolution"),
                                metadata_json={
                                    **(_md.get("metadata_json") or {}),
                                    "file_path": "",
                                    "frame_index": _fc,
                                    **( {"cloudinary_url": cdn_url} if cdn_url else {} ),
                                },
                                violence_label=_det.get("label") if _det else None,
                                violence_score=_det.get("score") if _det else None,
                                detections=_det.get("class_probs") if _det else {},
                                embedding={"chroma_id": _cid},
                            )

                        # Op-C: CLIP embed + Chroma upsert
                        def _op_embed():
                            chroma_meta = {
                                "camera_id":       camera_id,
                                "camera_location": settings.CAMERA_LOCATIONS.get(camera_id, camera_id),
                                "timestamp_iso":   utc_iso(_ts),
                                "resolution":      _md.get("resolution"),
                                "frame_index":     _fc,
                                "cloudinary_url":  cdn_url or "",
                                "violence_label":  _det.get("label") if _det else None,
                                "violence_score":  _det.get("score") if _det else None,
                                "_str_id":         _cid,
                            }
                            real_embedding = embed_image_bgr(_f)
                            chroma_store.upsert_frame(
                                frame_id=_cid,
                                metadata=chroma_meta,
                                document=f"Frame {_fc} from {camera_id} at {_tss}",
                                embedding=real_embedding,
                            )

                        parallel_ops = [loop.run_in_executor(_IO_POOL, _op_db)]
                        if _de:
                            parallel_ops.append(loop.run_in_executor(_IO_POOL, _op_embed))

                        await asyncio.gather(*parallel_ops, return_exceptions=True)
                        logger.info(f"[{camera_id}] frame {_tss}_{_fc} stored")
                    except Exception as e:
                        logger.warning(f"[{camera_id}] Storage task failed: {e}")

                asyncio.create_task(_task_wrapper())
                last_store_ts = now

            frame_count += 1
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            break
        except Exception:
            # Avoid verbose tracebacks
            logger.warning(f"Worker error for {camera_id}")
            await asyncio.sleep(0.05)

    # Cleanup background task tracking
    try:
        task = asyncio.current_task()
        if task and task in BACKGROUND_TASKS:
            BACKGROUND_TASKS.remove(task)
    except Exception:
        pass

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Require authentication; if missing, go to login page
    tok = extract_token(request)
    if not tok or tok not in SESSIONS:
        # redirect to static login page
        return RedirectResponse(url="/static/login-user.html", status_code=302)
    try:
        with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h3>UI not found</h3>")

@app.get("/cameras")
async def list_cameras():
    try:
        cams = db_manager.list_cameras(only_enabled=True)
        out = [{"id": str(c.id), "name": c.name or str(c.id)} for c in cams]
        # ensure workers running so detections are available
        for c in cams:
            cid = str(c.id)
            if not video_capture.running.get(cid):
                try:
                    video_capture.start_capture(cid)
                except Exception:
                    pass
            if cid not in extractors:
                extractors[cid] = MetadataExtractor(cid)
            if cid not in embed_tasks:
                embed_tasks[cid] = asyncio.create_task(capture_worker(cid))
    except Exception:
        out = []
    return JSONResponse({"cameras": out})

@app.get("/stream/{camera_id}")
async def stream_camera(camera_id: str):
    # Accept numeric ID as string
    try:
        cams = {str(c.id): c for c in db_manager.list_cameras(only_enabled=True)}
    except Exception:
        cams = {}
    if camera_id not in cams:
        raise HTTPException(status_code=404, detail="Unknown camera")

    boundary = "frame"

    # Ensure capture (and embedding worker) lazily start on first viewer
    cid = str(camera_id)
    if not video_capture.running.get(cid):
        try:
            video_capture.start_capture(cid)
            logger.info(f"Started camera {cid} (lazy)")
        except Exception:
            logger.warning(f"Failed to start camera {cid}")
    if cid not in extractors:
        extractors[cid] = MetadataExtractor(cid)
    if cid not in embed_tasks:
        embed_tasks[cid] = asyncio.create_task(capture_worker(cid))

    async def frame_generator():
        # Reset demo script if this is the first viewer
        if VIEWERS.get(cid, 0) == 0:
            if detector and hasattr(detector, 'reset_demo'):
                detector.reset_demo(cid)
        
        VIEWERS[cid] = VIEWERS.get(cid, 0) + 1
        while True:
            frame = video_capture.get_frame(camera_id)
            if frame is None:
                await asyncio.sleep(0.03)
                continue
            


            # Overlay timestamp and camera location on the frame
            try:
                overlay = frame.copy()
                h, w = frame.shape[:2]
                ts = utcnow().strftime('%Y-%m-%d %H:%M:%S')
                location = settings.CAMERA_LOCATIONS.get(camera_id, camera_id)
                line1 = f"{location}"
                line2 = f"{ts}"
                # Box background
                box_w = min(w, 420)
                box_h = 75 # slightly taller to fit detection result
                cv2.rectangle(overlay, (10, 10), (10 + box_w, 10 + box_h), (0, 0, 0), thickness=-1)
                # Blend for translucency
                alpha = 0.4
                frame = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0)
                # Text
                cv2.putText(frame, line1, (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2, cv2.LINE_AA)
                cv2.putText(frame, line2, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2, cv2.LINE_AA)
                
                # Add Detection Results to Overlay
                det = DETECTIONS.get(cid, {})
                if det and det.get("label"):
                    lbl = str(det["label"])
                    scr = float(det.get("score") or 0)
                    det_text = f"Result: {lbl} ({scr:.1%})"
                    color = (0, 0, 255) if det.get("is_alert") else (0, 255, 0)
                    cv2.putText(frame, det_text, (20, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
            except Exception:
                pass
            ok, buf = cv2.imencode('.jpg', frame)
            if not ok:
                await asyncio.sleep(0.01)
                continue
            jpg_bytes = buf.tobytes()
            yield (
                b"--" + boundary.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpg_bytes)).encode() + b"\r\n\r\n"
                + jpg_bytes + b"\r\n"
            )
            await asyncio.sleep(0.01) # Reduced for snappiness

    async def stream_with_teardown():
        try:
            async for chunk in frame_generator():
                yield chunk
        finally:
            # Decrement viewer count and stop capture if no viewers remain
            VIEWERS[cid] = max(0, VIEWERS.get(cid, 1) - 1)
            if VIEWERS.get(cid, 0) == 0:
                try:
                    if video_capture.running.get(cid):
                        video_capture.stop_capture(cid)
                except Exception:
                    pass

    return StreamingResponse(stream_with_teardown(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")

@app.get("/logs")
async def stream_logs(role: str = Depends(require_admin)):
    q = broadcaster.add_listener()

    async def event_stream():
        try:
            while True:
                msg = await q.get()
                data = f"data: {msg}\n\n"
                yield data.encode()
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.remove_listener(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# -------- Admin: Users management --------
@app.get("/admin/users")
async def admin_list_users(role: str = Depends(require_admin)):
    sess = db_manager.get_session()
    try:
        from src.metadata.models import AuthUser
        users = sess.query(AuthUser).all()
        out = []
        for u in users:
            out.append({
                "username": u.username,
                "role": u.role,
                "disabled": u.username in DISABLED_USERS,
                "created_at": getattr(u, "created_at", None)
            })
        return {"users": out}
    finally:
        sess.close()

@app.post("/admin/users")
async def admin_create_user(payload: dict[str, str], role: str = Depends(require_admin)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    urole = (payload.get("role") or "user").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    try:
        loop = asyncio.get_running_loop()
        u = await loop.run_in_executor(_ADMIN_POOL, db_manager.create_user, username, password, urole)
        return {"ok": True, "user": {"username": u.username, "role": u.role}}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/admin/users/reset_password")
async def admin_reset_password(payload: Dict[str, str], role: str = Depends(require_admin)):
    username = (payload.get("username") or "").strip()
    new_password = payload.get("new_password") or ""
    if not username or not new_password:
        raise HTTPException(status_code=400, detail="username and new_password required")
    sess = db_manager.get_session()
    try:
        from src.metadata.models import AuthUser
        def _reset():
            u = sess.query(AuthUser).filter(AuthUser.username == username).first()
            if not u:
                return None
            u.password_hash = db_manager._pwd.hash(new_password)
            sess.commit()
            return True
        
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(_ADMIN_POOL, _reset)
        if res is None:
            raise HTTPException(status_code=404, detail="user not found")
        return {"ok": True}
    finally:
        sess.close()

@app.post("/admin/users/disable")
async def admin_disable_user(payload: Dict[str, str], role: str = Depends(require_admin)):
    username = (payload.get("username") or "").strip()
    disabled = bool(payload.get("disabled", True))
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if disabled:
        DISABLED_USERS.add(username)
        # force logout any sessions
        for tok, info in list(SESSIONS.items()):
            if info.get("username") == username:
                SESSIONS.pop(tok, None)
    else:
        DISABLED_USERS.discard(username)
    return {"ok": True, "disabled": username in DISABLED_USERS}

@app.post("/admin/users/force_logout")
async def admin_force_logout(payload: Dict[str, str], role: str = Depends(require_admin)):
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    count = 0
    for tok, info in list(SESSIONS.items()):
        if info.get("username") == username:
            SESSIONS.pop(tok, None)
            count += 1
    return {"ok": True, "sessions_ended": count}

# -------- Admin: Analytics summary --------
@app.get("/admin/analytics/summary")
async def admin_analytics_summary(role: str = Depends(require_admin)):
    from src.metadata.models import VideoMetadata, VideoStream
    sess = db_manager.get_session()
    try:
        total_streams = sess.query(VideoStream).count()
        since = utcnow().timestamp() - 24*3600
        # count last 24h by timestamp if available
        recent = sess.query(VideoMetadata).count()
        # use DB cameras if available
        try:
            total_cameras = len(db_manager.list_cameras())
        except Exception:
            total_cameras = len(settings.CAMERA_SOURCES)
        return {
            "total_cameras": total_cameras,
            "streams": total_streams,
            "events_24h": recent,
            "active_users": max(1, len({v.get("username") for v in SESSIONS.values()})),
            "storage_usage": None,
            "critical_alerts": 0,
        }
    finally:
        sess.close()

# -------- System health (any authenticated) --------
@app.get("/admin/health")
async def admin_health(role: str = Depends(require_any_role)):
    if psutil is None:
        return {"cpu": None, "ram": None, "disk": None, "net": None}
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()._asdict()
        disk = psutil.disk_usage("/")._asdict()
        net = psutil.net_io_counters()._asdict()
        return {"cpu": cpu, "ram": mem, "disk": disk, "net": net}
    except Exception:
        return {"cpu": None, "ram": None, "disk": None, "net": None}

# -------- Admin: Cameras CRUD --------
@app.get("/admin/cameras")
async def admin_list_cameras(role: str = Depends(require_admin)):
    cams = db_manager.list_cameras()
    return {"cameras": [
        {"id": c.id, "name": c.name, "source_url": c.source_url, "zone": c.zone, "enabled": c.enabled}
        for c in cams
    ]}

@app.get("/api/system/limits")
async def system_limits(role: str = Depends(require_any_role)):
    """Return camera capacity info so the UI can disable the Add Camera button when at limit."""
    try:
        max_cams = int(db_manager.get_setting("max_cameras", "3") or 3)
    except Exception:
        max_cams = 3
    current = len(db_manager.list_cameras())
    return {
        "max_cameras": max_cams,
        "current_cameras": current,
        "at_limit": current >= max_cams,
        "slots_remaining": max(0, max_cams - current),
        "detection_fps": db_manager.get_setting("detection_fps", "15"),
        "clip_fps": db_manager.get_setting("clip_fps", "10"),
        "embed_fps": db_manager.get_setting("embed_fps", "1"),
    }

@app.post("/admin/cameras")
async def admin_create_camera(payload: Dict, role: str = Depends(require_admin)):
    name = (payload.get("name") or "").strip()
    source_url = (payload.get("source_url") or "").strip()
    zone = (payload.get("zone") or "").strip() or None
    enabled = bool(payload.get("enabled", True))
    embed_fps = payload.get("embed_fps")
    if not name or not source_url:
        raise HTTPException(status_code=400, detail="name and source_url required")

    # ── Camera limit enforcement ───────────────────────────────────────────
    try:
        max_cams = int(db_manager.get_setting("max_cameras", "3") or 3)
    except Exception:
        max_cams = 3
    current_count = len(db_manager.list_cameras())
    if current_count >= max_cams:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Camera limit reached ({current_count}/{max_cams}). "
                f"Remove an existing camera or ask your admin to raise the "
                f"'max_cameras' setting in Settings."
            )
        )

    # Apply default embed_fps from settings when caller didn't supply one
    if embed_fps is None:
        try:
            embed_fps = float(db_manager.get_setting("detection_fps", "15.0"))
        except Exception:
            embed_fps = 15.0

    loop = asyncio.get_running_loop()
    cam = await loop.run_in_executor(_ADMIN_POOL, lambda: db_manager.create_camera(
        name=name, source_url=source_url, zone=zone, enabled=enabled, embed_fps=embed_fps
    ))

    # reflect source and embed_fps in runtime maps; start capture if enabled (embeddings lazy)
    try:
        cam_manager.set_source(cam.id, cam.source_url)
        try:
            CAM_EMBED_FPS[str(cam.id)] = float(getattr(cam, 'embed_fps', 1) or 1)
        except Exception:
            CAM_EMBED_FPS[str(cam.id)] = 1.0
        if cam.enabled and not video_capture.running.get(str(cam.id)):
            try:
                video_capture.start_capture(str(cam.id))
                logger.info(f"Started camera {cam.id} (create)")
            except Exception:
                logger.warning(f"Failed to start camera {cam.id}")
    except Exception:
        pass
    return {"ok": True, "camera": {"id": cam.id, "name": cam.name, "source_url": cam.source_url, "zone": cam.zone, "enabled": cam.enabled}}

@app.patch("/admin/cameras/{camera_id}")
async def admin_update_camera(camera_id: int, payload: Dict, role: str = Depends(require_admin)):
    cid = str(camera_id)
    # Track old source to detect changes
    old_source = cam_manager.get_source(cid)

    loop = asyncio.get_running_loop()
    cam = await loop.run_in_executor(_ADMIN_POOL, lambda: db_manager.update_camera(
        camera_id,
        name=payload.get("name"),
        source_url=payload.get("source_url"),
        zone=payload.get("zone"),
        enabled=payload.get("enabled"),
        embed_fps=payload.get("embed_fps")
    ))
    
    if not cam:
        raise HTTPException(status_code=404, detail="camera not found")
    
    # Update in-memory managers
    try:
        cam_manager.set_source(cid, cam.source_url)
        try:
            CAM_EMBED_FPS[cid] = float(getattr(cam, 'embed_fps', 1) or 1)
        except Exception:
            CAM_EMBED_FPS[cid] = 1.0

        # Runtime state reflection
        is_running = video_capture.running.get(cid)
        source_changed = (old_source != cam.source_url)

        if cam.enabled:
            # If not running, start it
            if not is_running:
                video_capture.start_capture(cid)
                logger.info(f"Started camera {cid} (update/enable)")
            # If running but source changed, restart it
            elif source_changed:
                video_capture.stop_capture(cid)
                video_capture.start_capture(cid)
                logger.info(f"Restarted camera {cid} (source changed)")

        else:
            # If disabled and currently running, stop it
            if is_running:
                video_capture.stop_capture(cid)
                logger.info(f"Stopped camera {cid} (update/disable)")
            # Cleanup background tasks if fully disabled
            if cid in embed_tasks:
                try:
                    embed_tasks[cid].cancel()
                except Exception:
                    pass
                embed_tasks.pop(cid, None)
                extractors.pop(cid, None)
                
    except Exception as e:
        logger.warning(f"Runtime update failed for camera {cid}: {e}")
    
    return {"ok": True}

@app.delete("/admin/cameras/{camera_id}")
async def admin_delete_camera(camera_id: int, role: str = Depends(require_admin)):
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(_ADMIN_POOL, db_manager.delete_camera, camera_id)
    if not ok:
        raise HTTPException(status_code=404, detail="camera not found")
    # reflect in runtime
    try:
        cid = str(camera_id)
        if video_capture.running.get(cid):
            video_capture.stop_capture(cid)
        if cid in embed_tasks:
            try:
                embed_tasks[cid].cancel()
            except Exception:
                pass
            embed_tasks.pop(cid, None)
            extractors.pop(cid, None)
        cam_manager.remove_source(camera_id)
    except Exception:
        pass
    return {"ok": True}

@app.post("/admin/cameras/{camera_id}/test")
async def admin_test_camera(camera_id: int, role: str = Depends(require_admin)):
    # Lightweight placeholder test
    return {"ok": True}

@app.get("/api/detections")
async def api_detections(role: str = Depends(require_any_role)):
    """Return latest detection per camera for dashboard."""
    return {"detections": {k: {kk: vv for kk, vv in v.items() if kk != "overlay_jpeg"} for k, v in DETECTIONS.items()}}

# ---- Admin: Upload local video file for camera (store on server and return path) ----
@app.post("/admin/upload_video")
async def admin_upload_video(file: UploadFile = File(...), role: str = Depends(require_admin)):
    # Choose uploads directory relative to working dir
    uploads_dir = pathlib.Path("uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize filename
    name = pathlib.Path(file.filename or "video.mp4").name
    dest = uploads_dir / name
    # If exists, make unique
    i = 1
    base = dest.stem
    suffix = dest.suffix
    while dest.exists():
        dest = uploads_dir / f"{base}_{i}{suffix}"
        i += 1
    data = await file.read()
    dest.write_bytes(data)
    # Return absolute path so backend can open file
    return {"path": str(dest.resolve())}

# ---- Admin: Probe available capture devices (indices) ----
@app.get("/admin/devices")
async def admin_list_devices(role: str = Depends(require_admin)):
    import cv2
    found = []
    seen = set()
    # On Windows try DirectShow and MSMF explicitly; then default
    backends = []
    try:
        backends.extend([cv2.CAP_DSHOW, cv2.CAP_MSMF])
    except Exception:
        pass
    backends.append(0)  # auto
    # Probe a wider range conservatively
    for i in range(0, 12):
        for be in backends:
            try:
                cap = cv2.VideoCapture(i, be) if be else cv2.VideoCapture(i)
                ok = cap.isOpened()
                # also try to read one frame to confirm it works
                if ok:
                    ret, _ = cap.read()
                    ok = ret or ok
                if ok and i not in seen:
                    seen.add(i)
                    found.append({"index": i, "name": f"Camera {i}"})
            except Exception:
                pass
            finally:
                try:
                    cap.release()
                except Exception:
                    pass
    return {"devices": found}

# -------- Admin: Delete user --------
@app.post("/admin/users/delete")
async def admin_delete_user(payload: Dict[str, str], role: str = Depends(require_admin)):
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    sess = db_manager.get_session()
    try:
        from src.metadata.models import AuthUser
        def _delete():
            u = sess.query(AuthUser).filter(AuthUser.username == username).first()
            if not u:
                return False
            sess.delete(u)
            sess.commit()
            return True
            
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(_ADMIN_POOL, _delete)
        if not ok:
            raise HTTPException(status_code=404, detail="user not found")
        # clean sessions too
        for tok, info in list(SESSIONS.items()):
            if info.get("username") == username:
                SESSIONS.pop(tok, None)
        return {"ok": True}
    finally:
        sess.close()

# -------- Events feed (any authenticated) --------
@app.get("/events/feed")
async def events_feed(limit: int = 20, role: str = Depends(require_any_role)):
    from src.metadata.models import VideoMetadata
    sess = db_manager.get_session()
    try:
        q = sess.query(VideoMetadata).order_by(VideoMetadata.id.desc()).limit(max(1, min(limit, 200)))
        out = []
        for vm in q:
            out.append({
                "id": vm.id,
                "frame_id": vm.frame_id,
                "timestamp": str(vm.timestamp),
                "camera_location": vm.camera_location,
                "resolution": vm.resolution,
                "violence_label": vm.violence_label,
                "violence_score": vm.violence_score,
            })
        return {"events": out}
    finally:
        sess.close()

# -------- Map cameras (any authenticated) --------
@app.get("/map/cameras")
async def map_cameras(role: str = Depends(require_any_role)):
    cams = []
    try:
        for c in db_manager.list_cameras(only_enabled=True):
            cams.append({"id": c.id, "name": c.name, "zone": c.zone})
    except Exception:
        # fallback to static
        cams = [
            {"id": cid, "name": settings.CAMERA_LOCATIONS.get(cid, cid), "zone": settings.CAMERA_LOCATIONS.get(cid, cid)}
            for cid in cam_manager.discover_cameras()
        ]
    return {"cameras": cams}

# -------- Admin: Global Settings --------
@app.get("/api/admin/settings")
async def admin_get_settings(role: str = Depends(require_admin)):
    settings_list = db_manager.list_settings()
    return {"settings": [
        {"key": s.key, "value": s.value, "description": s.description, "updated_at": str(s.updated_at)}
        for s in settings_list
    ]}

@app.post("/api/admin/settings")
async def admin_update_settings(payload: Dict[str, str], role: str = Depends(require_admin)):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_ADMIN_POOL, lambda: [db_manager.set_setting(k, v) for k, v in payload.items()])
    return {"ok": True}

# -------- Embedding/Chroma stats endpoints --------
# -------- Chroma-backed embedding/search endpoints --------

@app.get("/api/embeddings/stats")
async def embeddings_stats(role: str = Depends(require_any_role)):
    """Return total frame count stored in Chroma."""
    try:
        from src.vector_store.chroma_store import get_collection
        collection = get_collection()
        return JSONResponse({"count": collection.count()})
    except Exception as e:
        logger.warning(f"Chroma stats failed: {e}")
        return JSONResponse({"count": 0})


@app.get("/api/debug/detection")
async def debug_detection(role: str = Depends(require_admin)):
    """Admin-only: show live detection state, demo-patch match, and last detections."""
    import time as _t
    out = {"cameras": {}, "detections": dict(DETECTIONS), "demo_loaded": False}

    if detector:
        out["demo_loaded"] = bool(getattr(detector, 'demo_results', {}))
        out["demo_keys"]   = list(getattr(detector, 'demo_results', {}).keys())[:5]

        for cam_id in list(cam_manager.camera_sources.keys()):
            src  = cam_manager.get_source(cam_id) or ""
            norm = detector._normalize_url(src)
            is_patch = norm in (getattr(detector, 'demo_results') or {})
            state = getattr(detector, '_state', {}).get(str(cam_id), {})
            elapsed = _t.time() - state.get("start_time", _t.time()) if state else 0

            active_interval = None
            if is_patch:
                intervals = detector.demo_results.get(norm, [])
                for iv in (intervals if isinstance(intervals, list) else []):
                    if iv.get("start", 0) <= elapsed < iv.get("end", 9999):
                        active_interval = iv
                        break

            out["cameras"][cam_id] = {
                "source_url":      src[:80],
                "normalized_url":  norm[:80],
                "is_demo_patch":   is_patch,
                "elapsed_sec":     round(elapsed, 1),
                "active_interval": active_interval,
                "last_detection":  getattr(
                    extractors.get(cam_id), 'last_detection', None
                ) if cam_id in extractors else None,
            }
    return JSONResponse(out)


@app.get("/api/embeddings/search_text")
async def embeddings_search_text(query: str, k: int = 12, role: str = Depends(require_any_role)):
    """Semantic NLP search: CLIP text embedding → Chroma cosine similarity → ranked frames.

    Flow:
      1. Encode the text query with CLIP (same model used when indexing frames).
      2. Search Chroma for the nearest neighbour CLIP image vectors.
      3. Return results ranked by cosine similarity (0..1), filtered by MIN_SCORE.

    Only frames that were indexed with a real CLIP vector will appear.
    Frames captured while the CLIP model was unavailable (zero vectors) are
    automatically ranked below the threshold and excluded.
    """
    MIN_SCORE = 0.15   # Minimum cosine similarity to surface a result (0..1)

    if not query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    # Step 1: Encode the query with CLIP (offloaded to thread pool — CPU/GPU bound)
    try:
        text_vec = await asyncio.get_running_loop().run_in_executor(
            _IO_POOL, lambda q=query: embed_text(q)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CLIP text encoding failed: {e}")

    # Step 2: Vector similarity search in Chroma
    try:
        from src.vector_store.chroma_store import get_collection
        collection = get_collection()
        res = collection.query(
            query_embeddings=[text_vec],
            n_results=max(1, min(k, 50)),
            include=["metadatas", "distances"]
        )

        results = []
        if res and res.get('ids') and res['ids'][0]:
            for i, _id in enumerate(res['ids'][0]):
                distance = res['distances'][0][i]
                similarity = round(1.0 - distance, 4)
                if similarity < MIN_SCORE:
                    continue
                
                payload = res['metadatas'][0][i] or {}
                cdn_url = payload.get("cloudinary_url", "")
                
                # Skip frames that have no thumbnail
                if not cdn_url or not cdn_url.startswith("http"):
                    continue

                results.append({
                    "id":             payload.get("_str_id", _id),
                    "score":          similarity,
                    "camera_id":      payload.get("camera_id"),
                    "timestamp_iso":  payload.get("timestamp_iso"),
                    "cloudinary_url": cdn_url,
                    "violence_label": payload.get("violence_label"),
                    "violence_score": payload.get("violence_score"),
                })

        return JSONResponse({
            "results": results,
            "query":   query,
            "count":   len(results),
        })

    except Exception as e:
        logger.warning(f"Chroma text search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Vector search failed: {e}")

@app.post("/api/embeddings/search_image")
async def embeddings_search_image(file: UploadFile = File(...), k: int = 12, role: str = Depends(require_any_role)):
    """Semantic image search — find visually similar frames via CLIP + Chroma."""
    try:
        raw = await file.read()
        from PIL import Image as _PIL
        import io as _io
        img = _PIL.open(_io.BytesIO(raw)).convert("RGB")
        arr_bgr = np.array(img)[:, :, ::-1]
        emb = embed_image_bgr(arr_bgr)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")
    try:
        from src.vector_store.chroma_store import get_collection
        collection = get_collection()
        res = collection.query(
            query_embeddings=[emb],
            n_results=max(1, min(k, 50)),
            include=["metadatas", "distances"]
        )
        
        results = []
        if res and res.get('ids') and res['ids'][0]:
            for i, _id in enumerate(res['ids'][0]):
                similarity = round(1.0 - res['distances'][0][i], 4)
                payload = res['metadatas'][0][i] or {}
                results.append({
                    "id": payload.get("_str_id", _id),
                    "score": similarity,
                    **{f: payload.get(f) for f in
                       ("camera_id", "timestamp_iso", "cloudinary_url", "violence_label", "violence_score")}
                })
        return JSONResponse({"results": results})
    except Exception as e:
        logger.warning(f"Chroma image search failed: {e}")
        return JSONResponse({"results": []})





@app.get("/api/events/feed")
async def events_feed(limit: int = 20, role: str = Depends(require_any_role)):
    try:
        events = db_manager.get_aggregated_events(limit=limit, hours=24)
        return events
    except Exception as e:
        logger.error(f"Error fetching events feed: {e}")
        return JSONResponse([], status_code=500)

@app.get("/api/video/clip")
async def get_video_clip(camera_id: str, timestamp: str, before: int = 5, after: int = 5, role: str = Depends(require_any_role)):
    try:
        from src.preprocessing.clip_generator import create_mp4_from_frames
        from datetime import datetime
        import uuid
        import os
        from fastapi.responses import FileResponse
        
        try:
            # Parse ISO string and ensure it's timezone-aware (UTC)
            # Standard ISO strings from JS toISOString() end in 'Z'
            if timestamp.endswith('Z'):
                from datetime import timezone
                base_ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            else:
                base_ts = datetime.fromisoformat(timestamp)
                
            # If naive, assume UTC as per system convention
            if base_ts.tzinfo is None:
                from datetime import timezone
                base_ts = base_ts.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.error(f"Timestamp parse error: {e}")
            raise HTTPException(status_code=400, detail="Invalid timestamp format")
            
        loop = asyncio.get_running_loop()
        paths = await loop.run_in_executor(None, db_manager.get_frames_for_clip, camera_id, base_ts, before, after)
        if not paths:
            # Fallback for deleted cameras: parse JPEG filenames directly!
            from datetime import timedelta
            start_ts = base_ts - timedelta(seconds=before)
            end_ts = base_ts + timedelta(seconds=after)
            proc_dir = os.path.join(settings.BASE_DIR, "data", "processed")
            
            def _find_orphaned_frames():
                found = []
                if os.path.isdir(proc_dir):
                    for fname in os.listdir(proc_dir):
                        if fname.startswith(f"{camera_id}_") and fname.endswith(".jpg"):
                            parts = fname.split("_")
                            if len(parts) >= 3:
                                ts_str = f"{parts[-3]}_{parts[-2]}"
                                try:
                                    f_ts = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
                                    if start_ts <= f_ts <= end_ts:
                                        found.append(os.path.join(proc_dir, fname))
                                except Exception: pass
                    found.sort()
                return found
                
            paths = await loop.run_in_executor(None, _find_orphaned_frames)
            
        if not paths:
            raise HTTPException(status_code=404, detail="No frames found in that time window")
            
        output_dir = os.path.join(settings.BASE_DIR, "data", "temp_clips")
        os.makedirs(output_dir, exist_ok=True)
        out_filename = f"clip_{camera_id}_{uuid.uuid4().hex[:8]}.mp4"
        out_path = os.path.join(output_dir, out_filename)
        
        # Calculate adaptive FPS for real-time 1:1 playback speed
        span_sec = max(1, before + after)
        num_frames = len(paths)
        video_fps = float(num_frames) / float(span_sec)
        
        # Ensure compatibility (min 5 FPS, max 30 FPS)
        final_paths = paths
        if video_fps < 5.0 and num_frames > 0:
            target_compatible_fps = 10.0
            replications = max(1, int(round(target_compatible_fps / video_fps)))
            video_fps = target_compatible_fps
            final_paths = []
            for p in paths:
                final_paths.extend([p] * replications)
        
        video_fps = max(1.0, min(video_fps, 30.0))
        
        # Build clip in a temp file, then upload to Cloudinary
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        success = await loop.run_in_executor(None, create_mp4_from_frames, final_paths, tmp_path, video_fps)
        if not success or not os.path.exists(tmp_path):
            raise HTTPException(status_code=500, detail="Failed to generate video clip")

        # Upload clip to Cloudinary and return redirect URL
        clip_public_id = f"surveilx/clips/clip_{camera_id}_{uuid.uuid4().hex[:8]}"
        try:
            clip_bytes = open(tmp_path, "rb").read()
            clip_url = await loop.run_in_executor(
                None, _cloudinary_upload_video, clip_bytes, clip_public_id
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        if not clip_url:
            raise HTTPException(status_code=500, detail="Clip upload to Cloudinary failed")

        return RedirectResponse(url=clip_url, status_code=302)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating clip: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# ── Overview response cache: avoid hitting Neon on every 5s poll ─────────────
_OVERVIEW_CACHE: dict = {}          # {"result": ..., "exp": monotonic_ts}
_OVERVIEW_TTL = 10.0                # seconds

@app.get("/api/stats/overview")
async def stats_overview(role: str = Depends(require_any_role)):
    import time as _t
    now_mono = _t.monotonic()

    # Serve from cache if fresh
    if _OVERVIEW_CACHE.get("exp", 0) > now_mono:
        return _OVERVIEW_CACHE["result"]

    # ── Compute stats (all blocking DB calls run in the I/O pool) ──────────
    def _compute():
        cams            = db_manager.list_cameras()
        total_cameras   = len(cams)
        active_streams  = len(BACKGROUND_TASKS)
        active_users    = len(SESSIONS)
        since           = utcnow() - timedelta(hours=24)
        critical_alerts = db_manager.count_critical_events_since(since)
        chart_data      = db_manager.get_events_stats(hours=24)

        events_24h = sum(
            cnt for lbl, cnt in (chart_data.get("by_type") or {}).items()
            if lbl != "Normal"
        )
        return {
            "total_cameras":  total_cameras,
            "active_streams": active_streams,
            "events_24h":     events_24h,
            "active_users":   active_users,
            "storage_usage":  "Chroma",        # UI removed this stat card
            "critical_alerts": critical_alerts,
            "charts":         chart_data,
        }

    result = await asyncio.get_running_loop().run_in_executor(_IO_POOL, _compute)

    _OVERVIEW_CACHE["result"] = result
    _OVERVIEW_CACHE["exp"]    = now_mono + _OVERVIEW_TTL
    return result

@app.get("/api/stats/health")
async def stats_health(role: str = Depends(require_admin)):
    cpu = 0.0
    ram = 0.0
    disk = 0.0

    try:
        if psutil:
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage(str(settings.BASE_DIR)).percent
        else:
            total, used, _ = shutil.disk_usage(settings.BASE_DIR)
            disk = round((used / total) * 100, 1) if total > 0 else 0
    except Exception:
        pass

    import torch
    device_name = "CPU"
    if torch.cuda.is_available():
        count = torch.cuda.device_count()
        name = torch.cuda.get_device_name(0)
        device_name = f"GPU: {name} ({count})"
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
         device_name = "MPS"
            
    return {
        "cpu": cpu,
        "ram": ram,
        "disk": disk,
        "network": "Online",
        "device": device_name,
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available()
    }
