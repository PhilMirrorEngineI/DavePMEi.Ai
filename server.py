# Flask JSON + CORS + Auth Handshake (render-safe + sqlite fallback)
from flask import Flask, request, jsonify
from functools import wraps
from pathlib import Path
import sqlite3, os, time, uuid, stat

app = Flask(__name__)

MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "").strip()
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*").strip()  # CSV or "*"
CONFIG_DB_PATH = os.environ.get("DB_PATH", "/var/data/dave.sqlite3").strip()
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "65536"))  # 64KB

# ---------- storage path selection ----------
def _dir_writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        testf = p / ".writetest"
        with testf.open("w") as f:
            f.write("ok")
        testf.unlink()
        return True
    except Exception:
        return False

def _pick_db_path():
    # prefer configured path; fall back to /tmp on permission error
    cfg = Path(CONFIG_DB_PATH)
    if _dir_writable(cfg.parent):
        return str(cfg)
    tmp = Path("/tmp/dave.sqlite3")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    return str(tmp)

DB_PATH = _pick_db_path()

# ---------- db ----------
def _db():
    if not hasattr(app, "_db"):
        app._db = sqlite3.connect(DB_PATH, check_same_thread=False)
        app._db.row_factory = sqlite3.Row
        # sane defaults for concurrency
        app._db.execute("PRAGMA journal_mode=WAL;")
        app._db.execute("PRAGMA busy_timeout=5000;")
        app._db.execute("""
            CREATE TABLE IF NOT EXISTS memories(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL,
              thread_id TEXT NOT NULL,
              slide_id TEXT NOT NULL,
              glyph_echo TEXT NOT NULL,
              drift_score REAL NOT NULL,
              seal TEXT NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              ts INTEGER NOT NULL
            );
        """)
        app._db.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts     ON memories(ts);")
        app._db.execute("CREATE INDEX IF NOT EXISTS idx_mem_user   ON memories(user_id);")
        app._db.execute("CREATE INDEX IF NOT EXISTS idx_mem_thread ON memories(thread_id);")
        app._db.commit()
    return app._db

# ---------- errors ----------
@app.errorhandler(Exception)
def handle_error(e):
    code = getattr(e, "code", 500)
    return jsonify({"ok": False, "error": str(e), "code": code}), code

# ---------- auth ----------
def _auth_ok():
    if not MEMORY_API_KEY:
        return False
    headers = request.headers
    k1 = headers.get("X-API-Key", "")
    k2 = headers.get("X-API-KEY", "")
    auth = headers.get("Authorization", "")
    bear = ""
    if auth.lower().startswith("bearer "):
        bear = auth.split(" ", 1)[1].strip()
    return any(k == MEMORY_API_KEY for k in (k1, k2, bear))

def require_key(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if request.method == "OPTIONS" or request.path in ("/", "/health"):
            return fn(*args, **kwargs)
        if not _auth_ok():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped

# ---------- CORS ----------
def _origin_allowed(origin: str) -> bool:
    if not origin:
        return False
    if ALLOWED_ORIGIN == "*":
        return True
    allowed = [o.strip() for o in ALLOWED_ORIGIN.split(",") if o.strip()]
    return origin in allowed

@app.after_request
def add_cors(resp):
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGIN == "*" or _origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, X-API-KEY, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    if request.path == "/get_memory":
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/options", methods=["OPTIONS"])
def options_any():
    return ("", 204)

# ---------- routes ----------
@app.route("/")
def root():
    return jsonify({
        "ok": True,
        "service": "DavePMEi Memory API (sqlite)",
        "db_path": DB_PATH,
        "endpoints": ["/health","/save_memory","/get_memory"]
    })

@app.route("/health")
def health():
    storage = "sqlite:/tmp" if DB_PATH.startswith("/tmp/") else "sqlite:custom"
    return jsonify({"ok": True, "ts": int(time.time()), "storage": storage, "db_path": DB_PATH})

@app.route("/save_memory", methods=["POST","OPTIONS"])
@require_key
def save_memory():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    user_id = str(data.get("user_id", "")).strip()
    content = str(data.get("content", "")).strip()
    role     = (str(data.get("role", "assistant")).strip() or "assistant")[:32]

    if not user_id or not content:
        return jsonify({"ok": False, "error": "Missing user_id or content"}), 400
    if len(content) > MAX_CONTENT_CHARS:
        return jsonify({"ok": False, "error": f"content too large (> {MAX_CONTENT_CHARS} chars)"}), 413

    thread_id   = (str(data.get("thread_id", "general")).strip() or "general")[:64]
    slide_id    = str(data.get("slide_id", str(uuid.uuid4()))).strip()
    glyph_echo  = (str(data.get("glyph_echo", "🪞")).strip() or "🪞")[:16]
    drift_score = float(data.get("drift_score", 0.05))
    seal        = (str(data.get("seal", "lawful")).strip() or "lawful")[:32]

    ts = int(time.time())
    db = _db()
    db.execute(
        """INSERT INTO memories(user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts)
    )
    db.commit()
    return jsonify({"ok": True, "status": "ok", "slide_id": slide_id, "ts": ts}), 201

@app.route("/get_memory", methods=["GET","OPTIONS"])
@require_key
def get_memory():
    if request.method == "OPTIONS":
        return ("", 204)

    user_id   = request.args.get("user_id")
    thread_id = request.args.get("thread_id")
    slide_id  = request.args.get("slide_id")
    seal      = request.args.get("seal")
    role      = request.args.get("role")
    try:
        limit = max(1, min(int(request.args.get("limit","10")), 200))
    except ValueError:
        return jsonify({"ok": False, "error": "limit must be an integer"}), 400

    clauses, params = [], []
    if user_id:   clauses.append("user_id = ?");   params.append(user_id)
    if thread_id: clauses.append("thread_id = ?"); params.append(thread_id)
    if slide_id:  clauses.append("slide_id = ?");  params.append(slide_id)
    if seal:      clauses.append("seal = ?");      params.append(seal)
    if role:      clauses.append("role = ?");      params.append(role)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = _db().execute(
        f"""SELECT user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts
            FROM memories {where_sql}
            ORDER BY ts DESC LIMIT ?""",
        params + [limit]
    ).fetchall()
    items = [dict(r) for r in rows]
    return jsonify({"ok": True, "items": items, "count": len(items)}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
