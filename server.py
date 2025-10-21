# DavePMEi Memory API — Hybrid Neon(Postgres) + SQLite Fallback (lawful PMEi v1.1)
# Endpoints:
#   GET  /health
#   GET  /
#   POST /save_memory     (auth)
#   GET  /get_memory      (auth)
#
# Env:
#   MEMORY_API_KEY, ALLOWED_ORIGIN, DATABASE_URL, DB_PATH, MAX_CONTENT_CHARS
#   PG_MINCONN/PG_MAXCONN, DEBUG_BOOT
#   ENABLE_KEEPALIVE=true
#   KEEPALIVE_INTERVAL=60
#   SELF_HEALTH_URL=https://davepmei-ai.onrender.com/health

import os, time, threading, re, uuid, sqlite3, contextlib
from pathlib import Path
from collections import defaultdict
from flask import Flask, request, jsonify
from functools import wraps
import requests

app = Flask(__name__)

# ---------- Env ----------
MEMORY_API_KEY    = os.getenv("MEMORY_API_KEY", "").strip()
ALLOWED_ORIGIN    = os.getenv("ALLOWED_ORIGIN", "*").strip()
DATABASE_URL      = os.getenv("DATABASE_URL", "").strip()
CONFIG_DB_PATH    = os.getenv("DB_PATH", "/var/data/dave.sqlite3").strip()
MAX_CONTENT_CHARS = int(os.getenv("MAX_CONTENT_CHARS", "65536"))
PG_MINCONN        = int(os.getenv("PG_MINCONN", "1"))
PG_MAXCONN        = int(os.getenv("PG_MAXCONN", "5"))
DEBUG_BOOT        = os.getenv("DEBUG_BOOT", "0") == "1"

SAFE_SEALS = {"ok", "important", "critical", "lawful"}
GLYPH_MAX = 16
SLIDE_RE  = re.compile(r"^t-\d{3,6}$")
RATE_BUCKET = defaultdict(list)

# ---------- Helpers ----------
def clamp_drift(x) -> float:
    try: return max(0.0, min(float(x), 0.30))
    except Exception: return 0.10

def sanitize_glyph(g: str) -> str:
    return (g or "🪞").strip()[:GLYPH_MAX]

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

# ---------- DB Layer ----------
class DB:
    kind = "sqlite"
    placeholder = "?"

    @classmethod
    def try_postgres(cls):
        if not DATABASE_URL: return False
        if not DATABASE_URL.lower().startswith(("postgres://","postgresql://")):
            return False
        try:
            import psycopg2, psycopg2.pool, psycopg2.extras
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
                conn.commit()
            cls.kind = "postgres"
            cls.placeholder = "%s"
            print("[DB] Postgres connected")
            return True
        except Exception as e:
            print(f"[DB] Postgres failed: {e}")
            return False

    @classmethod
    @contextlib.contextmanager
    def get_pg_conn(cls):
        conn = cls.pool.getconn()
        try: yield conn
        finally: cls.pool.putconn(conn)

    @classmethod
    def init_sqlite(cls):
        cfg = Path(CONFIG_DB_PATH)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cls.sqlite = sqlite3.connect(str(cfg), check_same_thread=False)
        cls.sqlite.row_factory = sqlite3.Row
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
        cls.sqlite.commit()
        cls.kind = "sqlite"

    @classmethod
    def init(cls):
        if not cls.try_postgres():
            cls.init_sqlite()

    @classmethod
    def insert_memory(cls, rec):
        if cls.kind == "postgres":
            with cls.get_pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO memories(user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts)
                        VALUES({cls.placeholder},{cls.placeholder},{cls.placeholder},{cls.placeholder},{cls.placeholder},
                               {cls.placeholder},{cls.placeholder},{cls.placeholder},{cls.placeholder});
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
                        SELECT slide_id, ts FROM memories
                        WHERE user_id={cls.placeholder} AND thread_id={cls.placeholder}
                        ORDER BY ts DESC LIMIT 1;
                    """, (user_id, thread_id))
                    row = cur.fetchone()
            return dict(row) if row else None
        else:
            row = cls.sqlite.execute("""
                SELECT slide_id, ts FROM memories
                WHERE user_id=? AND thread_id=? ORDER BY ts DESC LIMIT 1
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
                        FROM memories {where_sql}
                        ORDER BY ts DESC LIMIT {cls.placeholder};
                    """, params + [limit])
                    rows = cur.fetchall()
            return [dict(r) for r in rows]
        else:
            rows = cls.sqlite.execute(f"""
                SELECT user_id,thread_id,slide_id,glyph_echo,drift_score,seal,role,content,ts
                FROM memories {where_sql} ORDER BY ts DESC LIMIT ?
            """, params + [limit]).fetchall()
            return [dict(r) for r in rows]

DB.init()

# ---------- Keepalive ----------
def _keepalive():
    url = os.getenv("SELF_HEALTH_URL")
    interval = int(os.getenv("KEEPALIVE_INTERVAL", "60"))
    if not url:
        print("[KEEPALIVE] Disabled (no SELF_HEALTH_URL)")
        return
    print(f"[KEEPALIVE] Active: triple ping to {url} every {interval}s")
    while True:
        for i in range(3):
            try:
                requests.get(url, timeout=10)
                print(f"[KEEPALIVE] Ping {i+1}/3 -> 200 @ {int(time.time())}")
            except Exception as e:
                print(f"[KEEPALIVE] Error {i+1}/3: {e}")
            time.sleep(2)
        time.sleep(interval)

if os.getenv("ENABLE_KEEPALIVE", "true").lower() in ("1","true","yes","on"):
    threading.Thread(target=_keepalive, daemon=True).start()

# ---------- Auth ----------
def _auth_ok():
    if not MEMORY_API_KEY: return False
    h = request.headers
    k1, k2 = h.get("X-API-Key",""), h.get("X-API-KEY","")
    auth = h.get("Authorization","")
    bearer = auth.split(" ",1)[1].strip() if auth.lower().startswith("bearer ") else ""
    return any(x == MEMORY_API_KEY for x in (k1,k2,bearer))

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
@app.after_request
def add_headers(resp):
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGIN == "*" or origin in [o.strip() for o in ALLOWED_ORIGIN.split(",")]:
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Vary"] = "Origin"
    return resp

# ---------- Routes ----------
@app.route("/")
def root():
    return jsonify({"ok": True, "service": "DavePMEi Memory API", "storage": DB.kind})

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "storage": DB.kind})

@app.route("/save_memory", methods=["POST", "OPTIONS"])
@require_key
def save_memory():
    if request.method == "OPTIONS": return ("", 204)
    if not rate_limit_ok(f"save:{request.remote_addr}", max_per_min=120):
        return jsonify({"ok": False, "error": "Rate limit"}), 429

    d = request.get_json(silent=True) or {}
    user_id = str(d.get("user_id", "")).strip()
    thread_id = (str(d.get("thread_id", "general")).strip() or "general")[:64]
    content = str(d.get("content", "")).strip()
    if not user_id or not content:
        return jsonify({"ok": False, "error": "Missing user_id or content"}), 400

    drift = clamp_drift(d.get("drift_score", 0.10))
    glyph = sanitize_glyph(d.get("glyph_echo", "🪞"))
    seal = sanitize_seal(d.get("seal", "lawful"))
    role = (str(d.get("role", "assistant")).strip() or "assistant")[:32]
    slide_id = str(d.get("slide_id", "")).strip()
    if not slide_id or not SLIDE_RE.match(slide_id):
        last = DB.select_latest_in_thread(user_id, thread_id)
        if last and SLIDE_RE.match(last.get("slide_id", "")):
            n = int(last["slide_id"].split("-")[1])
            slide_id = f"t-{n+1:03d}"
        else:
            slide_id = "t-001"

    rec = dict(user_id=user_id, thread_id=thread_id, slide_id=slide_id,
               glyph_echo=glyph, drift_score=drift, seal=seal,
               role=role, content=content, ts=int(time.time()))
    out_id = DB.insert_memory(rec)
    return jsonify({"ok": True, "status": "ok", "slide_id": out_id, "ts": rec["ts"]}), 201

@app.route("/get_memory", methods=["GET", "OPTIONS"])
@require_key
def get_memory():
    if request.method == "OPTIONS": return ("", 204)
    if not rate_limit_ok(f"get:{request.remote_addr}", max_per_min=240):
        return jsonify({"ok": False, "error": "Rate limit"}), 429

    args = request.args
    filters = {k: args.get(k) for k in ("user_id","thread_id","slide_id","seal","role")}
    limit = max(1, min(int(args.get("limit", "50")), 200))
    before_ts = int(args.get("before_ts")) if args.get("before_ts") else None
    items = DB.select_memories(filters, limit, before_ts)
    next_cursor = items[-1]["ts"] if items else None
    return jsonify({"ok": True, "count": len(items), "next_before_ts": next_cursor, "items": items}), 200

# ---------- Local run ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
