# server.py — PMEi Memory API (Dave)
# Minimal, strict, privacy-first memory shard service with JSON-file storage on a Render Disk.

import os, re, json, time, uuid, threading, shutil
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from flask import Flask, request, jsonify, Response, send_file

# -------------------------------
# App & configuration
# -------------------------------
app = Flask(__name__)

# Storage / config (env overrides; defaults point at your Render Disk)
MEMORY_FILE       = os.environ.get("MEMORY_FILE", "/data/memory.json")
OPENAPI_FILENAME  = os.environ.get("OPENAPI_FILENAME", "openapi.json")
EXPECTED_API_KEY  = os.environ.get("MEMORY_API_KEY", "").strip()
ALLOWED_HOSTS     = {h.strip().lower() for h in os.environ.get("DAVEPMEI_ALLOWED_HOSTS", "").split(",") if h.strip()}

RATE_LIMIT_WINDOW_SEC = int(os.environ.get("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX        = int(os.environ.get("RATE_LIMIT_MAX", "120"))

# Simple in-process locks/state
_file_lock = threading.Lock()
_rate_lock = threading.Lock()
_rate_window: Dict[str, List[float]] = {}

# Ensure storage dir exists
os.makedirs(os.path.dirname(MEMORY_FILE) or ".", exist_ok=True)

# Initialize file if not present
if not os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)

def _now_ts() -> int:
    return int(time.time())

def _read_all() -> List[Dict[str, Any]]:
    with _file_lock:
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                return []
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []

def _write_all(items: List[Dict[str, Any]]) -> None:
    tmp = MEMORY_FILE + ".tmp"
    with _file_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        shutil.move(tmp, MEMORY_FILE)

def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "0.0.0.0").split(",")[0].strip()

def _rate_limit_ok() -> bool:
    now = time.time()
    key = _client_ip()
    with _rate_lock:
        window = _rate_window.setdefault(key, [])
        # drop old
        cutoff = now - RATE_LIMIT_WINDOW_SEC
        _rate_window[key] = [t for t in window if t >= cutoff]
        if len(_rate_window[key]) >= RATE_LIMIT_MAX:
            return False
        _rate_window[key].append(now)
        return True

def _need_auth() -> Optional[Response]:
    # allow health & openapi without auth
    if request.endpoint in ("health", "healthz", "openapi_json"):
        return None
    # allowed host (optional)
    if ALLOWED_HOSTS:
        host = (request.host or "").split(":")[0].lower()
        if host not in ALLOWED_HOSTS:
            return _err("forbidden host", "forbidden", 403)
    # api key
    key = request.headers.get("X-API-KEY", "").strip()
    if not EXPECTED_API_KEY or key != EXPECTED_API_KEY:
        return _err("Missing or invalid API key", "unauthorized", 401)
    return None

@app.before_request
def _gate():
    if not _rate_limit_ok():
        return _err("rate limit", "too_many_requests", 429)
    auth = _need_auth()
    if auth is not None:
        return auth

def _err(msg: str, code: str, status: int):
    rid = uuid.uuid4().hex
    return jsonify({"error": msg, "code": code, "request_id": rid}), status

# -------------------------------
# Privacy filter (very minimal)
# -------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d \-]{7,}\d")

def privacy_mask(text: str) -> str:
    text = EMAIL_RE.sub("***-PII-REDACTED***", text)
    text = PHONE_RE.sub("***-PII-REDACTED***", text)
    return text

# -------------------------------
# Routes
# -------------------------------
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "Dave PMEi memory API",
        "version": "1.0.3",
        "host": request.host,
        "routes": ["/health", "/healthz", "/openapi.json", "/save_memory", "/latest_memory", "/get_memory", "/privacy_filter"],
        "ts": _now_ts(),
    })

@app.get("/healthz")
def healthz():
    return health()

@app.get("/openapi.json")
def openapi_json():
    # serve the checked-in file (keeps the Actions schema aligned)
    if not os.path.exists(OPENAPI_FILENAME):
        return _err("openapi not found", "not_found", 404)
    return send_file(OPENAPI_FILENAME, mimetype="application/json")

@app.post("/save_memory")
def save_memory():
    try:
        data = request.get_json(force=True, silent=False) or {}
    except Exception:
        return _err("invalid json", "bad_request", 400)

    required = ["user_id", "thread_id", "slide_id", "glyph_echo", "drift_score", "seal", "content"]
    missing = [k for k in required if k not in data]
    if missing:
        return _err(f"missing fields: {', '.join(missing)}", "bad_request", 400)

    # coerce types
    try:
        drift = float(data["drift_score"])
    except Exception:
        return _err("drift_score must be a number", "bad_request", 400)

    item = {
        "user_id":   str(data["user_id"]),
        "thread_id": str(data["thread_id"]),
        "slide_id":  str(data["slide_id"]),
        "glyph_echo":str(data["glyph_echo"]),
        "drift_score": drift,
        "seal":     str(data["seal"]),
        "content":  str(data["content"]),
        "ts":       _now_ts()
    }

    items = _read_all()
    items.append(item)              # append; newest is last
    _write_all(items)

    return jsonify({
        "status": "ok",
        "slide_id": item["slide_id"],
        "ts": item["ts"],
        "request_id": uuid.uuid4().hex
    })

@app.get("/latest_memory")
def latest_memory():
    items = _read_all()
    if not items:
        return jsonify({})
    return jsonify(items[-1])

@app.get("/get_memory")
def get_memory():
    # filters: user_id, thread_id, slide_id, seal  | limit
    user_id   = request.args.get("user_id")
    thread_id = request.args.get("thread_id")
    slide_id  = request.args.get("slide_id")
    seal      = request.args.get("seal")
    try:
        limit = int(request.args.get("limit", "10"))
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 10

    items = _read_all()

    def keep(it):
        if user_id   and it.get("user_id")   != user_id:   return False
        if thread_id and it.get("thread_id") != thread_id: return False
        if slide_id  and it.get("slide_id")  != slide_id:  return False
        if seal      and it.get("seal")      != seal:      return False
        return True

    filtered = [it for it in items if keep(it)]
    # newest first
    filtered = list(reversed(filtered))[:limit]
    return jsonify(filtered)

@app.post("/privacy_filter")
def run_privacy_filter():
    try:
        data = request.get_json(force=True, silent=False) or {}
    except Exception:
        return _err("invalid json", "bad_request", 400)

    content = str(data.get("content", ""))
    return jsonify({"filtered_content": privacy_mask(content)})

# Entrypoint
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
