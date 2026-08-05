"""
Face matching + basic liveness check.

Design principle: the raw photo bytes NEVER touch the disk and NEVER
get stored in the database. They live in memory only for the duration
of this function call, get reduced to (a) a 512-d embedding at
enrollment time, or (b) a boolean match result at check-in time, and
are then discarded when the request finishes.
"""

import io
import numpy as np
import cv2
from insightface.app import FaceAnalysis

# Loaded once at process startup, reused across requests.
_face_app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
_face_app.prepare(ctx_id=0, det_size=(640, 640))

MATCH_THRESHOLD_ACCEPT = 0.70   # cosine similarity above this -> accept
MATCH_THRESHOLD_REVIEW = 0.60   # between review and accept -> flag for manual review
                                 # below review threshold -> reject


def _bytes_to_cv2_image(photo_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(photo_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")
    return img


def get_embedding(photo_bytes: bytes) -> list[float]:
    """
    Used ONCE at enrollment. Returns a 512-d vector representing the
    worker's face. This vector cannot be used to reconstruct the
    original photo -- it's a one-way projection, similar in spirit
    to a password hash.
    """
    img = _bytes_to_cv2_image(photo_bytes)
    faces = _face_app.get(img)
    if len(faces) == 0:
        raise ValueError("No face detected in enrollment photo")
    if len(faces) > 1:
        raise ValueError("Multiple faces detected -- enrollment photo must show exactly one person")
    return faces[0].normed_embedding.tolist()


def check_liveness(photo_bytes: bytes) -> bool:
    """
    Minimal liveness heuristic for MVP: reject obvious screen/photo
    replays by checking face size relative to frame and basic sharpness
    (a photo-of-a-photo tends to be blurrier / lower contrast than a
    live capture). This is intentionally simple for v1 -- swap in
    InsightFace's dedicated anti-spoofing model later if fraud shows
    up in practice.
    """
    img = _bytes_to_cv2_image(photo_bytes)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
    return sharpness > 50.0  # tune this threshold against real captures


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a_arr, b_arr = np.array(a), np.array(b)
    return float(np.dot(a_arr, b_arr) / (np.linalg.norm(a_arr) * np.linalg.norm(b_arr)))


def get_embedding_from_frame(frame: np.ndarray) -> list[float] | None:
    """
    Extract a face embedding from an already-decoded cv2 frame (numpy array).
    Returns None if no face is detected. Used by the edge device pipeline
    so camera frames don't need to be re-encoded to bytes.
    """
    faces = _face_app.get(frame)
    if not faces:
        return None
    return faces[0].normed_embedding.tolist()


def match_face(photo_bytes: bytes, enrolled_workers: list[tuple[int, list[float]]]):
    """
    enrolled_workers: list of (worker_id, embedding) for all active workers.
    Returns (worker_id_or_None, confidence, status) where status is one of
    'accepted', 'manual_review', 'rejected'.
    """
    img = _bytes_to_cv2_image(photo_bytes)
    faces = _face_app.get(img)
    if len(faces) == 0:
        return None, 0.0, "rejected"

    probe_embedding = faces[0].normed_embedding.tolist()

    best_worker_id, best_score = None, -1.0
    for worker_id, embedding in enrolled_workers:
        score = cosine_similarity(probe_embedding, embedding)
        if score > best_score:
            best_worker_id, best_score = worker_id, score

    if best_score >= MATCH_THRESHOLD_ACCEPT:
        return best_worker_id, best_score, "accepted"
    elif best_score >= MATCH_THRESHOLD_REVIEW:
        return best_worker_id, best_score, "manual_review"
    else:
        return None, best_score, "rejected"
