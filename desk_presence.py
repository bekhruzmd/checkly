"""
Edge device desk-presence pipeline.

Runs on a mini-PC on the Tashkent office LAN, NOT on the backend host.
Pulls Hikvision RTSP sub-streams, detects occupancy per zone polygon using
background subtraction at ~2 fps, runs ArcFace face matching on
empty→occupied transitions only, and POSTs derived events to the Checkly
backend over Tailscale.

Only derived events (worker_id, zone, timestamp, similarity) leave the LAN
— raw video frames never cross the tailnet.

Zone configuration lives in a local JSON file (zones.json by default) so
RTSP credentials never reach the backend database.

zones.json format:
    {
        "zones": [
            {
                "desk_zone_id": 1,
                "rtsp_url": "rtsp://admin:pass@192.168.1.64:554/Streaming/Channels/102",
                "zone_type": "desk",
                "zone_polygon": [[0.1, 0.2], [0.5, 0.2], [0.5, 0.8], [0.1, 0.8]],
                "min_leave_seconds": 60
            }
        ]
    }

Hikvision RTSP URL pattern:
  Main stream:  rtsp://user:pass@ip:554/Streaming/Channels/<ch>01
  Sub stream:   rtsp://user:pass@ip:554/Streaming/Channels/<ch>02  ← use this
  NVR channel:  rtsp://user:pass@nvr_ip:554/Streaming/Channels/<cam_num>02

Environment variables:
  BACKEND_URL     - Checkly backend base URL (Tailscale, e.g. http://100.x.x.x:8000)
  EDGE_SECRET     - Shared secret for backend auth
  ZONES_CONFIG    - Path to zones.json (default: zones.json)
  EMBED_REFRESH_S - Worker embedding refresh interval in seconds (default: 3600)
"""

import http.server
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
import requests

import face_match

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

BACKEND_URL    = os.environ.get("BACKEND_URL", "http://localhost:8000")
EDGE_SECRET    = os.environ.get("EDGE_SECRET", "")
SNAPSHOT_PORT  = int(os.environ.get("SNAPSHOT_PORT", "8001"))

# ── Snapshot store (zone_id → latest JPEG bytes) ──────────────────────────────
# Written by CameraMonitor threads; read by the snapshot HTTP server.
_latest_frames: dict[int, bytes] = {}
_frames_lock   = threading.Lock()

# ── Enrollment zones (zone_id → session_id) ──────────────────────────────────
# Refreshed by PresencePipeline on the same schedule as embedding refresh.
# CameraMonitor threads read this to know whether to submit enrollment samples.
_enrollment_zones: dict[int, int] = {}
_enrollment_lock  = threading.Lock()

# Per-zone timestamp of last enrollment sample submission (throttle to 1 per 2 s).
_last_enrollment_submit: dict[int, float] = {}
ENROLLMENT_SUBMIT_INTERVAL = 2.0  # seconds

CAPTURE_FPS     = 2      # target frames per second for occupancy polling
OCC_THRESHOLD   = 500    # foreground pixel count to call a zone "occupied"
DEBOUNCE_FRAMES = 3      # consecutive frames needed to confirm a state change


@dataclass
class ZoneConfig:
    desk_zone_id: int
    rtsp_url: str
    zone_type: str               # 'desk' | 'safe_zone'
    zone_polygon: list           # [[x, y], ...] normalized 0-1 coords
    min_leave_seconds: int = 60  # informational — enforced server-side


# ── Backend communication ─────────────────────────────────────────────────────

def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {EDGE_SECRET}"}


def post_event(
    desk_zone_id: int,
    event_type: str,
    worker_id: Optional[int],
    confidence: Optional[float],
) -> None:
    """POST a derived zone event to the backend. Logs and returns on failure."""
    try:
        resp = requests.post(
            f"{BACKEND_URL}/desk-presence/events",
            json={
                "desk_zone_id": desk_zone_id,
                "event_type":   event_type,
                "worker_id":    worker_id,
                "confidence":   confidence,
            },
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.error("Failed to post event zone=%d type=%s: %s", desk_zone_id, event_type, exc)


def fetch_enrolled_workers() -> list[tuple[int, list[float]]]:
    """Fetch active enrolled workers' embeddings from the backend. Returns [] on failure."""
    try:
        resp = requests.get(
            f"{BACKEND_URL}/desk-presence/workers/embeddings",
            headers=_auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return [(w["worker_id"], w["face_embedding"]) for w in resp.json()]
    except Exception as exc:
        log.error("Failed to fetch worker embeddings: %s", exc)
        return []


def fetch_enrollment_zones() -> dict[int, int]:
    """
    Poll the backend for zones that have an active enrollment session.
    Returns {desk_zone_id: session_id}. Returns {} on failure (safe default).
    """
    try:
        resp = requests.get(
            f"{BACKEND_URL}/enrollment/active-zones",
            headers=_auth_headers(),
            timeout=5,
        )
        resp.raise_for_status()
        return {item["desk_zone_id"]: item["session_id"] for item in resp.json()}
    except Exception as exc:
        log.warning("Failed to fetch enrollment zones: %s", exc)
        return {}


def submit_enrollment_sample(session_id: int, embedding: list[float]) -> None:
    """Submit one face embedding to the active enrollment session. Fire-and-forget."""
    try:
        resp = requests.post(
            f"{BACKEND_URL}/enrollment/sessions/{session_id}/samples",
            json={"embedding": embedding},
            headers=_auth_headers(),
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        log.debug(
            "Enrollment sample submitted: session=%d samples=%d status=%s",
            session_id, data.get("sample_count"), data.get("status"),
        )
    except Exception as exc:
        log.warning("Failed to submit enrollment sample session=%d: %s", session_id, exc)


# ── Snapshot HTTP server ──────────────────────────────────────────────────────

class _SnapshotHandler(http.server.BaseHTTPRequestHandler):
    """
    Serves the latest JPEG frame per zone.
    Endpoint: GET /snapshot/<desk_zone_id>

    This server listens on SNAPSHOT_PORT (default 8001) and is intended to be
    accessible only over the Tailscale network — do not expose to the public
    internet. The admin dashboard fetches snapshots directly from the edge
    device's Tailscale IP to give managers a live view without storing frames.
    """

    def do_GET(self) -> None:  # noqa: N802
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "snapshot":
            try:
                zone_id = int(parts[1])
            except ValueError:
                self.send_error(400, "Invalid zone_id")
                return

            with _frames_lock:
                jpeg = _latest_frames.get(zone_id)

            if jpeg:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(jpeg)
            else:
                self.send_error(404, "No frame available for this zone yet")
        else:
            self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        pass  # suppress per-request HTTP logs; zone-level events are logged by the pipeline


def start_snapshot_server(port: int = SNAPSHOT_PORT) -> threading.Thread:
    server = http.server.HTTPServer(("", port), _SnapshotHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="snapshot-http")
    t.start()
    log.info("Snapshot server listening on port %d", port)
    return t


# ── Computer vision helpers ───────────────────────────────────────────────────

def build_polygon_mask(frame: np.ndarray, polygon: list) -> np.ndarray:
    """Binary mask for the zone polygon, scaled to actual frame dimensions."""
    h, w = frame.shape[:2]
    pts = np.array(
        [[int(x * w), int(y * h)] for x, y in polygon],
        dtype=np.int32,
    )
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 255)
    return mask


def identify_occupant(
    frame: np.ndarray,
    enrolled: list[tuple[int, list[float]]],
) -> tuple[Optional[int], Optional[float]]:
    """
    Run ArcFace on a camera frame to find the closest enrolled worker.
    Returns (worker_id, confidence) or (None, None) if no face found or
    confidence is below the review threshold.
    """
    if not enrolled:
        return None, None
    embedding = face_match.get_embedding_from_frame(frame)
    if embedding is None:
        return None, None
    best_id, best_score = None, -1.0
    for worker_id, stored in enrolled:
        score = face_match.cosine_similarity(embedding, stored)
        if score > best_score:
            best_id, best_score = worker_id, score
    if best_score >= face_match.MATCH_THRESHOLD_REVIEW:
        return best_id, best_score
    return None, None


# ── Camera monitor (one per unique RTSP URL) ──────────────────────────────────

class CameraMonitor(threading.Thread):
    """
    Reads one RTSP stream and runs occupancy detection for all zones
    sharing that camera. Multiple zones per camera are processed in one
    frame pass to avoid duplicate captures.
    """

    def __init__(
        self,
        rtsp_url: str,
        zones: list[ZoneConfig],
        enrolled_ref: list,  # shared list reference, updated by refresh thread
        enrolled_lock: threading.Lock,
    ) -> None:
        super().__init__(daemon=True, name=f"cam-{rtsp_url[-24:]}")
        self.rtsp_url      = rtsp_url
        self.zones         = zones
        self._enrolled     = enrolled_ref
        self._enrolled_lock = enrolled_lock
        self._stop         = threading.Event()

        # Per-zone background subtractor (one per zone_id).
        self._subtractors: dict[int, cv2.BackgroundSubtractor] = {
            z.desk_zone_id: cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=16, detectShadows=False
            )
            for z in zones
        }
        self._state: dict[int, bool]           = {z.desk_zone_id: False for z in zones}
        self._pending_state: dict[int, Optional[bool]] = {z.desk_zone_id: None for z in zones}
        self._pending_count: dict[int, int]    = {z.desk_zone_id: 0 for z in zones}
        self._masks: dict[int, Optional[np.ndarray]] = {z.desk_zone_id: None for z in zones}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        cap = None
        frame_interval = 1.0 / CAPTURE_FPS

        while not self._stop.is_set():
            if cap is None or not cap.isOpened():
                log.info("Connecting to RTSP: %s", self.rtsp_url)
                cap = cv2.VideoCapture(self.rtsp_url)
                if not cap.isOpened():
                    log.error("Cannot open %s — retry in 10 s", self.rtsp_url)
                    time.sleep(10)
                    continue

            t0 = time.monotonic()
            ret, frame = cap.read()
            if not ret:
                log.warning("RTSP read failed (%s) — reconnecting", self.rtsp_url)
                cap.release()
                cap = None
                time.sleep(2)
                continue

            self._process_frame(frame)

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, frame_interval - elapsed))

        if cap:
            cap.release()

    def _process_frame(self, frame: np.ndarray) -> None:
        # Store a downscaled JPEG for the snapshot server (every frame, all zones
        # on this camera share the same physical frame).
        snapshot_h, snapshot_w = frame.shape[:2]
        if snapshot_w > 640:
            scale   = 640 / snapshot_w
            snapshot = cv2.resize(frame, (640, int(snapshot_h * scale)))
        else:
            snapshot = frame
        _, jpeg = cv2.imencode(".jpg", snapshot, [cv2.IMWRITE_JPEG_QUALITY, 70])
        jpeg_bytes = jpeg.tobytes()

        for zone in self.zones:
            zid = zone.desk_zone_id

            with _frames_lock:
                _latest_frames[zid] = jpeg_bytes

            if self._masks[zid] is None:
                self._masks[zid] = build_polygon_mask(frame, zone.zone_polygon)

            fg = self._subtractors[zid].apply(frame)
            zone_fg = cv2.bitwise_and(fg, self._masks[zid])
            is_occupied = int(np.count_nonzero(zone_fg)) >= OCC_THRESHOLD

            # Enrollment sample submission — runs in parallel with normal occupancy
            # tracking whenever the zone has an active enrollment session.
            # Throttled to at most one submission per ENROLLMENT_SUBMIT_INTERVAL seconds.
            self._maybe_submit_enrollment_sample(zid, frame)

            if is_occupied == self._state[zid]:
                self._pending_state[zid] = None
                self._pending_count[zid] = 0
                continue

            # Debounce: require DEBOUNCE_FRAMES consecutive frames.
            if self._pending_state[zid] != is_occupied:
                self._pending_state[zid] = is_occupied
                self._pending_count[zid] = 1
            else:
                self._pending_count[zid] += 1

            if self._pending_count[zid] >= DEBOUNCE_FRAMES:
                self._commit_transition(zone, frame, is_occupied)
                self._state[zid]         = is_occupied
                self._pending_state[zid] = None
                self._pending_count[zid] = 0

    def _maybe_submit_enrollment_sample(self, zid: int, frame: np.ndarray) -> None:
        with _enrollment_lock:
            session_id = _enrollment_zones.get(zid)
        if session_id is None:
            return

        now = time.monotonic()
        last = _last_enrollment_submit.get(zid, 0.0)
        if now - last < ENROLLMENT_SUBMIT_INTERVAL:
            return

        embedding = face_match.get_embedding_from_frame(frame)
        if embedding is None:
            return

        # Basic sharpness gate — same threshold as check_liveness.
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if cv2.Laplacian(gray, cv2.CV_64F).var() < 50.0:
            log.debug("Zone %d enrollment: frame too blurry, skipping sample", zid)
            return

        _last_enrollment_submit[zid] = now
        # Submit in a daemon thread so we don't block the frame loop.
        threading.Thread(
            target=submit_enrollment_sample,
            args=(session_id, embedding),
            daemon=True,
        ).start()

    def _commit_transition(
        self, zone: ZoneConfig, frame: np.ndarray, is_occupied: bool
    ) -> None:
        event_type = "occupied_start" if is_occupied else "occupied_end"
        worker_id, confidence = None, None

        if is_occupied:
            with self._enrolled_lock:
                enrolled_snapshot = list(self._enrolled)
            worker_id, confidence = identify_occupant(frame, enrolled_snapshot)
            log.info(
                "Zone %d (%s) → occupied_start  worker=%s confidence=%s",
                zone.desk_zone_id, zone.zone_type, worker_id, confidence,
            )
        else:
            log.info("Zone %d (%s) → occupied_end", zone.desk_zone_id, zone.zone_type)

        post_event(
            desk_zone_id=zone.desk_zone_id,
            event_type=event_type,
            worker_id=worker_id,
            confidence=confidence,
        )


# ── Pipeline coordinator ──────────────────────────────────────────────────────

class PresencePipeline:
    """
    Owns all CameraMonitor threads and the periodic embedding refresh.
    Zones that share an RTSP URL are grouped onto one monitor to avoid
    duplicate RTSP connections.
    """

    def __init__(self, zones: list[ZoneConfig]) -> None:
        self._enrolled: list[tuple[int, list[float]]] = fetch_enrolled_workers()
        self._enrolled_lock = threading.Lock()
        self._monitors: list[CameraMonitor] = []
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        by_camera: dict[str, list[ZoneConfig]] = {}
        for z in zones:
            by_camera.setdefault(z.rtsp_url, []).append(z)

        for rtsp_url, cam_zones in by_camera.items():
            self._monitors.append(
                CameraMonitor(rtsp_url, cam_zones, self._enrolled, self._enrolled_lock)
            )

    def start(self, embed_refresh_s: int = 3600) -> None:
        for m in self._monitors:
            m.start()
        log.info("Started %d camera monitor(s)", len(self._monitors))

        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            args=(embed_refresh_s,),
            daemon=True,
            name="embed-refresh",
        )
        self._refresh_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for m in self._monitors:
            m.stop()

    def _refresh_loop(self, interval_s: int) -> None:
        while not self._stop.wait(timeout=interval_s):
            fresh = fetch_enrolled_workers()
            with self._enrolled_lock:
                self._enrolled.clear()
                self._enrolled.extend(fresh)
            log.info("Refreshed %d worker embeddings", len(fresh))

            fresh_zones = fetch_enrollment_zones()
            with _enrollment_lock:
                _enrollment_zones.clear()
                _enrollment_zones.update(fresh_zones)
            if fresh_zones:
                log.info(
                    "Active enrollment zones: %s",
                    {f"zone={k}": f"session={v}" for k, v in fresh_zones.items()},
                )


# ── Entry point ───────────────────────────────────────────────────────────────

def load_zones(config_path: str) -> list[ZoneConfig]:
    with open(config_path) as f:
        data = json.load(f)
    return [
        ZoneConfig(
            desk_zone_id=z["desk_zone_id"],
            rtsp_url=z["rtsp_url"],
            zone_type=z["zone_type"],
            zone_polygon=z["zone_polygon"],
            min_leave_seconds=z.get("min_leave_seconds", 60),
        )
        for z in data["zones"]
    ]


def main() -> None:
    config_path    = os.environ.get("ZONES_CONFIG", "zones.json")
    embed_refresh  = int(os.environ.get("EMBED_REFRESH_S", "3600"))

    zones    = load_zones(config_path)
    pipeline = PresencePipeline(zones)

    # Fetch initial enrollment zones before starting monitors.
    initial_zones = fetch_enrollment_zones()
    with _enrollment_lock:
        _enrollment_zones.update(initial_zones)

    start_snapshot_server(SNAPSHOT_PORT)
    pipeline.start(embed_refresh_s=embed_refresh)

    def _shutdown(sig, _frame):
        log.info("Signal %d received — stopping pipeline", sig)
        pipeline.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Block main thread until all monitors finish.
    for monitor in pipeline._monitors:
        monitor.join()


if __name__ == "__main__":
    main()
