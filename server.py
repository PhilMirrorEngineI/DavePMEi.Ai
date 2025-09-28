# server.py — DavePMEi.Ai (PMEi Memory API)
import os, json, time, threading
from flask import Flask, request, jsonify, send_from_directory, redirect
from werkzeug.exceptions import BadRequest, Unauthorized

app = Flask(__name__)

# --- Config ---
DAVEPMEI_VERSION = os.getenv("DAVEPMEI_VERSION", "1.0.0")
DAVEPMEI_HOST = os.getenv("DAVEPMEI_HOST", "philmirrorenginei.ai")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "changeme")
MEMORY_FILE = os.getenv("MEMORY_FILE", "pmei_memories.jsonl")
OPENAPI_FILENAME = os.getenv("OPENAPI_FILENAME", "openapi.json")

# --- Lock for thread safety ---
state_lock = threading.Lock()

# --- HTTPS redirect ---
@app.before_request
def enforce_https():
    if request.headers.get("X-Forwarded-Proto", "http") != "https":
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)

# --- Auth helper ---
def require_api_key():
    key = request.headers.get("X-API-KEY") or request.headers.get("MEMORY_API_KEY")
    if not key or key != MEMORY_API_KEY:
        raise Unauthorized("Missing or invalid API key")

# --- Save memory ---
@app.route("/save_memory", methods=["POST"])
def save_memory():
    require_api_key()
    data = request.get_json(force=True)

    required = ["user_id","thread_id","slide_id","glyph_echo","drift_score","seal","content"]
    for f in required:
        if f not in data:
            raise BadRequest(f"Missing field: {f}")

    data["ts"] = int(time.time())

    with state_lock:
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")

    return jsonify({"status": "saved", "slide_id": data["slide_id"]})

# --- Latest memory ---
@app.route("/latest_memory", methods=["GET"])
def latest_memory():
    require_api_key()
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if not lines:
                return jsonify({})
            return jsonify(json.loads(lines[-1]))
    except FileNotFoundError:
        return jsonify({})

# --- Get list of memories ---
@app.route("/get_memory", methods=["GET"])
def get_memory():
    require_api_key()
    limit = int(request.args.get("limit", 10))
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            items = [json.loads(l) for l in lines[-limit:]]
            return jsonify(items[::-1])  # newest first
    except FileNotFoundError:
        return jsonify([])

# --- Privacy filter ---
@app.route("/privacy_filter", methods=["POST"])
def privacy_filter():
    require_api_key()
    data = request.get_json(force=True)
    text = data.get("content", "")
    # Simple example filter (extend with regex/PII detection later)
    filtered = text.replace("password", "[REDACTED]")
    return jsonify({"filtered": filtered})

# --- Health checks ---
@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        ok=True,
        version=DAVEPMEI_VERSION,
        service="Dave PMEi memory API",
        host=DAVEPMEI_HOST,
        routes=["/health","/openapi.json","/save_memory","/latest_memory","/get_memory","/privacy_filter"],
        ts=int(time.time())
    )

@app.route("/openapi.json", methods=["GET"])
def openapi():
    return send_from_directory(".", OPENAPI_FILENAME)

# --- Run ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
