from sqlalchemy import (
    Column, Integer, String, TIMESTAMP, ForeignKey, JSON, Float, Boolean, Index
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

Base = declarative_base()

class AuthUser(Base):
    __tablename__ = "auth_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False)  # 'admin' or 'user'
    created_at = Column(TIMESTAMP, server_default=func.now())

class VideoStream(Base):
    __tablename__ = "video_streams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Legacy external camera identifier, retained for compatibility
    camera_id = Column(String(50), nullable=False)
    # New relational link to cameras.id (kept additive to avoid destructive change)
    camera_pk = Column(Integer, ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    status = Column(String(20), default="captured")

    camera = relationship("Camera", back_populates="streams")
    metadata_entries = relationship("VideoMetadata", back_populates="video_stream")

class Camera(Base):
    __tablename__ = "cameras"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    source_url = Column(String(512), nullable=False)
    zone = Column(String(100))
    enabled = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    # Target frames-per-second for embedding/storage (per camera)
    embed_fps = Column(Integer, default=15)

    streams = relationship("VideoStream", back_populates="camera")
    metadata_entries = relationship("VideoMetadata", back_populates="camera")

class GlobalSetting(Base):
    __tablename__ = "global_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(String(512), nullable=False)
    description = Column(String(255))
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

class VideoMetadata(Base):
    __tablename__ = "video_metadata"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_stream_id = Column(Integer, ForeignKey("video_streams.id"), nullable=True)
    # Direct link to camera as well for easier querying/joins
    camera_pk = Column(Integer, ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    frame_id = Column(String(200), unique=True, index=True, nullable=False)
    timestamp = Column(TIMESTAMP(timezone=True), nullable=False)
    camera_location = Column(String(100))
    resolution = Column(String(20))
    # future-friendly fields for detection & vector search
    violence_label = Column(String(50))           # e.g., shooting, burglary, fighting, stealing
    violence_score = Column(Float)                # model confidence
    detections = Column(JSON, default={})         # arbitrary detection payloads
    # embeddings are stored in Chroma Cloud; keep optional cache fields
    embedding = Column(JSON, default=None)        # optional cached embedding vector
    embedding_model = Column(String(100), default="clip", server_default="clip")  # model name used
    metadata_json = Column(JSON, default={})

    video_stream = relationship("VideoStream", back_populates="metadata_entries")
    camera = relationship("Camera", back_populates="metadata_entries")

    __table_args__ = (
        Index("idx_timestamp_label", "timestamp", "violence_label"),
    )
