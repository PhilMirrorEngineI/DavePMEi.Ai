# PMEi Memory API (Dave)

Secure, lawful memory shards for PhilMirrorEnginei.ai.

Base URLs:
- Primary: https://www.philmirrorenginei.ai
- Apex (redirects to primary): https://philmirrorenginei.ai

Authentication:
Every protected request must include your API key header:
X-API-KEY: <your key>

Endpoints:
- GET /health → Public service health
- GET /healthz → Liveness (alias for /health)
- GET /openapi.json → API specification
- POST /save_memory → Save a shard (auth required)
- GET /latest_memory → Return most recent shard (auth required)
- GET /get_memory?limit=10 → List shards, newest first (auth required)
- POST /privacy_filter → Run minimal PII filter on input text (auth required)

Save Memory Request Example:
{ "user_id": "phil", "thread_id": "smoke", "slide_id": "t-001", "glyph_echo": "ping", "drift_score": 0, "seal": "ok", "content": "smoke test" }

Save Memory Response Example:
{ "status": "saved", "slide_id": "t-001" }

Quick cURL Examples:
BASE=https://www.philmirrorenginei.ai
KEY=YOUR_API_KEY
curl -s $BASE/health
curl -s -X POST $BASE/save_memory -H 'Content-Type: application/json' -H "X-API-KEY: $KEY" -d '{"user_id":"phil","thread_id":"smoke","slide_id":"t-001","glyph_echo":"ping","drift_score":0,"seal":"ok","content":"smoke test"}'
curl -s -H "X-API-KEY: $KEY" $BASE/latest_memory
curl -s -H "X-API-KEY: $KEY" "$BASE/get_memory?limit=5"
curl -s -X POST $BASE/privacy_filter -H 'Content-Type: application/json' -H "X-API-KEY: $KEY" -d '{"content":"my password is 123"}'

Hoppscotch Setup:
- base = https://www.philmirrorenginei.ai
- key = <your api key>
Requests:
GET {{base}}/health
POST {{base}}/save_memory with headers Content-Type: application/json and X-API-KEY: {{key}} and JSON body
GET {{base}}/latest_memory with X-API-KEY
GET {{base}}/get_memory?limit=5 with X-API-KEY

Deployment Notes:
Procfile: web: gunicorn --workers 1 --threads 2 --timeout 60 --bind 0.0.0.0:$PORT server:app
Env vars: MEMORY_API_KEY=<your key>, DAVEPMEI_HOST=philmirrorenginei.ai
DNS: A @ 216.24.57.1, CNAME www davepmei-ai.onrender.com
Render: both domains must show Verified + Certificate Issued
Health path: /health
Storage: JSONL ephemeral, attach Render Disk for persistence

Troubleshooting:
401 Unauthorized → check API key and header
405 Method Not Allowed → check HTTP method
307/308 Redirect → use www.philmirrorenginei.ai
200 {} or empty → ensure full required fields
Cold start delay → hit /health once then retry
