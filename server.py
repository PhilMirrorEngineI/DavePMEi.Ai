# server.py — PMEi Memory API (Dave)
# ----------------------------------
# Minimal, strict, privacy-first memory shard service with:
# - Nightly backups (JSONL snapshots)
# - /metrics endpoint
# - Simple per-IP rate limiting

import os
import re
import json
import time
import shutil
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from flask import Flask, request, jsonify, Response, send_file

# -----------------------
# Config & global state
# -----------------------
app = Flask(__name__)

# Storage/config
MEMORY_FILE = os.environ.get("MEMORY_FILE", "pmei_memories.jsonl")
OPENAPI_FILENAME = os.environ.get("OPENAPI_FILENAME", "openapi.json")
EXPECTED_API_KEY = os.environ.get("MEMORY_API_KEY", "").strip()
ALLOWED_HOSTS = set(h.strip().lower() for h in os.environ.get("DAVEPMEI_ALLOWED_HOSTS", "").split(",") if h.strip())

# Backups
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
# Hour (UTC) to run nightly backup; default 03:00 UTC
BACKUP_HOUR_UTC = int(os.environ.get("BACKUP_HOUR_UTC", "3"))

# Rate limiting (per-IP)
RATE_LIMIT_WINDOW_SEC = int(os.environ.get("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "120"))

LOCK = threading.Lock()
LATEST: Optional[Dict[str, Any]] = None  # in-process cache of most recent shard

# Metrics (very simple in-memory counters)
METRICS = {
    "started_ts": int(time.time()),
    "total_requests": 0,
    "saved_memories": 0,
    "errors_4xx": 0,
    "errors_5xx": 0,
    "rate_limited": 0,
}

# Rate-limit buckets: ip -> list[timestamps]
RL_BUCKETS: Dict[str, List[float]] = {}
RL_LOCK = threading.Lock()


# -----------------------
# Helpers
# -----------------------
def ts_now_ms() -> int:
    return int(time.time() * 1000)


def req_id() -> str:
    return os.urandom(6).hex()


def no_store(resp: Response) -> Response:
    """Disable caching on responses that must be fresh."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def get_api_key_from_request() -> str:
    """
    Accept either:
      - X-API-KEY: <key>      (preferred)
      - MEMORY_API_KEY: <key> (legacy/compat)
    """
    return (
        request.headers.get("X-API-KEY")
        or request.headers.get("x-api-key")
        or request.headers.get("MEMORY_API_KEY")
        or request.headers.get("memory_api_key")
        or ""
    ).strip()


def require_auth() -> Optional[Response]:
    if not EXPECTED_API_KEY:
        # Service misconfigured — be explicit
        return jsonify({
            "error": "Server missing MEMORY_API_KEY",
            "code": "config_error",
            "request_id": req_id(),
        }), 500

    provided = get_api_key_from_request()
    if not provided or provided != EXPECTED_API_KEY:
        return jsonify({
            "error": "Missing or invalid API key",
            "code": "unauthorized",
            "request_id": req_id(),
        }), 401

    # Optional: host allowlist (mostly for reverse proxy/CDN checks)
    if ALLOWED_HOSTS:
        host = (request.headers.get("Host") or "").lower()
        if host and host not in ALLOWED_HOSTS:
            return jsonify({
                "error": "Host not allowed",
                "code": "forbidden",
                "request_id": req_id(),
            }), 403

    return None


def append_jsonl(item: Dict[str, Any]) -> None:
    """Append one JSON object to the JSONL file, flush and fsync for durability."""
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_all_jsonl() -> List[Dict[str, Any]]:
    if not os.path.exists(MEMORY_FILE):
        return []
    out: List[Dict[str, Any]] = []
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                out.append(json.loads(s))
            except Exception:
                continue
    return out


def read_last_line_or_cached() -> Optional[Dict[str, Any]]:
    global LATEST
    if LATEST:
        return LATEST
    if not os.path.exists(MEMORY_FILE):
        return None
    try:
        with open(MEMORY_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            # Walk backward to previous newline, then read final line
            back = 1
            while size - back >= 0:
                f.seek(-back, os.SEEK_END)
                if f.read(1) == b"\n" and back != 1:
                    break
                back += 1
            line = f.readline().decode("utf-8").strip()
            if not line:
                return None
            return json.loads(line)
    except Exception:
        return None


# --- simple privacy filter (email & phone) ---
EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+?\d[\s-]?){7,15}\b")


def mask_pii(text: str) -> str:
    text = EMAIL_RE.sub(lambda m: "***PII-EMAIL***", text)
    text = PHONE_RE.sub(lambda m: "***PII-PHONE***", text)
    return text


# -----------------------
# Rate limiting hooks
# -----------------------
@app.before_request
def _before_request_metrics_and_rl():
    # metrics: count request
    METRICS["total_requests"] += 1

    # allow-list some public endpoints from RL
    path = request.path or ""
    if path in ("/health", "/healthz", "/openapi.json"):
        return

    # very simple sliding-window per-IP limiter
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()
    with RL_LOCK:
        bucket = RL_BUCKETS.get(ip)
        if bucket is None:
            bucket = []
            RL_BUCKETS[ip] = bucket
        # drop timestamps outside window
        cutoff = now - RATE_LIMIT_WINDOW_SEC
        i = 0
        while i < len(bucket) and bucket[i] < cutoff:
            i += 1
        if i:
            del bucket[:i]
        # check capacity
        if len(bucket) >= RATE_LIMIT_MAX:
            METRICS["rate_limited"] += 1
            return jsonify({
                "error": "rate_limited",
                "code": "too_many_requests",
                "request_id": req_id(),
                "window_sec": RATE_LIMIT_WINDOW_SEC,
                "limit": RATE_LIMIT_MAX
            }), 429
        # record
        bucket.append(now)


@app.after_request
def _after_request_status_counts(resp: Response):
    try:
        code = int(resp.status_code)
        if 400 <= code < 500:
            METRICS["errors_4xx"] += 1
        elif code >= 500:
            METRICS["errors_5xx"] += 1
    except Exception:
        pass
    return resp


# -----------------------
# Nightly backup daemon
# -----------------------
def _seconds_until_next_backup_utc(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _run_backup_daemon():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    while True:
        try:
            # sleep until the next scheduled time (UTC hour)
            time.sleep(_seconds_until_next_backup_utc(BACKUP_HOUR_UTC))

            # build snapshot filename
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            dst = os.path.join(BACKUP_DIR, f"backup_{stamp}.jsonl")

            # perform copy under lock to avoid partial line
            with LOCK:
                if os.path.exists(MEMORY_FILE):
                    shutil.copyfile(MEMORY_FILE, dst)
        except Exception:
            # never crash the daemon; wait 60s and try again
            time.sleep(60)


# fire-and-forget background thread
threading.Thread(target=_run_backup_daemon, name="backup-daemon", daemon=True).start()


# -----------------------
# Routes
# -----------------------
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "Dave PMEi memory API",
        "ts": ts_now_ms(),
        "routes": ["/health", "/healthz", "/openapi.json", "/save_memory", "/latest_memory", "/get_memory", "/privacy_filter", "/metrics"],
        "rate_limit_window_sec": RATE_LIMIT_WINDOW_SEC,
        "rate_limit_max": RATE_LIMIT_MAX,
    })


@app.get("/healthz")
def healthz():
    return health()


@app.get("/openapi.json")
def openapi_json():
    if not os.path.exists(OPENAPI_FILENAME):
        return jsonify({"error": f"{OPENAPI_FILENAME} not found"}), 404
    # Spec can be cached by clients
    return send_file(OPENAPI_FILENAME, mimetype="application/json")


@app.get("/metrics")
def metrics():
    # Simple read-only metrics (public). Add auth if you prefer.
    uptime = int(time.time()) - METRICS["started_ts"]
    data = {
        "uptime_seconds": uptime,
        "total_requests": METRICS["total_requests"],
        "saved_memories": METRICS["saved_memories"],
        "errors_4xx": METRICS["errors_4xx"],
        "errors_5xx": METRICS["errors_5xx"],
        "rate_limited": METRICS["rate_limited"],
        "ts": ts_now_ms()
    }
    return jsonify(data)


@app.post("/save_memory")
def save_memory():
    # Auth
    auth_err = require_auth()
    if auth_err:
        return auth_err

    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({"error": f"invalid json: {e}", "code": "bad_request", "request_id": req_id()}), 400

    required = ["user_id", "thread_id", "slide_id", "glyph_echo", "drift_score", "seal", "content"]
    missing = [k for k in required if k not in payload]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}", "code": "bad_request", "request_id": req_id()}), 400

    # Stamp and persist
    shard = dict(payload)
    shard["ts"] = ts_now_ms()
    shard["request_id"] = req_id()

    with LOCK:
        append_jsonl(shard)
        global LATEST
        LATEST = shard
        METRICS["saved_memories"] += 1

    return jsonify({
        "status": "ok",
        "slide_id": shard.get("slide_id"),
        "ts": shard["ts"],
        "request_id": shard["request_id"],
    })


@app.get("/latest_memory")
def latest_memory():
    # Auth
    auth_err = require_auth()
    if auth_err:
        return auth_err

    item = read_last_line_or_cached()
    return no_store(jsonify(item or {}))


@app.get("/get_memory")
def get_memory():
    # Auth
    auth_err = require_auth()
    if auth_err:
        return auth_err

    try:
        limit = int(request.args.get("limit", 10))
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 10

    user_id = request.args.get("user_id")
    thread_id = request.args.get("thread_id")
    slide_id = request.args.get("slide_id")
    seal = request.args.get("seal")

    items = read_all_jsonl()

    # Optional filtering
    def ok(it: Dict[str, Any]) -> bool:
        if user_id and it.get("user_id") != user_id:
            return False
        if thread_id and it.get("thread_id") != thread_id:
            return False
        if slide_id and it.get("slide_id") != slide_id:
            return False
        if seal and it.get("seal") != seal:
            return False
        return True

    items = [it for it in items if ok(it)]
    # newest first
    items = items[-limit:][::-1]
    return no_store(jsonify(items))


@app.post("/privacy_filter")
def privacy_filter():
    # Auth
    auth_err = require_auth()
    if auth_err:
        return auth_err

    data = request.get_json(force=True, silent=True) or {}
    content = str(data.get("content", ""))
    filtered = mask_pii(content)
    return jsonify({"filtered_content": filtered})


# -----------------------
# Entrypoint (local dev)
# -----------------------
if __name__ == "__main__":
    # For Render/Prod use gunicorn:
    #   web: gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 60
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
