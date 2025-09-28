# PMEi Memory API (Dave)

Secure, lawful memory shards for **PhilMirrorEnginei.ai**.

- **Primary Base URL:** `https://www.philmirrorenginei.ai`
- **Apex:** `https://philmirrorenginei.ai` (redirects to primary)
- **OpenAPI:** `/openapi.json`
- **Version:** `1.0.3`

---

## Authentication

All protected endpoints require the API key header:

~~~
X-API-KEY: <YOUR_API_KEY>
~~~

> No auth required for `/health`, `/healthz`, `/openapi.json`.

---

## Endpoints

### Health
- `GET /health` – service health (public)
- `GET /healthz` – liveness (alias for `/health`)
- `GET /openapi.json` – OpenAPI schema (public)

### Memory
- `POST /save_memory` – save a memory shard (**auth**)
- `GET /latest_memory` – latest shard or `{}` (**auth**)
- `GET /get_memory?limit=10[&user_id=...&thread_id=...&slide_id=...&seal=...]` – list shards, newest first (**auth**)

### Privacy
- `POST /privacy_filter` – minimal PII masking (**auth**)

---

## Schemas

### SaveMemoryRequest (JSON)
~~~json
{
  "user_id": "phil",
  "thread_id": "smoke",
  "slide_id": "t-001",
  "glyph_echo": "ping",
  "drift_score": 0,
  "seal": "ok",
  "content": "smoke test"
}
~~~

### SaveMemoryResponse (JSON)
~~~json
{
  "status": "ok",
  "slide_id": "t-001",
  "ts": 1759075229,
  "request_id": "abc123def456"
}
~~~

### MemoryItem (JSON)
~~~json
{
  "user_id": "phil",
  "thread_id": "smoke",
  "slide_id": "t-001",
  "glyph_echo": "ping",
  "drift_score": 0,
  "seal": "ok",
  "content": "smoke test",
  "ts": 1759075229,
  "request_id": "abc123def456"
}
~~~

---

## Quick cURL

> Tip: set env vars to keep commands short.

~~~bash
BASE=https://www.philmirrorenginei.ai
KEY=<YOUR_API_KEY>
~~~

### 1) Health (no auth)
~~~bash
curl -s $BASE/health
~~~

### 2) Save a memory
~~~bash
curl -s -X POST $BASE/save_memory \
  -H 'Content-Type: application/json' \
  -H "X-API-KEY: $KEY" \
  -d '{
        "user_id":"phil",
        "thread_id":"smoke",
        "slide_id":"t-001",
        "glyph_echo":"ping",
        "drift_score":0,
        "seal":"ok",
        "content":"first memory shard via curl"
      }'
~~~

### 3) Fetch latest
~~~bash
curl -s -H "X-API-KEY: $KEY" $BASE/latest_memory
~~~

### 4) List memories (newest first)
~~~bash
curl -s -H "X-API-KEY: $KEY" "$BASE/get_memory?limit=5"
~~~

### 5) Privacy filter
~~~bash
curl -s -X POST $BASE/privacy_filter \
  -H 'Content-Type: application/json' \
  -H "X-API-KEY: $KEY" \
  -d '{ "content": "email me at phil@example.com or +44 7700 900123" }'
~~~

Expected:
~~~json
{ "filtered_content": "email me at ***PII-EMAIL*** or ***PII-PHONE***" }
~~~

---

## Hoppscotch / Postman Notes

- **Method/URL:** match the list above.
- **Headers (auth endpoints):**
  - `X-API-KEY: <YOUR_API_KEY>`
  - `Content-Type: application/json` (for POST)
- If you hit the apex and see **307/302**, repeat against `https://www.philmirrorenginei.ai`.

---

## OpenAI Actions (ChatGPT)

- Import from: `https://www.philmirrorenginei.ai/openapi.json`
- **Auth:** API Key  
  - Custom header name: `X-API-KEY`  
  - API key value: your secret.
- Available operations:
  - `health_check`, `healthz`, `openapi`
  - `save_memory`, `get_latest_memory`, `get_memory`, `privacy_filter`

---

## Deployment Notes (Render)

- **Start (Procfile):**
  ~~~
  web: gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 60
  ~~~
- **Env vars:**
  - `MEMORY_API_KEY=<YOUR_API_KEY>` (required)
  - `MEMORY_FILE=pmei_memories.jsonl` (optional)
  - `OPENAPI_FILENAME=openapi.json` (optional)
- **Custom Domains:**
  - Namecheap DNS  
    - `A  @   216.24.57.1`  
    - `CNAME  www   davepmei-ai.onrender.com`
  - Render → both domains must show **Verified + Certificate Issued**
- **Health Path:** `/health`
- **Storage:** JSONL is local; attach a Render Disk for durability across deploys.

---

## Troubleshooting

- **401 Unauthorized** → check exact header `X-API-KEY` and value.
- **400 Bad Request** → required fields missing; `drift_score` must be numeric.
- **`{}` from `/latest_memory`** → nothing saved yet; save then retry.
- **Redirects** → use `https://www.philmirrorenginei.ai` to avoid apex 307/302.
- **Dedup/overwrite** → reuse of identical fields may update a prior shard; use a new `slide_id` like `t-$(date +%s)`.
- **Cold start** → ping `/health` then retry.

---

## Changelog

- **1.0.3**
  - OpenAPI aligned with backend.
  - Added `request_id` in responses & items.
  - Documented `healthz` and `/openapi.json`.
  - Consolidated README with verified cURL/Hoppscotch/Actions flows.

---

## License

Proprietary — PMEi / Dave only. Do not redistribute.
