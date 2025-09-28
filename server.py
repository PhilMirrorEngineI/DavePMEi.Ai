import os
import re
import json
import time
import threading
from collections import deque
from typing import Any, Dict, List

from flask import Flask, request, jsonify, send_from_directory, Response
from werkzeug.middleware.proxy_fix import ProxyFix

# ------------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
app.url_map.strict_slashes = False  # avoid 301/308 redirect surprises
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SERVICE_NAME = "PMEi Memory API (Dave)"
SERVICE_VERSION = "1.0.1"
HOST_HINT = os.getenv("DAVEPMEI_HOST", "philmirrorenginei.ai")

# API key config
API_KEY_ENV_NAME = "MEMORY_API_KEY"
EXPECTED_API_KEY = os.getenv(API_KEY_ENV_NAME, "")

# Memory storage (in-memory + append-only file for simple durability)
_MEM_LOCK = threading.Lock()
_MEM: deque = deque(maxlen=10000)  # newest appended to the right
_MEM_LOG_PATH = os.getenv("MEM_LOG_PATH", "memories.jsonl")  # best-effort

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _now_ts() -> int:
    return int(time.time())

def _require_api_key() -> Response | None:
    """
    Enforce presence of a valid API key in the X-API-KEY header.
    Returns a Flask Response on failure, or None if authorized.
    """
    if not EXPECTED_API_KEY:
        # Service misconfigured – treat as locked
        return jsonify({"error": "Server missing API key config"}), 500

    provided = request.headers.get("X-API-KEY") or request.headers.get("MEMORY_API_KEY")
    if not provided or provided != EXPECTED_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None

def _validate_save_payload(data: Dict[str, Any]) -> tuple[bool, str | None]:
    required_str = ["user_id", "thread_id", "slide_id", "glyph_echo", "seal", "content"]
    for k in required_str:
        if k not in data or not isinstance(data[k], str) or data[k] == "":
            return False, f"Field '{k}' is required and must be a non-empty string."

    if "drift_score" not in data or not isinstance(data["drift_score"], (int, float)):
        return False, "Field 'drift_score' is required and must be a number."

    # reject additionalProperties if present and not in known set
    allowed = set(required_str + ["drift_score"])
    extras = [k for k in data.keys() if k not in allowed]
    if extras:
        # not fatal, but align with 'additionalProperties: false' by rejecting
        return False, f"Unexpected field(s): {', '.join(sorted(extras))}."
    return True, None

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{7,}\d")

def _mask_pii(text: str) -> str:
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)[:2]}***@***", text)
    text = _PHONE_RE.sub(lambda m: "***-PII-REDACTED***", text)
    return text

def _append_memory(item: Dict[str, Any]) -> None:
    with _MEM_LOCK:
        _MEM.append(item)
        try:
            with open(_MEM_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            # best-effort; ignore file errors in stateless environments
            pass

def _latest_memory() -> Dict[str, Any] | Dict:
    with _MEM_LOCK:
        return dict(_MEM[-1]) if _MEM else {}

def _list_memories(limit: int) -> List[Dict[str, Any]]:
    with _MEM_LOCK:
        if limit <= 0:
            return []
        # newest are at the right; slice from the end
        slice_ = list(_MEM)[-limit:]
        # return newest first
        slice_.reverse()
        return slice_

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.get("/health")
def health() -> Response:
    return jsonify({
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "host": HOST_HINT,
        "ts": _now_ts(),
    })

@app.get("/healthz")
def healthz() -> Response:
    return health()

@app.get("/openapi.json")
def openapi_json() -> Response:
    """
    Serve the OpenAPI file from the same directory as server.py.
    Ensure openapi.json is committed at the repo root.
    """
    try:
        here = os.path.abspath(os.path.dirname(__file__))
        return send_from_directory(here, "openapi.json", mimetype="application/json")
    except Exception as e:
        return jsonify({"error": f"openapi.json not found: {e}"}), 404

@app.post("/save_memory")
def save_memory() -> Response:
    # Auth
    unauthorized = _require_api_key()
    if unauthorized:
        return unauthorized

    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json(silent=True) or {}
    ok, err = _validate_save_payload(data)
    if not ok:
        return jsonify({"error": "invalid_payload", "detail": err}), 400

    item = {
        "user_id": data["user_id"],
        "thread_id": data["thread_id"],
        "slide_id": data["slide_id"],
        "glyph_echo": data["glyph_echo"],
        "drift_score": float(data["drift_score"]),
        "seal": data["seal"],
        "content": data["content"],
        "ts": _now_ts(),
    }
    _append_memory(item)
    return jsonify({"status": "saved", "slide_id": item["slide_id"]})

@app.get("/latest_memory")
def latest_memory() -> Response:
    unauthorized = _require_api_key()
    if unauthorized:
        return unauthorized

    item = _latest_memory()
    # Per spec, either MemoryItem or {}
    return jsonify(item)

@app.get("/get_memory")
def get_memory() -> Response:
    unauthorized = _require_api_key()
    if unauthorized:
        return unauthorized

    try:
        limit = int(request.args.get("limit", "10"))
    except ValueError:
        return jsonify({"error": "invalid_limit"}), 400

    # clamp to [1,200]
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    items = _list_memories(limit)
    return jsonify(items)

@app.post("/privacy_filter")
def privacy_filter() -> Response:
    unauthorized = _require_api_key()
    if unauthorized:
        return unauthorized

    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    if not isinstance(content, str):
        return jsonify({"error": "invalid_payload", "detail": "content must be a string"}), 400

    masked = _mask_pii(content)
    return jsonify({"filtered_content": masked})

# ------------------------------------------------------------------------------
# WSGI entrypoint
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    # Useful for local testing: MEMORY_API_KEY must be set.
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
