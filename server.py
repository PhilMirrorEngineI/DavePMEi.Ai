# server.py — Dave / PMEi Flask service (Render-ready)
# routes: /health, /healthz, /openapi.json, /save_memory, /latest_memory, /get_memory, /privacy_filter

from __future__ import annotations
import os, time, uuid, json, re, threading
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, request, jsonify, make_response, redirect
from werkzeug.exceptions import BadRequest

# ---------------------------
# Config (env)
# ---------------------------
APP_PORT         = int(os.getenv("PORT", "8080"))
DAVEPMEI_VERSION = os.getenv("DAVEPMEI_VERSION", "1.0.2")
DAVEPMEI_HOST    = os.getenv("DAVEPMEI_HOST", "philmirrorenginei.ai")
DAVEPMEI_HOSTS   = [h.strip() for h in os.getenv("DAVEPMEI_HOSTS", f"{DAVEPMEI_HOST},www.{DAVEPMEI_HOST}").split(",") if h.strip()]
DAVEPMEI_ALLOWED = [h.strip() for h in os.getenv("DAVEPMEI_ALLOWED_HOSTS", ",".join(DAVEPMEI_HOSTS)).split(",") if h.strip()]

MEMORY_API_KEY   = os.getenv("MEMORY_API_KEY", "")
MEMORY_FILE      = os.getenv("MEMORY_FILE", "pmei_memories.jsonl")
OPENAPI_FILENAME = os.getenv("OPENAPI_FILENAME", "openapi.json")

# ---------------------------
# App
# ---------------------------
app = Flask(__name__)
app.url_map.strict_slashes = False
_write_lock = threading.Lock()

# ---------------------------
# Helpers
# ---------------------------
def _req_id() -> str:
    return request.headers.get("X-Request-ID", str(uuid.uuid4()))

def _now() -> int:
    return int(time.time())

def _incoming_key() -> Optional[str]:
    # Accept both spellings; last resort ?key= for manual tests.
    return (
        request.headers.get("X-API-KEY")
        or request.headers.get("X_API_KEY")
        or request.headers.get("MEMORY_API_KEY")
        or request.args.get("key")
    )

def _json_error(code: int, msg: str):
    rid = _req_id()
    resp = jsonify({"error": msg, "code": str(code), "request_id": rid})
    resp.status_code = code
    resp.headers["X-Request-ID"] = rid
    return resp

def _add_common_headers(resp):
    resp.headers.setdefault("X-Request-ID", _req_id())
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, X-API-KEY, MEMORY_API_KEY, X-Request-ID")
    if request.path == "/openapi.json":
        resp.headers.setdefault("Cache-Control", "public, max-age=600, immutable")
    else:
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp

@app.after_request
def _after(resp):
    return _add_common_headers(resp)

# ---------------------------
# Gate: auth + open routes
# ---------------------------
OPEN_ROUTES = {"/health", "/healthz", "/openapi.json"}

@app.before_request
def _gate():
    # Allow health & schema without key
    if request.path in OPEN_ROUTES or request.method == "OPTIONS":
        return None
    if not MEMORY_API_KEY:
        return _json_error(500, "Server missing MEMORY_API_KEY")
    key = _incoming_key()
    if not key or key != MEMORY_API_KEY:
        return _json_error(403, "forbidden")
    return None

# ---------------------------
# Storage (newline-delimited JSON)
# ---------------------------
def _append_jsonl(item: Dict[str, Any]):
    os.makedirs(os.path.dirname(MEMORY_FILE) or ".", exist_ok=True)
    line = json.dumps(item, ensure_ascii=False)
    with _write_lock:
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")

def _iter_jsonl() -> Iterable[Dict[str, Any]]:
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except FileNotFoundError:
        return

def _apply_filters(items: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    q_user   = request.args.get("user_id")
    q_thread = request.args.get("thread_id")
    q_slide  = request.args.get("slide_id")
    q_seal   = request.args.get("seal")
    for it in items:
        if q_user   and it.get("user_id")   != q_user:   continue
        if q_thread and it.get("thread_id") != q_thread: continue
        if q_slide  and it.get("slide_id")  != q_slide:  continue
        if q_seal   and it.get("seal")      != q_seal:   continue
        yield it

# ---------------------------
# Validation
# ---------------------------
REQUIRED_FIELDS = ["user_id", "thread_id", "slide_id", "glyph_echo", "drift_score", "seal", "content"]

def _validate_save_payload(d: Dict[str, Any]):
    missing = [k for k in REQUIRED_FIELDS if k not in d]
    if missing:
        raise BadRequest(f"Missing fields: {', '.join(missing)}")
    if not isinstance(d.get("drift_score"), (int, float)):
        raise BadRequest("drift_score must be a number.")
    for k in ["user_id","thread_id","slide_id","glyph_echo","seal","content"]:
        if not isinstance(d.get(k), str) or not d[k]:
            raise BadRequest(f"{k} must be a non-empty string.")

# ---------------------------
# Routes
# ---------------------------
@app.get("/health")
def health():
    return jsonify(
        status="ok",
        service="DavePMEi.Ai",
        version=DAVEPMEI_VERSION,
        host=DAVEPMEI_HOST,
        routes=["/health","/healthz","/openapi.json","/save_memory","/latest_memory","/get_memory","/privacy_filter"],
        ts=_now(),
    ), 200

@app.get("/healthz")
def healthz():
    # Simple liveness probe mirrors /health
    return health()

@app.get("/openapi.json")
def serve_openapi():
    try:
        with open(OPENAPI_FILENAME, "r", encoding="utf-8") as f:
            data = f.read()
        resp = make_response(data)
        resp.mimetype = "application/json"
        return resp
    except FileNotFoundError:
        # minimal fallback to keep tools alive if file missing
        return jsonify({"openapi":"3.1.0","info":{"title":"PMEi Memory API","version":DAVEPMEI_VERSION},"servers":[{"url":f"https://{DAVEPMEI_HOST}"}]}), 200

@app.post("/save_memory")
def save_memory():
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            raise BadRequest("Body must be a JSON object.")
        _validate_save_payload(payload)
    except BadRequest as e:
        return _json_error(400, str(e))

    ts = _now()
    item = {
        "user_id":     payload["user_id"],
        "thread_id":   payload["thread_id"],
        "slide_id":    payload["slide_id"],
        "glyph_echo":  payload["glyph_echo"],
        "drift_score": float(payload["drift_score"]),
        "seal":        payload["seal"],
        "content":     payload["content"],
        "ts":          ts,
    }
    _append_jsonl(item)
    rid = _req_id()
    resp = jsonify({"status":"ok","slide_id": item["slide_id"], "ts": ts, "request_id": rid})
    resp.headers["X-Request-ID"] = rid
    return resp, 200

@app.get("/latest_memory")
def latest_memory():
    latest: Optional[Dict[str, Any]] = None
    for it in _apply_filters(_iter_jsonl()):
        if latest is None or int(it.get("ts", 0)) > int(latest.get("ts", 0)):
            latest = it
    return jsonify(latest or {}), 200

@app.get("/get_memory")
def get_memory():
    try:
        limit = int(request.args.get("limit", "10"))
    except ValueError:
        limit = 10
    limit = max(1, min(200, limit))

    items = list(_apply_filters(_iter_jsonl()))
    items.sort(key=lambda x: int(x.get("ts", 0)), reverse=True)
    return jsonify(items[:limit]), 200

# --- Privacy filter (mask emails/phones) ---
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{4}\b")

def _mask(text: str) -> str:
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text

@app.post("/privacy_filter")
def privacy_filter():
    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict):
            raise BadRequest("Body must be a JSON object.")
        content = data.get("content")
        if not isinstance(content, str) or not content:
            raise BadRequest("content must be a non-empty string.")
    except BadRequest as e:
        return _json_error(400, str(e))
    return jsonify({"filtered_content": _mask(content)}), 200

# --- Optional: canonical host redirect (keeps links tidy on custom domain)
@app.before_request
def _enforce_host_redirect():
    host = request.host.split(":")[0]
    if DAVEPMEI_ALLOWED and host not in DAVEPMEI_ALLOWED and request.path not in OPEN_ROUTES:
        parts = urlsplit(request.url)
        target = urlunsplit(("https", DAVEPMEI_HOST, parts.path, parts.query, parts.fragment))
        return redirect(target, code=308)

# Entry
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT, debug=False)
