# DavePMEi Memory API — Hybrid Neon(Postgres) + SQLite Fallback
# Endpoints
#   GET  /health
#   GET  /
#   POST /save_memory     (auth)
#   GET  /get_memory      (auth)  -> supports limit & before_ts cursor
#
# Env
#   MEMORY_API_KEY   : required for auth
#   ALLOWED_ORIGIN   : CSV of origins or "*"
#   DATABASE_URL     : postgresql://... (Neon)  -> else SQLite fallback
#   DB_PATH          : sqlite file (default /var/data/dave.sqlite3)
#   MAX_CONTENT_CHARS: default 65536
#   PG_MINCONN/PG_MAXCONN: pool sizing
#   DEBUG_BOOT       : "1" to log boot decisions (no secrets)
#
# PMEi Guards
#   - drift_score is clamped to <= 0.30
#   - seal must be one of {ok, important, critical, lawful}
#   - glyph_echo trimmed to <= 16 chars
#   - auto slide_id t-001, t-002... per (user_id, thread_id) if not provided

from flask import Flask, request, jsonify
from functools import wraps
from pathlib import Path
from collections import defaultdict
import os, time, uuid, sqlite3, contextlib, re

app = Flask(__name__)

# ----------------------- Env -----------------------
MEMORY_API_KEY      = os.environ.get("MEMORY_API_KEY", "").strip()
ALLOWED_ORIGIN      = os.environ.get("ALLOWED_ORIGIN", "*").strip()
DATABASE_URL        = os.environ.get("DATABASE_URL", "").strip()
CONFIG_DB_PATH      = os.environ.get("DB_PATH", "/var/data/dave.sqlite3").strip()
MAX_CONTENT_CHARS   = int(os.environ.get("MAX_CONTENT_CHARS", "65536"))
PG_MINCONN          = int(os.environ.get("PG_MINCONN", "1"))
PG_MAXCONN          = int(os.environ.get("PG_MAXCONN", "5"))
DEBUG_BOOT          = os.environ.get("DEBUG_BOOT", "0") == "1"

# -------------------- PMEi constraints -------------
SAFE_SEALS = {"ok", "important", "critical", "lawful"}
GLYPH_MAX = 16
SLIDE_RE  = re.compile(r"^t-\d{3,6}$")  # t-001..t-999999
RATE_BUCKET = defaultdict(list)          # naive per-minute limiter

def clamp_drift(x) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.10
    return max(0.0, min(v, 0.30))

def sanitize_glyph(g: str) -> str:
    g = (g or "🪞").strip()
    return g[:GLYPH_MAX]

def sanitize_seal(s: str) -> str:
    s = (s or "lawful").strip().lower()
    return s if s in SAFE_SEALS else "lawful"

def rate_limit_ok(key: str, max_per_min=120):
    now = time.time()
    bucket = RATE_BUCKET[key]
    while bucket and now - bucket[0] > 60:
        bucket.pop(0)
    if len(bucket) >= max_per_min:
        return False
    bucket.append(now)
    return True

# -------------------- DB Abstraction ----------------
class DB:
    kind = "sqlite"      # "postgres" or "sqlite"
    placeholder = "?"    # "%s" for pg, "?" for sqlite

    @classmethod
    def try_postgres(cls):
        # redact helper for safe logs
        def redact(url: str) -> str:
            try:
                pre, rest = url.split("://", 1)
                if "@" in rest and ":" in rest.split("@", 1)[0]:
                    user, after_user = rest.split("@", 1)[0], rest.split("@", 1)[1]
                    user_name = user.split(":")[0]
                    return f"{pre}://{user_name}:***@{after_user}"
            except Exception:
                pass
            return url

        if not DATABASE_URL:
            if DEBUG_BOOT: print("[DB] DATABASE_URL is empty -> using SQLite")
            return False
        if not (DATABASE_URL.lower().startswith("postgres://")
                or DATABASE_URL.lower().startswith("postgresql://")):
            if DEBUG_BOOT: print("[DB] DATABASE_URL present but not postgres:// -> using SQLite")
            return False

        try:
            import psycopg2
            import psycopg2.pool
            import psycopg2.extras
        except Exception as e:
            if DEBUG_BOOT: print(f"[DB] psycopg2 import failed -> {e!r} ; falling back to SQLite")
            return False

        try:
            if DEBUG_BOOT: print(f"[DB] Attempting Postgres pool connect -> {redact(DATABASE_URL)}")
            cls.pg_extras = psycopg2.extras
            cls.pool = psycopg2.pool.SimpleConnectionPool(
                PG_MINCONN, PG_MAXCONN, dsn=DATABASE_URL,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5
            )
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
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts       ON memories(ts DESC);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_user     ON memories(user_id);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_thread   ON memories(thread_id);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_user_thr ON memories(user_id, thread_id, ts DESC);")
                conn.commit()
            cls.kind = "postgres"
            cls.placeholder = "%s"
            if DEBUG_BOOT: print("[DB] Postgres connection OK; storage=postgres")
            return True
        except Exception as e:
            if DEBUG_BOOT: print(f"[DB] Postgres connect/init failed -> {type(e).__name__}: {e} ; falling back to SQLite")
            return False

    @classmethod
    @contextlib.contextmanager
    def get_pg_conn(cls):
        conn = cls.pool.getconn()
        try:
            yield conn
        finally:
            cls.pool.putconn(conn)

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
        cls.sqlite.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts       ON memories(ts DESC);")
        cls.sqlite.execute("CREATE INDEX IF NOT EXISTS idx_mem_user     ON memories(user_id);")
        cls.sqlite.execute("CREATE INDEX IF NOT EXISTS idx_mem_thread   ON memories(thread_id);")
        cls.sqlite.execute("CREATE INDEX IF NOT EXISTS idx_mem_user_thr ON memories(user_id, thread_id, ts DESC);")
        cls.sqlite.commit()
        cls.kind = "sqlite"
        cls.placeholder = "?"

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
    def select_latest_in_thread(cls, user_id: str, thread_id: str):
        if cls.kind == "postgres":
            with cls.get_pg_conn() as conn:
                with conn.cursor(cursor_factory=cls.pg_extras.RealDictCursor) as cur:
                    cur.execute(f"""
                        SELECT slide_id, ts
                        FROM memories
                        WHERE user_id={cls.placeholder} AND thread_id={cls.placeholder}
                        ORDER BY ts DESC
                        LIMIT 1;
                    """, (user_id, thread_id))
                    row = cur.fetchone()
            return dict(row) if row else None
        else:
            row = cls.sqlite.execute("""
                SELECT slide_id, ts
                FROM memories
                WHERE user_id=? AND thread_id=?
                ORDER BY ts DESC
                LIMIT 1
            """, (user_id, thread_id)).fetchone()
            return dict(row) if row else None

    @classmethod
    def select_memories(cls, filters, limit:int, before_ts:int|None=None):
        clauses, params = [], []
        for key in ("user_id","thread_id","slide_id","seal","role"):
            val = filters.get(key)
            if val:
                clauses.append(f"{key} = {cls.placeholder}")
                params.append(val)
        if before_ts:
            clauses.append(f"ts < {cls.placeholder}")
            params.append(before_ts)
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

# -------------------- CORS + Security --------------
def _origin_allowed(origin: str) -> bool:
    if not origin:
        return False
    if ALLOWED_ORIGIN == "*":
        return True
    allowed = [o.strip() for o in ALLOWED_ORIGIN.split(",") if o.strip()]
    return origin in allowed

@app.after_request
def add_headers(resp):
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGIN == "*" or _origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, X-API-KEY, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    if request.path == "/get_memory":
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp

@app.route("/options", methods=["OPTIONS"])
def options_any():
    return ("", 204)

# -------------------- Helpers ----------------------
def next_slide_id_for(user_id: str, thread_id: str) -> str:
    last = DB.select_latest_in_thread(user_id, thread_id)
    if last and SLIDE_RE.match(last.get("slide_id", "")):
        n = int(last["slide_id"].split("-")[1])
        return f"t-{n+1:03d}"
    return "t-001"

def client_ip():
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "ip"
    )

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
    if DEBUG_BOOT:
        detail["has_db_url"] = bool(DATABASE_URL)
    return jsonify(detail)

@app.route("/save_memory", methods=["POST","OPTIONS"])
@require_key
def save_memory():
    if request.method == "OPTIONS":
        return ("", 204)

    if not rate_limit_ok(f"save:{client_ip()}", max_per_min=120):
        return jsonify({"ok": False, "error": "Rate limit"}), 429

    data = request.get_json(silent=True) or {}
    user_id   = str(data.get("user_id", "")).strip()
    thread_id = (str(data.get("thread_id", "general")).strip() or "general")[:64]
    content   = str(data.get("content", "")).strip()
    role      = (str(data.get("role", "assistant")).strip() or "assistant")[:32]

    if not user_id or not content:
        return jsonify({"ok": False, "error": "Missing user_id or content"}), 400
    if len(content) > MAX_CONTENT_CHARS:
        return jsonify({"ok": False, "error": f"content too large (> {MAX_CONTENT_CHARS} chars)"}), 413

    drift_score = clamp_drift(data.get("drift_score", 0.10))
    glyph_echo  = sanitize_glyph(data.get("glyph_echo", "🪞"))
    seal        = sanitize_seal(data.get("seal", "lawful"))

    slide_id = str(data.get("slide_id", "")).strip()
    if not slide_id or not SLIDE_RE.match(slide_id):
        slide_id = next_slide_id_for(user_id, thread_id)

    rec = {
        "user_id":   user_id,
        "thread_id": thread_id,
        "slide_id":  slide_id,
        "glyph_echo":glyph_echo,
        "drift_score": drift_score,
        "seal":      seal,
        "role":      role,
        "content":   content,
        "ts":        int(time.time())
    }
    out_id = DB.insert_memory(rec)
    return jsonify({"ok": True, "status": "ok", "slide_id": out_id, "ts": rec["ts"]}), 201

@app.route("/get_memory", methods=["GET","OPTIONS"])
@require_key
def get_memory():
    if request.method == "OPTIONS":
        return ("", 204)

    if not rate_limit_ok(f"get:{client_ip()}", max_per_min=240):
        return jsonify({"ok": False, "error": "Rate limit"}), 429

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

    before_ts = None
    if args.get("before_ts"):
        try:
            before_ts = int(args.get("before_ts"))
        except ValueError:
            return jsonify({"ok": False, "error": "before_ts must be an integer epoch seconds"}), 400

    items = DB.select_memories(filters, limit, before_ts=before_ts)
    next_cursor = items[-1]["ts"] if items else None
    return jsonify({"ok": True, "items": items, "count": len(items), "next_before_ts": next_cursor}), 200

# -------------------- Run --------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
