"""Shared utilities used by main.py and presence.py."""

import hashlib
import json


def compute_row_hash(prev_hash: str | None, payload: dict) -> str:
    """SHA-256 chain: each row hashes its content + the previous row's hash."""
    content = (prev_hash or "") + json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(content.encode()).hexdigest()
