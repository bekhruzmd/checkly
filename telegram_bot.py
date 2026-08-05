"""
Telegram integration — notifications only, plus presence-check prompts.

Two responsibilities:
1. Sending outbound messages (check-in confirmations, presence prompts).
2. Polling for inbound photo replies and routing them to the presence
   check respond endpoint on the local FastAPI server.

The polling loop runs in a daemon thread started at FastAPI startup.
It makes a local HTTP call to POST /presence/checks/{id}/respond so the
face-matching logic stays in one place (the FastAPI handler), not here.

Set TELEGRAM_TOKEN in environment or config before deploying.
"""

import io
import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)

TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
API   = f"https://api.telegram.org/bot{TOKEN}"

# chat_id → check_id for workers currently awaiting a selfie response.
# Written by register_pending() called from the presence router,
# read and cleared by the polling loop when a photo arrives.
_pending: dict[int, int] = {}
_pending_lock = threading.Lock()

# Base URL for the local FastAPI server (where presence endpoints live).
_api_base: str = "http://127.0.0.1:8000"


def configure(token: str, api_base: str = "http://127.0.0.1:8000") -> None:
    global TOKEN, API, _api_base
    TOKEN    = token
    API      = f"https://api.telegram.org/bot{TOKEN}"
    _api_base = api_base


def send_message(chat_id: int, text: str) -> None:
    if not TOKEN:
        log.warning("TELEGRAM_TOKEN not set — skipping send_message")
        return
    try:
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        log.error("Telegram sendMessage failed: %s", e)


def send_unauthorized_leave_notification(
    chat_id: int, leave_count: int, threshold: int
) -> None:
    at_limit = leave_count >= threshold
    msg = (
        f"[Checkly] Unauthorized desk absence recorded. "
        f"Today's count: {leave_count}/{threshold}."
    )
    if at_limit:
        msg += " You have reached today's leave limit — a deduction may be applied."
    send_message(chat_id, msg)


def send_authorized_break_confirmation(chat_id: int) -> None:
    send_message(chat_id, "[Checkly] Authorized break recorded.")


def send_presence_prompt(chat_id: int, check_id: int, window_seconds: int) -> None:
    minutes = window_seconds // 60
    send_message(
        chat_id,
        f"[Checkly] You've been idle for a while. Please reply with a selfie "
        f"within {minutes} min to confirm you're at your desk. (Check #{check_id})",
    )


def register_pending(chat_id: int, check_id: int) -> None:
    with _pending_lock:
        _pending[chat_id] = check_id


def clear_pending(chat_id: int) -> None:
    with _pending_lock:
        _pending.pop(chat_id, None)


def _poll_loop() -> None:
    """Long-poll Telegram for updates; route incoming photos to the presence handler."""
    if not TOKEN:
        log.warning("TELEGRAM_TOKEN not set — bot polling disabled")
        return

    offset = 0
    log.info("Telegram polling started")

    while True:
        try:
            resp = requests.post(
                f"{API}/getUpdates",
                json={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=40,
            )
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")

                if not chat_id:
                    continue

                with _pending_lock:
                    check_id = _pending.get(chat_id)

                if check_id and msg.get("photo"):
                    # Largest available photo size is the last element.
                    file_id = msg["photo"][-1]["file_id"]
                    _handle_presence_photo(chat_id, check_id, file_id)

        except Exception as e:
            log.error("Telegram poll error: %s", e)
            time.sleep(5)


def _handle_presence_photo(chat_id: int, check_id: int, file_id: str) -> None:
    """Download the photo from Telegram and POST it to the presence respond endpoint."""
    try:
        # Resolve file path
        r = requests.post(f"{API}/getFile", json={"file_id": file_id}, timeout=10)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]

        # Download the photo bytes
        photo_resp = requests.get(
            f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=15
        )
        photo_resp.raise_for_status()
        photo_bytes = photo_resp.content

        # POST to the local presence respond endpoint, then immediately discard bytes.
        resp = requests.post(
            f"{_api_base}/presence/checks/{check_id}/respond",
            files={"photo": ("selfie.jpg", io.BytesIO(photo_bytes), "image/jpeg")},
            timeout=30,
        )
        del photo_bytes  # never stored — discarded as soon as the request is sent
        photo_resp = None
        resp.raise_for_status()
        result = resp.json().get("result", "unknown")

        if result == "passed":
            send_message(chat_id, "[Checkly] ✓ Confirmed — you're marked present.")
        elif result == "failed":
            send_message(
                chat_id,
                "[Checkly] Face not matched (confidence too low). "
                "Your manager has been notified.",
            )
        else:
            send_message(chat_id, f"[Checkly] Presence check result: {result}.")

        clear_pending(chat_id)

    except Exception as e:
        log.error("Failed to handle presence photo for check %s: %s", check_id, e)
        send_message(
            chat_id,
            "[Checkly] Could not process your photo. Please contact your manager.",
        )


def start_polling() -> None:
    """Start the polling loop in a daemon thread. Call once at app startup."""
    t = threading.Thread(target=_poll_loop, daemon=True, name="telegram-poll")
    t.start()
