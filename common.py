"""Shared constants and helpers used by more than one pipeline script.

Kept deliberately small: anything that lives here must be true for the whole
pipeline, not just one component (see AGENTS.md). alert.py's persisted rules
and api.py's rolling hints both read from it so they can never drift apart.
"""

import json
import os
from pathlib import Path

# External traffic only — LAN (192.168.*) is trusted by design of this setup.
# Single source of truth for the "external" predicate: every query that must
# ignore homelab traffic uses EXT, so tuning the trust boundary happens here
# once instead of in each rule's SQL.
EXT = "remote_ip IS NOT NULL AND remote_ip NOT LIKE '192.168%'"

# External requests per hour that count as "hot" (info-level finding / hint).
HOT_IP_THRESHOLD = 50


def atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic write: temp file + rename, so a crash never corrupts the file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
