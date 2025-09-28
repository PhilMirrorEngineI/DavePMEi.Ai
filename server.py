--- a/server.py
+++ b/server.py
@@
-from flask import Flask, request, jsonify, send_from_directory, redirect
+from flask import Flask, request, jsonify, send_from_directory, redirect
 import os, time, uuid, threading
@@
-# env
-MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")
+# env
+MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")
@@
-# helpers
-def require_api_key():
-    key = request.headers.get("X-API-KEY")
-    if not key or key != MEMORY_API_KEY:
-        raise Unauthorized("Invalid or missing MEMORY_API_KEY.")
+# --- helpers ---
+def _incoming_key():
+    # Accept multiple header spellings so Actions can't fail closed.
+    return (
+        request.headers.get("X-API-KEY")
+        or request.headers.get("X_API_KEY")
+        or request.headers.get("MEMORY_API_KEY")
+        or request.args.get("key")
+    )
+
+def require_api_key():
+    key = _incoming_key()
+    if not key or key != MEMORY_API_KEY:
+        return jsonify({"error": "forbidden", "code": "forbidden"}), 403
@@
-@app.get("/health")
-def health():
-    return jsonify(ok=True, routes=["/health","/openapi.json","/save_memory","/latest_memory","/get_memory"], service="Dave PMEi memory API", ts=int(time.time()))
+@app.get("/health")
+def health():
+    return jsonify(
+        ok=True,
+        routes=["/health","/openapi.json","/save_memory","/latest_memory","/get_memory","/healthz"],
+        service="Dave PMEi memory API",
+        ts=int(time.time()),
+    )
+
+# Render sometimes defaults to /healthz
+@app.get("/healthz")
+def healthz():
+    return health()
@@
-@app.before_request
-def before():
-    # leave health and spec open
-    if request.path in ("/health", "/openapi.json"):
-        return None
-    return require_api_key()
+@app.before_request
+def before():
+    # leave health & spec open
+    if request.path in ("/health", "/healthz", "/openapi.json"):
+        return None
+    return require_api_key()
