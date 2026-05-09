from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
from .models import Base, VideoStream, VideoMetadata, AuthUser, Camera, GlobalSetting
from datetime import datetime, timedelta
from config.settings import settings
from src.utils.time_utils import utcnow
import logging
import time as _time

from passlib.context import CryptContext

logger = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self, db_url=None):
        self.db_url = db_url or settings.DB_URL
        if not self.db_url:
            raise ValueError("Database URL not configured. Set SURVEILX_DB_URL in .env")

        logger.info(f"Connecting to database: {self._mask_password(self.db_url)}")

        # ── Connection pool tuned for Neon free-tier pooler (pgbouncer) ──────────
        # IMPORTANT: Neon's pooler does NOT support startup parameters in connect_args
        # (e.g. statement_timeout) — they cause OperationalError on every connection.
        # pool_pre_ping=True  → validate before checkout (detects pooler idle drops)
        # pool_size=2         → keep 2 warm connections (enough for free-tier)
        # max_overflow=4      → allow burst up to 6 total
        # pool_recycle=1800   → recycle before Neon's 5-min idle timeout
        self.engine = create_engine(
            self.db_url,
            poolclass=QueuePool,
            pool_size=2,
            max_overflow=4,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_timeout=15,
        )

        self.Session = scoped_session(sessionmaker(bind=self.engine))
        self._pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

        # ── Settings cache: key → (value, expire_ts) ──────────────────────────────
        self._settings_cache: dict = {}          # key → (value, expire_ts)
        self._SETTINGS_TTL = 30.0                # seconds

        # ── Stats cache: (result, expire_ts) ─────────────────────────────────────
        self._stats_cache: dict = {}             # cache_key → (result, expire_ts)

        self._create_tables()

    # ─────────────────────────── helpers ────────────────────────────────────────

    def _mask_password(self, db_url: str) -> str:
        if "@" in db_url:
            parts = db_url.split("@", 1)
            return f"{parts[0].split('//')[0]}//***:***@{parts[1]}"
        return db_url

    def _create_tables(self):
        try:
            Base.metadata.create_all(self.engine)
            logger.info("Database tables created/verified")
        except Exception as e:
            logger.error(f"Error creating database tables: {e}")
            raise

    def get_session(self):
        return self.Session()

    def _stats_get(self, key: str):
        """Return cached stats result or None if missing/expired."""
        entry = self._stats_cache.get(key)
        if entry and _time.monotonic() < entry[1]:
            return entry[0]
        return None

    def _stats_set(self, key: str, value, ttl: float = 15.0):
        self._stats_cache[key] = (value, _time.monotonic() + ttl)

    def _stats_invalidate(self):
        self._stats_cache.clear()

    # ─────────────────────────── Users ──────────────────────────────────────────

    def get_user_by_username(self, username: str):
        session = self.get_session()
        try:
            return session.query(AuthUser).filter(AuthUser.username == username).first()
        finally:
            session.close()

    def create_user(self, username: str, password: str, role: str = "user"):
        session = self.get_session()
        try:
            existing = session.query(AuthUser).filter(AuthUser.username == username).first()
            if existing:
                return existing
            u = AuthUser(
                username=username,
                password_hash=self._pwd.hash(password or ""),
                role=role,
            )
            session.add(u)
            session.commit()
            return u          # No refresh — avoids extra SELECT
        finally:
            session.close()

    def ensure_default_users(self):
        """Seed default users and settings if they don't already exist."""
        try:
            import os
            admin_pwd = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")
            user_pwd  = os.getenv("DEFAULT_USER_PASSWORD",  "user123")
            
            self.create_user("admin", admin_pwd, role="admin")
            self.create_user("user",  user_pwd,  role="user")

            defaults = [
                ("violence_threshold",      "0.5",   "Minimum confidence for violence alerts (0.0-1.0)"),
                ("retention_days",          "7",     "Days to keep captured frames and clips"),
                ("event_merge_gap_seconds", "30",    "Wait X seconds before splitting consecutive detections into separate events"),
                ("show_alert_popup",        "true",  "Display the red CRITICAL ALERT modal on detections (true/false)"),
                ("clip_max_seconds",        "120",   "Maximum duration (seconds) for a time-range video clip generated from the NLP search panel"),
                ("max_cameras",             "3",     "Maximum number of simultaneous cameras allowed (recommended: 3 on free hosting, up to 6 with 16GB RAM)"),
                ("detection_fps",           "15",    "Frames per second for real-time violence detection logic"),
                ("clip_fps",                "10",    "Frames per second for generated video clips"),
                ("embed_fps",               "1",     "Frames per second for CLIP Chroma embeddings (keep low to save CPU)"),
                ("timezone",                "UTC+0", "System timezone for display and event logging (e.g. UTC+0, UTC+5)"),
            ]
            # Bulk read all settings once, seed missing ones
            session = self.get_session()
            try:
                existing_keys = {s.key for s in session.query(GlobalSetting.key).all()}
                for key, val, desc in defaults:
                    if key not in existing_keys:
                        session.add(GlobalSetting(key=key, value=val, description=desc))
                session.commit()
                # Pre-warm settings cache
                for key, val, _ in defaults:
                    if key not in self._settings_cache:
                        self._settings_cache[key] = (val, _time.monotonic() + self._SETTINGS_TTL)
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"ensure_default_users error: {e}")

    # ─────────────────────────── Settings ───────────────────────────────────────

    def get_setting(self, key: str, default: str = None) -> str:
        # Check TTL cache first
        entry = self._settings_cache.get(key)
        if entry and _time.monotonic() < entry[1]:
            return entry[0]

        session = self.get_session()
        try:
            s = session.query(GlobalSetting).filter(GlobalSetting.key == key).first()
            val = s.value if s else default
            if val is not None:
                self._settings_cache[key] = (val, _time.monotonic() + self._SETTINGS_TTL)
            return val
        finally:
            session.close()

    def set_setting(self, key: str, value: str, description: str = None):
        session = self.get_session()
        try:
            s = session.query(GlobalSetting).filter(GlobalSetting.key == key).first()
            if not s:
                s = GlobalSetting(key=key, value=str(value), description=description)
                session.add(s)
            else:
                s.value = str(value)
                if description:
                    s.description = description
            session.commit()
            # Update cache immediately
            self._settings_cache[key] = (str(value), _time.monotonic() + self._SETTINGS_TTL)
        finally:
            session.close()

    def list_settings(self):
        session = self.get_session()
        try:
            return session.query(GlobalSetting).all()
        finally:
            session.close()

    # ─────────────────────────── Frame pipeline ──────────────────────────────────


    def insert_frame_pipeline(
        self,
        camera_id: str,
        camera_pk: int | None,
        frame_id: str,
        timestamp,
        camera_location=None,
        resolution=None,
        metadata_json: dict | None = None,
        violence_label: str | None = None,
        violence_score: float | None = None,
        detections: dict | None = None,
        embedding: dict | None = None,
    ):
        """
        Insert VideoStream + VideoMetadata in ONE transaction, ONE round-trip.
        """
        session = self.get_session()
        try:
            # Verify camera_pk exists if provided to prevent FK violation
            if camera_pk is not None:
                exists = session.query(Camera).filter(Camera.id == camera_pk).first()
                if not exists:
                    logger.warning(f"camera_pk {camera_pk} not found in DB for camera_id {camera_id}. Dropping PK to avoid FK violation.")
                    camera_pk = None

            vs = VideoStream(camera_id=camera_id, camera_pk=camera_pk, status="captured")
            session.add(vs)
            session.flush()  # populate vs.id

            vm = VideoMetadata(
                video_stream_id=vs.id,
                frame_id=frame_id,
                timestamp=timestamp,
                camera_location=camera_location,
                resolution=resolution,
                violence_label=violence_label,
                violence_score=violence_score,
                detections=detections or {},
                embedding=embedding,
                metadata_json=metadata_json or {},
                camera_pk=camera_pk,
            )
            session.add(vm)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"insert_frame_pipeline failed (camera_id={camera_id}, pk={camera_pk}): {e}")
            raise
        finally:
            session.close()

    def insert_video_metadata(self, timestamp, frame_id, camera_location=None,
                              resolution=None, violence_label=None, violence_score=None,
                              detections=None, embedding=None, embedding_model=None,
                              metadata_json=None, video_stream_id=None, camera_pk=None):
        """Legacy compat wrapper — still used from _op_db()."""
        session = self.get_session()
        try:
            if camera_pk is not None:
                exists = session.query(Camera).filter(Camera.id == camera_pk).first()
                if not exists:
                    camera_pk = None

            vm = VideoMetadata(
                video_stream_id=video_stream_id,
                frame_id=frame_id,
                timestamp=timestamp,
                camera_location=camera_location,
                resolution=resolution,
                violence_label=violence_label,
                violence_score=violence_score,
                detections=detections or {},
                embedding=embedding,
                metadata_json=metadata_json or {},
                camera_pk=camera_pk,
            )
            session.add(vm)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Legacy insert_video_metadata failed: {e}")
            raise
        finally:
            session.close()

    # ─────────────────────────── Queries / Stats ─────────────────────────────────

    def query_metadata(self, **filters):
        session = self.get_session()
        try:
            q = session.query(VideoMetadata)
            for k, v in filters.items():
                if hasattr(VideoMetadata, k):
                    q = q.filter(getattr(VideoMetadata, k) == v)
            return q.all()
        finally:
            session.close()

    def count_events_since(self, since_ts: datetime, exclude_label: str = None) -> int:
        ck = f"count_events_{since_ts.hour}_{exclude_label}"
        cached = self._stats_get(ck)
        if cached is not None:
            return cached

        session = self.get_session()
        try:
            q = session.query(VideoMetadata).filter(VideoMetadata.timestamp >= since_ts)
            if exclude_label:
                q = q.filter(VideoMetadata.violence_label != exclude_label)
            result = q.count()
            self._stats_set(ck, result, ttl=15.0)
            return result
        finally:
            session.close()

    def get_events_stats(self, hours: int = 24):
        """Returns aggregated stats for charts. Cached for 30s."""
        ck = f"events_stats_{hours}"
        cached = self._stats_get(ck)
        if cached is not None:
            return cached

        session = self.get_session()
        try:
            since = utcnow() - timedelta(hours=hours)
            try:
                merge_gap = float(self.get_setting("event_merge_gap_seconds", "30"))
            except Exception:
                merge_gap = 30.0

            # Filter at SQL level — only non-Normal rows, only needed columns
            rows = (
                session.query(
                    VideoMetadata.violence_label,
                    VideoMetadata.timestamp,
                    VideoMetadata.camera_pk,
                )
                .filter(VideoMetadata.timestamp >= since)
                .filter(VideoMetadata.violence_label.isnot(None))
                .order_by(VideoMetadata.timestamp.asc())
                .all()
            )

            active_events = {}
            sequences = []

            for row in rows:
                lbl = row.violence_label or "Normal"
                if lbl == "Initializing...":
                    continue
                cid = str(row.camera_pk)
                ts  = row.timestamp

                if cid in active_events and active_events[cid]['label'] == lbl:
                    gap = (ts - active_events[cid]['last_ts']).total_seconds()
                    if gap <= merge_gap:
                        active_events[cid]['last_ts'] = ts
                        continue

                if cid in active_events:
                    sequences.append(active_events.pop(cid))

                active_events[cid] = {'label': lbl, 'start_ts': ts, 'last_ts': ts}

            for s in active_events.values():
                sequences.append(s)

            now = utcnow()
            by_time = {(now - timedelta(hours=i)).strftime("%H:00"): {} for i in range(hours)}
            by_type: dict = {}

            for s in sequences:
                lbl = s['label']
                h   = s['start_ts'].strftime("%H:00")
                if h in by_time:
                    by_time[h][lbl] = by_time[h].get(lbl, 0) + 1
                by_type[lbl] = by_type.get(lbl, 0) + 1

            result = {"by_time": by_time, "by_type": by_type}
            self._stats_set(ck, result, ttl=30.0)
            return result
        finally:
            session.close()

    def get_aggregated_events(self, limit: int = 20, hours: int = 24):
        ck = f"agg_events_{limit}_{hours}"
        cached = self._stats_get(ck)
        if cached is not None:
            return cached

        session = self.get_session()
        try:
            try:
                threshold = float(self.get_setting("violence_threshold", "0.5"))
            except Exception:
                threshold = 0.5
            try:
                MERGE_GAP = float(self.get_setting("event_merge_gap_seconds", "30"))
            except Exception:
                MERGE_GAP = 30.0

            since = utcnow() - timedelta(hours=hours)

            # SQL-level filter: exclude Normal + low confidence
            rows = (
                session.query(
                    VideoMetadata.violence_label,
                    VideoMetadata.violence_score,
                    VideoMetadata.timestamp,
                    VideoMetadata.camera_pk,
                )
                .filter(VideoMetadata.timestamp >= since)
                .filter(VideoMetadata.violence_label.isnot(None))
                .filter(VideoMetadata.violence_label != "Normal")
                .filter(VideoMetadata.violence_label != "Initializing...")
                .filter(VideoMetadata.violence_score >= threshold)
                .order_by(VideoMetadata.timestamp.asc())
                .all()
            )

            if not rows:
                self._stats_set(ck, [], ttl=15.0)
                return []

            active_events = {}
            completed_events = []

            for r in rows:
                lbl  = r.violence_label
                conf = r.violence_score or 0.0
                ts   = r.timestamp
                cid  = str(r.camera_pk)

                if cid in active_events:
                    curr = active_events[cid]
                    time_gap = (ts - curr['last_ts']).total_seconds()
                    if curr['label'] == lbl and time_gap <= MERGE_GAP:
                        curr['last_ts'] = ts
                        curr['max_conf'] = max(curr['max_conf'], conf)
                    else:
                        completed_events.append(active_events.pop(cid))
                        active_events[cid] = {'camera_id': cid, 'label': lbl,
                                              'start_ts': ts, 'last_ts': ts, 'max_conf': conf}
                else:
                    active_events[cid] = {'camera_id': cid, 'label': lbl,
                                          'start_ts': ts, 'last_ts': ts, 'max_conf': conf}

            for curr in active_events.values():
                completed_events.append(curr)

            completed_events.sort(key=lambda x: x['start_ts'], reverse=True)

            results = [
                {
                    "camera_id":  ev['camera_id'],
                    "label":      ev['label'],
                    "timestamp":  ev['start_ts'].isoformat(),
                    "last_ts":    ev['last_ts'].isoformat(),
                    "confidence": ev['max_conf'],
                    "duration":   (ev['last_ts'] - ev['start_ts']).total_seconds()
                }
                for ev in completed_events[:limit]
            ]
            self._stats_set(ck, results, ttl=15.0)
            return results
        finally:
            session.close()

    def count_critical_events_since(self, since_ts: datetime) -> int:
        ck = f"critical_{since_ts.hour}"
        cached = self._stats_get(ck)
        if cached is not None:
            return cached

        session = self.get_session()
        try:
            CRITICAL = ['Fighting', 'Shooting', 'Burglary', 'Fire', 'Explosion', 'Accident']
            result = (
                session.query(VideoMetadata)
                .filter(VideoMetadata.timestamp >= since_ts)
                .filter(VideoMetadata.violence_label.in_(CRITICAL))
                .count()
            )
            self._stats_set(ck, result, ttl=15.0)
            return result
        finally:
            session.close()

    def get_frames_for_clip(self, camera_id: str, base_ts: datetime,
                            before_sec: int, after_sec: int) -> list:
        session = self.get_session()
        try:
            start_ts = base_ts - timedelta(seconds=before_sec)
            end_ts   = base_ts + timedelta(seconds=after_sec)
            rows = (
                session.query(VideoMetadata)
                .join(VideoStream, VideoMetadata.video_stream_id == VideoStream.id)
                .filter(VideoStream.camera_id == camera_id)
                .filter(VideoMetadata.timestamp >= start_ts)
                .filter(VideoMetadata.timestamp <= end_ts)
                .order_by(VideoMetadata.timestamp.asc(), VideoMetadata.id.asc())
                .all()
            )
            sources = []
            for r in rows:
                md = r.metadata_json or {}
                # Prefer Cloudinary URL (always populated for new frames)
                cdn = md.get("cloudinary_url", "")
                if cdn and cdn.startswith("http"):
                    sources.append(cdn)
                    continue
                # Fallback: local file path (legacy rows)
                fp = md.get("file_path", "")
                if fp:
                    sources.append(fp)
            return sources
        finally:
            session.close()

    # ─────────────────────────── Cameras ─────────────────────────────────────────

    def list_cameras(self, only_enabled: bool = False):
        session = self.get_session()
        try:
            q = session.query(Camera)
            if only_enabled:
                q = q.filter(Camera.enabled == True)
            return q.all()
        finally:
            session.close()

    def create_camera(self, name: str, source_url: str, zone: str | None = None,
                      enabled: bool = True, embed_fps: int | float | None = None) -> Camera:
        session = self.get_session()
        try:
            fields = dict(name=name, source_url=source_url, zone=zone, enabled=enabled)
            if embed_fps is not None:
                try:
                    fields["embed_fps"] = int(embed_fps) if float(embed_fps).is_integer() else float(embed_fps)
                except Exception:
                    pass
            cam = Camera(**fields)
            session.add(cam)
            session.commit()
            session.refresh(cam)   # Needed here — caller needs cam.id for _refresh_cameras_from_db
            return cam
        finally:
            session.close()

    def update_camera(self, camera_id: int, **fields) -> Camera | None:
        session = self.get_session()
        try:
            cam = session.query(Camera).get(camera_id)
            if not cam:
                return None
            for k, v in fields.items():
                if hasattr(cam, k) and v is not None:
                    setattr(cam, k, v)
            session.commit()
            session.refresh(cam)
            return cam
        finally:
            session.close()

    def delete_camera(self, camera_id: int) -> bool:
        session = self.get_session()
        try:
            cam = session.query(Camera).get(camera_id)
            if not cam:
                return False
            # We no longer delete associated metadata/streams here.
            # The 'ondelete=SET NULL' at the DB level preserves the history.
            session.delete(cam)
            session.commit()
            self._stats_invalidate()
            return True
        finally:
            session.close()
    def get_and_delete_old_frames(self, days: float) -> list[dict]:
        """Find frames older than `days`, delete them from DB, and return their info for external cleanup."""
        session = self.get_session()
        try:
            cutoff = utcnow() - timedelta(days=days)
            rows = session.query(VideoMetadata).filter(VideoMetadata.timestamp < cutoff).all()
            if not rows:
                return []
            
            deleted_assets = []
            for r in rows:
                md = r.metadata_json or {}
                deleted_assets.append({
                    "frame_id": r.frame_id,
                    "cloudinary_url": md.get("cloudinary_url", ""),
                    "file_path": md.get("file_path", "")
                })
                session.delete(r)
                
            session.commit()
            return deleted_assets
        except Exception as e:
            session.rollback()
            logger.error(f"Error in get_and_delete_old_frames: {e}")
            return []
        finally:
            session.close()

