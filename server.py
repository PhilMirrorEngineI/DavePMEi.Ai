# server.py — PMEi Memory API (Dave)
# Minimal, strict, privacy-first memory shard service with:
# - SQLite persistence (Render Disk-friendly)
# - Public /health, /healthz, /openapi.json
# - Auth via X-API-KEY on all other routes

import os
import re
import json
import time
import uuid
import sqlite3
import hashlib
import base64
from flask import Flask, request, jsonify, g, Response

# -----------------------------
# Config
# -----------------------------
MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "").strip()

DEFAULT_DB_PATH = "/data/dave.sqlite3" if os.path.isdir("/data") else (
    "/var/data/dave.sqlite3" if os.path.isdir("/var/data") else "./dave.sqlite3"
)
SQLITE_PATH = os.environ.get("SQLITE_PATH", DEFAULT_DB_PATH)
OPENAPI_PATH = os.environ.get("OPENAPI_PATH", "./openapi.json")

app = Flask(__name__)

_db_dir = os.path.dirname(os.path.abspath(SQLITE_PATH))
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

# -----------------------------
# DB helpers
# -----------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db

def init_db() -> None:
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            thread_id   TEXT NOT NULL,
            slide_id    TEXT NOT NULL,
            glyph_echo  TEXT NOT NULL,
            drift_score REAL NOT NULL,
            seal        TEXT NOT NULL,
            content     TEXT NOT NULL,
            ts          INTEGER NOT NULL
        );
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_hash TEXT UNIQUE NOT NULL,
            reflection_id TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_ts INTEGER NOT NULL
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
    if request.path in ("/health", "/healthz", "/openapi.json"):
        return
    supplied = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if not MEMORY_API_KEY or supplied != MEMORY_API_KEY:
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

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

def privacy_mask(text: str) -> str:
    text = EMAIL_RE.sub("[redacted-email]", text)
    text = PHONE_RE.sub("[redacted-phone]", text)
    return text

def generate_reflection_id(email: str) -> str:
    if not email:
        raise ValueError("Email required")
    hashed_bytes = hashlib.sha256(email.lower().encode("utf-8")).digest()
    reflection_id = base64.urlsafe_b64encode(hashed_bytes).decode("utf-8").rstrip("=")
    return f"GLYPH-{reflection_id[:16]}"

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

@app.route("/register", methods=["POST"])
def register_user():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Missing email or password"}), 400

    reflection_id = generate_reflection_id(email)
    email_hash = hashlib.sha256(email.lower().encode()).hexdigest()
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    ts = int(time.time())

    db = get_db()
    db.execute(
        """
        INSERT OR REPLACE INTO users (email_hash, reflection_id, password_hash, created_ts)
        VALUES (?, ?, ?, ?);
        """,
        (email_hash, reflection_id, password_hash, ts)
    )
    db.commit()

    return jsonify({
        "status": "registered",
        "reflection_id": reflection_id
    })

@app.route("/save_memory", methods=["POST"])
def save_memory():
    data = request.get_json(silent=True) or {}

    if "email" in data and "user_id" not in data:
        data["user_id"] = generate_reflection_id(data["email"])

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
        """
        SELECT user_id, thread_id, slide_id, glyph_echo, drift_score, seal, content, ts
        FROM memories
        ORDER BY ts DESC
        LIMIT 1;
        """
    ).fetchone()
    if not row:
        return jsonify({})
    return jsonify(row_to_memory_item(row))

@app.route("/get_memory", methods=["GET"])
def get_memory():
    try:
        limit = int(request.args.get("limit", 10))
    except ValueError:
        limit = 10
    limit = max(1, min(limit, 200))

    user_id   = request.args.get("user_id")
    thread_id = request.args.get("thread_id")
    slide_id  = request.args.get("slide_id")
    seal      = request.args.get("seal")

    clauses, params = [], []
    if user_id:
        clauses.append("user_id = ?");   params.append(user_id)
    if thread_id:
        clauses.append("thread_id = ?"); params.append(thread_id)
    if slide_id:
        clauses.append("slide_id = ?");  params.append(slide_id)
    if seal:
        clauses.append("seal = ?");      params.append(seal)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT user_id, thread_id, slide_id, glyph_echo, drift_score, seal, content, ts "
                f"FROM memories {where_sql} ORDER BY ts DESC LIMIT ?;"
    )
    params.append(limit)

    db = get_db()
    rows = db.execute(sql, params).fetchall()
    items = [row_to_memory_item(r) for r in rows]
    return jsonify(items)
# -----------------------------
# App startup
# -----------------------------
with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
