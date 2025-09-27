# server.py  — DavePMEi.Ai (PMEi) render service
import os, json, time, threading
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, redirect
from werkzeug.exceptions import Unauthorized, BadRequest
from werkzeug.middleware.proxy_fix import ProxyFix

# ── Config (env) ────────────────────────────────────────────────────────────────
APP_PORT = int(os.getenv("PORT", "8000"))  # Render provides $PORT

# Brand / parent
DAVEPMEI_VERSION = os.getenv("DAVEPMEI_VERSION", "1.0.0")
# legacy single-host (kept for backward compat; not used if DAVEPMEI_HOSTS is set)
DAVEPMEI_HOST    = os.getenv("DAVEPMEI_HOST", "davepmei.ai")
PARENT_SERVICE   = "PhilMirrorEnginei.ai (PMEi)"

# Multi-host support (comma-separated list). If set, this supersedes DAVEPMEI_HOST.
DAVEPMEI_HOSTS = [h.strip().lower() for h in os.getenv("DAVEPMEI_HOSTS", "").split(",") if h.strip()]

# CORS (comma-separated list of allowed origins)
DAVEPMEI_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("DAVEPMEI_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# Auth + storage
MEMORY_API_KEY   = os.getenv("MEMORY_API_KEY")                # set in Render
MEMORY_FILE      = os.getenv("MEMORY_FILE", "pmei_memories.jsonl")
OPENAPI_FILENAME = os.getenv("OPENAPI_FILENAME", "openapi.json")

# ── App ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app)  # respect X-Forwarded-* from Render
_write_lock = threading.Lock()

# ── Helpers ────────────────────────────────────────────────────────────────────
def require_api_key():
    key = request.headers.get("X-API-KEY")
    if not key or not MEMORY_API_KEY or key != MEMORY_API_KEY:
        raise Unauthorized("Invalid or missing X-API-KEY.")

def validate_payload(d: dict):
    required = ["user_id","thread_id","slide_id","glyph_echo","drift_score","seal","content"]
    missing = [k for k in required if k not in d]
    if missing:
        raise BadRequest(f"Missing field(s): {', '.join(missing)}")
    if not isinstance(d.get("drift_score"), (int, float)):
        raise BadRequest("drift_score must be a number.")

def _load_last_n(n: int):
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if n > 0:
            lines = lines[-n:]
        return [json.loads(line) for line in lines]
    except FileNotFoundError:
        return []

# ── Host handling ──────────────────────────────────────────────────────────────
@app.before_request
def allow_multiple_hosts():
    """
    Accept requests on any host listed in DAVEPMEI_HOSTS.
    If DAVEPMEI_HOSTS is empty, fall back to legacy DAVEPMEI_HOST single-canonical redirect.
    If a different host is used, 301 to the first allowed host (or DAVEPMEI_HOST).
    """
    host = (request.headers.get("Host") or "").lower()

    if DAVEPMEI_HOSTS:
        if host and host not in DAVEPMEI_HOSTS:
            target = DAVEPMEI_HOSTS[0]
            return redirect(request.url.replace(f"//{host}", f"//{target}", 1), code=301)
        return  # allowed host → proceed
    # fallback: legacy single-host enforcement
    if host and DAVEPMEI_HOST and host != DAVEPMEI_HOST:
        return redirect(request.url.replace(f"//{host}", f"//{DAVEPMEI_HOST}", 1), code=301)

# ── Security, CORS, provenance headers ─────────────────────────────────────────
@app.after_request
def set_headers(resp):
    origin = request.headers.get("Origin")
    if origin and origin in DAVEPMEI_ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-KEY"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    # Branding / provenance
    resp.headers["X-DavePMEi-Origin"] = "render"
    resp.headers["X-DavePMEi-Version"] = DAVEPMEI_VERSION
    resp.headers["X-Parent-Service"] = PARENT_SERVICE
    # Hardening
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'none'"
    return resp

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "service": "DavePMEi.Ai",
        "parent": PARENT_SERVICE,
        "version": DAVEPMEI_VERSION,
        "time": datetime.utcnow().isoformat()+"Z",
        "routes": ["/health","/openapi.json","/save_memory","/latest_memory","/get_memory"]
    })

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "DavePMEi.Ai",
        "parent": PARENT_SERVICE,
        "version": DAVEPMEI_VERSION
    })

@app.get("/openapi.json")
def openapi_spec():
    directory = os.path.abspath(os.path.dirname(__file__))
    return send_from_directory(directory, OPENAPI_FILENAME, mimetype="application/json")

@app.post("/save_memory")
def save_memory():
    require_api_key()
    data = request.get_json(silent=True) or {}
    validate_payload(data)
    data["ts"] = int(time.time())
    with _write_lock:
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    return jsonify({"status": "saved", "slide_id": data["slide_id"]})

@app.get("/latest_memory")
def latest_memory():
    require_api_key()
    items = _load_last_n(1)
    return jsonify(items[0] if items else {})

@app.get("/get_memory")
def get_memory():
    require_api_key()
    limit_s = request.args.get("limit", "10")
    try:
        limit = int(limit_s)
    except ValueError:
        raise BadRequest("limit must be an integer")
    items = _load_last_n(limit)
    return jsonify(list(reversed(items)) if items else [])

# Optional alias to match earlier docs: POST /memory → /save_memory
@app.post("/memory")
def memory_alias():
    return save_memory()

# ── Local run (Render uses Procfile) ───────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
