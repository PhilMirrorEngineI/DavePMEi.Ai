# --- Flask JSON + CORS + Auth Handshake ---

from flask import Flask, request, jsonify
from functools import wraps
import os, time

app = Flask(__name__)

MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "")  # set in Render dashboard
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*") # e.g. https://your-vercel-app.vercel.app

# 1) Always reply JSON (never HTML error pages)
@app.errorhandler(Exception)
def handle_error(e):
    code = getattr(e, "code", 500)
    return jsonify({"ok": False, "error": str(e), "code": code}), code

# 2) Simple auth decorator
def require_key(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        key = request.headers.get("X-API-KEY", "")
        if not key or key != MEMORY_API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped

# 3) CORS for your Vercel app
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-KEY"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/save_memory", methods=["POST","OPTIONS"])
@require_key
def save_memory():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    required = ["user_id","thread_id","slide_id","glyph_echo","drift_score","seal","content"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Missing fields: {missing}"}), 400
    # TODO: persist to sqlite/jsonl here
    saved = {**data, "ok": True, "saved_at": int(time.time())}
    return jsonify(saved), 201

@app.route("/get_memory", methods=["GET","OPTIONS"])
@require_key
def get_memory():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    user_id = request.args.get("user_id","").strip()
    limit   = int(request.args.get("limit","5"))
    if not user_id:
        return jsonify({"ok": False, "error": "user_id required"}), 400
    # TODO: load from storage; THIS IS A MOCK RESPONSE SHAPE
    rows = []  # replace with real rows
    return jsonify({"ok": True, "items": rows[:limit], "count": len(rows[:limit])}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
