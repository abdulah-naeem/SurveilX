import os
import logging
from typing import List, Dict, Optional, Any
import chromadb
from chromadb.config import Settings as ChromaSettings

logger = logging.getLogger(__name__)

CHROMA_HOST = os.getenv("CHROMA_HOST", "chroma.railway.internal").strip()
CHROMA_PORT = os.getenv("CHROMA_PORT", "8000").strip()
CHROMA_TOKEN = os.getenv("CHROMA_TOKEN", "2igo7cy4p184i5w7").strip()

# Make sure to include http:// or https://
if not CHROMA_HOST.startswith("http"):
    RAILWAY_URL = f"http://{CHROMA_HOST}"
else:
    RAILWAY_URL = CHROMA_HOST

if CHROMA_PORT:
    RAILWAY_URL = f"{RAILWAY_URL}:{CHROMA_PORT}"

logger.info(f"Chroma: connecting to remote at {RAILWAY_URL}")

# Connect to Railway ChromaDB instance lazily to prevent start-up crashes if offline
_client = None

def get_client():
    global _client
    if _client is None:
        _client = chromadb.HttpClient(
            host=RAILWAY_URL,
            headers={"Authorization": f"Bearer {CHROMA_TOKEN}"} if CHROMA_TOKEN else {}
        )
    return _client

COLLECTION_NAME = "video_frames"

def get_collection():
    return get_client().get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"}
    )

def _sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not metadata: return {}
    out = {}
    for k, v in metadata.items():
        if v is None: continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out

def upsert_frame(
    frame_id: str,
    metadata: Dict[str, Any] = None,
    document: Optional[str] = None,
    embedding: Optional[List[float]] = None,
) -> None:
    collection = get_collection()
    data = {
        "ids": [frame_id],
        "metadatas": [_sanitize_metadata(metadata)]
    }
    if document is not None:
        data["documents"] = [document]
    if embedding is not None:
        data["embeddings"] = [embedding]
        
    collection.upsert(**data)


def query_by_metadata(where: Dict[str, Any], n_results: int = 5, include: Optional[List[str]] = None) -> Dict[str, List[Any]]:
    return get_collection().query(where=where, n_results=n_results, include=include or ["metadatas"])

def get_all_metadata() -> List[Dict[str, Any]]:
    res = get_collection().get(include=["metadatas"])
    return res.get("metadatas", [])

try:
    get_collection()
except Exception as e:
    logger.warning(f"Could not init Chroma collection on startup: {e}")



def delete_frames(frame_ids: list[str]) -> bool:
    """Delete a list of frames from Chroma collection."""
    if not frame_ids:
        return True
    try:
        col = get_collection()
        col.delete(ids=frame_ids)
        logger.info(f"Deleted {len(frame_ids)} frames from Chroma")
        return True
    except Exception as e:
        logger.error(f"Chroma delete failed: {e}")
        return False
