from __future__ import annotations

import datetime
import json
import os
import sys
import time
from typing import Any, Optional

from .constants import DEBUG_NDJSON_LOG_PATH, DEBUG_RUN_ID


def _log(message: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[ticket_runner {ts}] {message}", file=sys.stderr, flush=True)


def _debug_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    """
    NDJSON debug log (no secrets).
    Writes to <bugbot files>/.cursor/debug.log
    """
    try:
        payload = {
            "sessionId": "debug-session",
            "runId": DEBUG_RUN_ID,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        os.makedirs(os.path.dirname(DEBUG_NDJSON_LOG_PATH), exist_ok=True)
        with open(DEBUG_NDJSON_LOG_PATH, "ab") as f:
            f.write(json.dumps(payload).encode("utf-8", errors="replace") + b"\n")
    except Exception:
        # Never crash the script due to debug logging
        pass


def _write_log_line(log_fp: Optional[object], message: str) -> None:
    if log_fp is None:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        log_fp.write(f"[ticket_runner {ts}] {message}\n".encode("utf-8", errors="replace"))
    except Exception:
        pass


