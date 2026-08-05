"""
Checkly idle detection agent.

Runs on each worker's PC. Monitors keyboard and mouse activity during the
worker's assigned shift window and reports idle_start / idle_end events to
the Checkly backend. The backend decides when to trigger a presence check
(using shift_monitoring_config.idle_threshold_seconds); the agent just reports
raw idle state.

Configuration: edit config.ini next to this file.
  [agent]
  worker_id   = 5
  backend_url = http://your-server:8000

Deployment:
  Windows  — wrap with NSSM as a service, or use Task Scheduler at user logon.
  Linux    — install as a systemd user service (~/.config/systemd/user/).

Only runs while the worker's shift is active (fetched from the backend on
startup and re-fetched after midnight to handle overnight shifts).
"""

import configparser
import logging
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

import requests
from pynput import keyboard, mouse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [idle-agent] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.ini"

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> tuple[int, str]:
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        sys.exit(f"config.ini not found at {CONFIG_PATH}")
    cfg.read(CONFIG_PATH)
    worker_id   = cfg.getint("agent", "worker_id")
    backend_url = cfg.get("agent", "backend_url").rstrip("/")
    return worker_id, backend_url


# ── Shift window (fetched from backend, re-fetched after midnight) ─────────────

def fetch_shift_window(worker_id: int, backend_url: str) -> dict | None:
    try:
        r = requests.get(
            f"{backend_url}/presence/workers/{worker_id}/shift-window", timeout=10
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("monitoring_enabled"):
            log.info("Monitoring disabled or no schedule for today — sleeping 30 min")
            return None
        return data
    except Exception as e:
        log.error("Could not fetch shift window: %s", e)
        return None


def _parse_time(s: str) -> dtime:
    h, m, *rest = s.split(":")
    return dtime(int(h), int(m), int(rest[0]) if rest else 0)


def is_within_window(window: dict) -> bool:
    if not window:
        return False
    start = _parse_time(window["start_time"])
    end   = _parse_time(window["end_time"])
    now   = datetime.now().time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


# ── Backend reporting ──────────────────────────────────────────────────────────

def report_event(worker_id: int, backend_url: str, event: str) -> None:
    try:
        r = requests.post(
            f"{backend_url}/presence/idle-events",
            json={"worker_id": worker_id, "event": event},
            timeout=10,
        )
        r.raise_for_status()
        log.info("Reported %s → %s", event, r.json().get("status"))
    except Exception as e:
        log.error("Failed to report %s: %s", event, e)


# ── Input listener ─────────────────────────────────────────────────────────────

class IdleTracker:
    def __init__(self) -> None:
        self.last_input = time.monotonic()

    def on_activity(self, *_) -> None:
        self.last_input = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_input


# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    worker_id, backend_url = load_config()
    log.info("Starting — worker_id=%d  backend=%s", worker_id, backend_url)

    tracker = IdleTracker()

    # Global listeners keep running even outside shift hours (cheap; just increment a counter).
    kb_listener = keyboard.Listener(
        on_press=tracker.on_activity,
        on_release=tracker.on_activity,
        daemon=True,
    )
    ms_listener = mouse.Listener(
        on_move=tracker.on_activity,
        on_click=tracker.on_activity,
        on_scroll=tracker.on_activity,
        daemon=True,
    )
    kb_listener.start()
    ms_listener.start()

    window: dict | None = None
    window_fetched_date: str | None = None
    idle_reported = False

    while True:
        now_date = datetime.now().date().isoformat()

        # Re-fetch shift window once per calendar day (handles overnight shifts).
        if window_fetched_date != now_date:
            window = fetch_shift_window(worker_id, backend_url)
            window_fetched_date = now_date
            idle_reported = False
            log.info("Shift window for %s: %s", now_date, window)

        if not window or not is_within_window(window):
            # Outside shift — sleep 30 s then recheck.
            time.sleep(30)
            continue

        idle_sec       = tracker.idle_seconds()
        threshold_sec  = window.get("idle_threshold_seconds", 360)

        if idle_sec >= threshold_sec and not idle_reported:
            log.info("Idle %.0f s — reporting idle_start", idle_sec)
            report_event(worker_id, backend_url, "idle_start")
            idle_reported = True

        elif idle_sec < threshold_sec * 0.5 and idle_reported:
            # Activity resumed — report idle_end when idle drops below half the threshold.
            log.info("Activity resumed — reporting idle_end")
            report_event(worker_id, backend_url, "idle_end")
            idle_reported = False

        time.sleep(1)


if __name__ == "__main__":
    run()
