# DavePMEi Memory API — Hybrid Neon(Postgres) + SQLite Fallback
# - POST /save_memory   (auth required)
# - GET  /get_memory    (auth required)
# - GET  /health, /     (no auth)
#
# Behavior:
#   If DATABASE_URL starts with postgres:// or postgresql:// -> use Neon (psycopg2, pooled)
#   else -> SQLite at DB_PATH (default /var/data/dave.sqlite3, fallback to /tmp/dave.sqlite3)
#
# CORS: ALLOWED_ORIGIN="https://your-vercel.app,https://your-runner.com" (or "*")
# Auth: MEMORY_API_KEY via X-API-Key / X-API-KEY / Authorization: Bearer

from flask import Flask, request, jsonify
from functools import wraps
from pathlib import Path
import os, time, uuid, sqlite3, contextlib

app = Flask(__name__)

# ----------------------- Env -----------------------
MEMORY_API_KEY      = os.environ.get("MEMORY_API_KEY", "").strip()
ALLOWED_ORIGIN      = os.environ.get("ALLOWED_ORIGIN", "*").strip()   # CSV or "*"
DATABASE_URL        = os.environ.get("DATABASE_URL", "").strip()      # Neon URL
CONFIG_DB_PATH      = os.environ.get("DB_PATH", "/var/data/dave.sqlite3").strip()
MAX_CONTENT_CHARS   = int(os.environ.get("MAX_CONTENT_CHARS", "65536"))
PG_MINCONN          = int(os.environ.get("PG_MINCONN", "1"))
PG_MAXCONN          = int(os.environ.get("PG_MAXCONN", "5"))

# -------------------- DB Abstraction ----------------
class DB:
    kind = "sqlite"      # "postgres" or "sqlite"
    placeholder = "?"    # "%s" for pg, "?" for sqlite

    # --- Postgres (Neon) ---
    @classmethod
    def try_postgres(cls):
        if not (DATABASE_URL.lower().startswith("postgres://") or DATABASE_URL.lower().startswith("postgresql://")):
            return False
        try:
            import psycopg2
            import psycopg2.pool
            import psycopg2.extras
        except Exception:
            return False
        try:
            cls.pg_extras = psycopg2.extras
            cls.pool = psycopg2.pool.SimpleConnectionPool(
                PG_MINCONN, PG_MAXCONN, dsn=DATABASE_URL, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5
            )
            # smoke test + init
            with cls.get_pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS memories(
                          id BIGSERIAL PRIMARY KEY,
                          user_id TEXT NOT NULL,
                          thread_id TEXT NOT NULL,
                          slide_id TEXT NOT NULL,
                          glyph_echo TEXT NOT NULL,
                          drift_score DOUBLE PRECISION NOT NULL,
                          seal TEXT NOT NULL,
                          role TEXT NOT NULL,
                          content TEXT NOT NULL,
                          ts BIGINT NOT NULL
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts     ON memories(ts DESC);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_user   ON memories(user_id);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_thread ON memories(thread_id);")
                conn.commit()
            cls.kind = "postgres"
            cls.placeholder = "%s"
            return True
        except Exception:
            # If pg init fails for any reason, fall back to sqlite
            return False

    @classmethod
    @contextlib.contextmanager
    def get_pg_conn(cls):
        conn = cls.pool.getconn()
        try:
            yield conn
        finally:
            cls.pool.putconn(conn)

    # --- SQLite ---
    @staticmethod
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

    @classmethod
    def init_sqlite(cls):
        # prefer configured path, else /tmp
        cfg = Path(CONFIG_DB_PATH)
        if not cls._dir_writable(cfg.parent):
            cfg = Path("/tmp/dave.sqlite3")
            cfg.parent.mkdir(parents=True, exist_ok=True)
        cls.sqlite_path = str(cfg)
        cls.sqlite = sqlite3.connect(cls.sqlite_path, check_same_thread=False)
        cls.sqlite.row_factory = sqlite3.Row
        cls.sqlite.execute("PRAGMA journal_mode=WAL;")
        cls.sqlite.execute("PRAGMA busy_timeout=5000;")
        cls.sqlite.execute("""
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
        cls.sqlite.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts     ON memories(ts DESC);")
        cls.sqlite.execute("CREATE INDEX IF NOT EXISTS idx_mem_user   ON memories(user_id);")
        cls.sqlite.execute("CREATE INDEX IF NOT EXISTS idx_mem_thread ON memories(thread_id);")
        cls.sqlite.commit()
        cls.kind = "sqlite"
        cls.placeholder = "?"

    # --- Public helpers ---
    @classmethod
    def init(cls):
        if cls.try_postgres():
            return
        cls.init_sqlite()

    @classmethod
    def insert_memory(cls, rec):
        if cls.kind == "postgres":
            with cls.get_pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO memories(user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts)
                        VALUES({cls.placeholder},{cls.placeholder},{cls.placeholder},{cls.placeholder},{cls.placeholder},
                               {cls.placeholder},{cls.placeholder},{cls.placeholder},{cls.placeholder})
                        RETURNING slide_id;
                    """, (rec["user_id"], rec["thread_id"], rec["slide_id"], rec["glyph_echo"], rec["drift_score"],
                          rec["seal"], rec["role"], rec["content"], rec["ts"]))
                conn.commit()
            return rec["slide_id"]
        else:
            cls.sqlite.execute("""
                INSERT INTO memories(user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (rec["user_id"], rec["thread_id"], rec["slide_id"], rec["glyph_echo"], rec["drift_score"],
                  rec["seal"], rec["role"], rec["content"], rec["ts"]))
            cls.sqlite.commit()
            return rec["slide_id"]

    @classmethod
    def select_memories(cls, filters, limit:int):
        clauses, params = [], []
        for key in ("user_id","thread_id","slide_id","seal","role"):
            val = filters.get(key)
            if val:
                clauses.append(f"{key} = {cls.placeholder}")
                params.append(val)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if cls.kind == "postgres":
            with cls.get_pg_conn() as conn:
                with conn.cursor(cursor_factory=cls.pg_extras.RealDictCursor) as cur:
                    cur.execute(f"""
                        SELECT user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts
                        FROM memories
                        {where_sql}
                        ORDER BY ts DESC
                        LIMIT {cls.placeholder};
                    """, params + [limit])
                    rows = cur.fetchall()
            return [dict(r) for r in rows]
        else:
            rows = cls.sqlite.execute(f"""
                SELECT user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts
                FROM memories
                {where_sql}
                ORDER BY ts DESC
                LIMIT ?
            """, params + [limit]).fetchall()
            return [dict(r) for r in rows]

DB.init()

# -------------------- Errors -> JSON ----------------
@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    return jsonify({"ok": False, "error": str(e), "code": code}), code

# -------------------- Auth -------------------------
def _auth_ok():
    if not MEMORY_API_KEY:
        return False
    h = request.headers
    k1 = h.get("X-API-Key", "")
    k2 = h.get("X-API-KEY", "")
    auth = h.get("Authorization", "")
    bearer = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") and " " in auth else ""
    return any(x == MEMORY_API_KEY for x in (k1, k2, bearer))

def require_key(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if request.method == "OPTIONS" or request.path in ("/", "/health"):
            return fn(*args, **kwargs)
        if not _auth_ok():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped

# -------------------- CORS -------------------------
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
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
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

# -------------------- Routes -----------------------
@app.route("/")
def root():
    info = {
        "ok": True,
        "service": "DavePMEi Memory API (hybrid)",
        "storage": DB.kind,
        "endpoints": ["/health","/save_memory","/get_memory"]
    }
    if DB.kind == "sqlite":
        info["sqlite_path"] = getattr(DB, "sqlite_path", "?")
    return jsonify(info)

@app.route("/health")
def health():
    detail = {"ok": True, "ts": int(time.time()), "storage": DB.kind}
    if DB.kind == "sqlite":
        detail["sqlite_path"] = getattr(DB, "sqlite_path", "?")
    return jsonify(detail)

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

    rec = {
        "user_id":   user_id,
        "thread_id": (str(data.get("thread_id", "general")).strip() or "general")[:64],
        "slide_id":  str(data.get("slide_id", str(uuid.uuid4()))).strip(),
        "glyph_echo":(str(data.get("glyph_echo", "🪞")).strip() or "🪞")[:16],
        "drift_score": float(data.get("drift_score", 0.05)),
        "seal":      (str(data.get("seal", "lawful")).strip() or "lawful")[:32],
        "role":      role,
        "content":   content,
        "ts":        int(time.time())
    }
    slide_id = DB.insert_memory(rec)
    return jsonify({"ok": True, "status": "ok", "slide_id": slide_id, "ts": rec["ts"]}), 201

@app.route("/get_memory", methods=["GET","OPTIONS"])
@require_key
def get_memory():
    if request.method == "OPTIONS":
        return ("", 204)

    args = request.args
    filters = {
        "user_id":   args.get("user_id"),
        "thread_id": args.get("thread_id"),
        "slide_id":  args.get("slide_id"),
        "seal":      args.get("seal"),
        "role":      args.get("role"),
    }
    try:
        limit = max(1, min(int(args.get("limit","50")), 200))
    except ValueError:
        return jsonify({"ok": False, "error": "limit must be an integer"}), 400

    items = DB.select_memories(filters, limit)
    return jsonify({"ok": True, "items": items, "count": len(items)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
