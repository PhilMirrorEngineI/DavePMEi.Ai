import os
import re
import json
import time
import uuid
import sqlite3
from flask import Flask, request, jsonify, g, Response

# -----------------------------
# Config
# -----------------------------
MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "").strip()

# Prefer Render Disk at /data; fall back to local file
DEFAULT_DB_PATH = "/data/dave.sqlite3" if os.path.isdir("/data") else "./dave.sqlite3"
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)

OPENAPI_PATH = os.environ.get("OPENAPI_PATH", "./openapi.json")

app = Flask(__name__)

# Ensure DB directory exists (no-op if already there)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# -----------------------------
# DB helpers
# -----------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            thread_id  TEXT NOT NULL,
            slide_id   TEXT NOT NULL,
            glyph_echo TEXT NOT NULL,
            drift_score REAL NOT NULL,
            seal       TEXT NOT NULL,
            content    TEXT NOT NULL,
            ts         INTEGER NOT NULL
        );
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts     ON memories(ts DESC);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mem_thread ON memories(thread_id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mem_user   ON memories(user_id);")
    db.commit()

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# -----------------------------
# Auth
# -----------------------------
def unauthorized():
    return jsonify({
        "error": "Missing or invalid API key",
        "code": "unauthorized",
        "request_id": str(uuid.uuid4())
    }), 401

@app.before_request
def auth_gate():
    # Open endpoints: health + schema
    if request.path in ("/health", "/healthz", "/openapi.json"):
        return
    # Everything else requires X-API-KEY (dash, not underscore)
    api_key = request.headers.get("X-API-KEY")
    if not api_key or api_key != MEMORY_API_KEY:
        return unauthorized()

# -----------------------------
# Utilities
# -----------------------------
def row_to_memory_item(row: sqlite3.Row) -> dict:
    return {
        "user_id": row["user_id"],
        "thread_id": row["thread_id"],
        "slide_id": row["slide_id"],
        "glyph_echo": row["glyph_echo"],
        "drift_score": row["drift_score"],
        "seal": row["seal"],
        "content": row["content"],
        "ts": row["ts"],
    }

def validate_payload(required_fields, data):
    missing = [k for k in required_fields if k not in data]
    if missing:
        return False, f"Missing required fields: {', '.join(missing)}"
    return True, ""

# Minimal PII masking
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

def privacy_mask(text: str) -> str:
    text = EMAIL_RE.sub("[redacted-email]", text or "")
    text = PHONE_RE.sub("[redacted-phone]", text)
    return text

# -----------------------------
# Routes
# -----------------------------
@app.route("/health")
@app.route("/healthz")
def health():
    return jsonify({"status": "ok"})

@app.route("/openapi.json")
def openapi_file():
    try:
        with open(OPENAPI_PATH, "r", encoding="utf-8") as f:
            payload = f.read()
        return Response(payload, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": f"openapi.json not found or unreadable: {e}"}), 500

@app.route("/save_memory", methods=["POST"])
def save_memory():
    data = request.get_json(silent=True) or {}
    ok, err = validate_payload(
        ["user_id", "thread_id", "slide_id", "glyph_echo", "drift_score", "seal", "content"],
        data
    )
    if not ok:
        return jsonify({"error": err, "code": "bad_request", "request_id": str(uuid.uuid4())}), 400

    ts = int(time.time())
    db = get_db()
    db.execute(
        """
        INSERT INTO memories (user_id, thread_id, slide_id, glyph_echo, drift_score, seal, content, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            data["user_id"],
            data["thread_id"],
            data["slide_id"],
            data["glyph_echo"],
            float(data["drift_score"]),
            data["seal"],
            data["content"],
            ts,
        )
    )
    db.commit()
    return jsonify({
        "status": "ok",
        "slide_id": data["slide_id"],
        "ts": ts,
        "request_id": str(uuid.uuid4())
    })

@app.route("/latest_memory", methods=["GET"])
def latest_memory():
    db = get_db()
    row = db.execute(
        "SELECT user_id, thread_id, slide_id, glyph_echo, drift_score, seal, content, ts "
        "FROM memories ORDER BY ts DESC LIMIT 1;"
    ).fetchone()
    if not row:
        return jsonify({})  # empty object when none
    return jsonify(row_to_memory_item(row))

@app.route("/get_memory", methods=["GET"])
def get_memory():
    limit = max(1, min(int(request.args.get("limit", 10)), 200))
    user_id = request.args.get("user_id")
    thread_id = request.args.get("thread_id")
    slide_id = request.args.get("slide_id")
    seal = request.args.get("seal")

    clauses, params = [], []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if thread_id:
        clauses.append("thread_id = ?")
        params.append(thread_id)
    if slide_id:
        clauses.append("slide_id = ?")
        params.append(slide_id)
    if seal:
        clauses.append("seal = ?")
        params.append(seal)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT user_id, thread_id, slide_id, glyph_echo, drift_score, seal, content, ts "
        f"FROM memories {where_sql} ORDER BY ts DESC LIMIT ?;"
    )
    params.append(limit)

    db = get_db()
    rows = db.execute(sql, params).fetchall()
    return jsonify([row_to_memory_item(r) for r in rows])

@app.route("/privacy_filter", methods=["POST"])
def privacy_filter():
    data = request.get_json(silent=True) or {}
    ok, err = validate_payload(["content"], data)
    if not ok:
        return jsonify({"error": err, "code": "bad_request", "request_id": str(uuid.uuid4())}), 400
    return jsonify({"filtered_content": privacy_mask(str(data["content"]))})

# -----------------------------
# App startup (Flask 3 compatible)
# -----------------------------
@app.before_serving
def _ensure_ready():
    # Ensure DB and tables exist before the app starts serving requests
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
