# DavePMEi.Ai

[![Render Deploy](https://img.shields.io/badge/Render-Live-brightgreen)](https://davepmei-ai.onrender.com/health)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

**DavePMEi.Ai** is a secure Flask API service for lawful memory storage and glyph echoes.  
It runs as part of the **PhilMirrorEnginei.ai (PMEi)** framework.

---

## 🚀 Endpoints

- `GET /health` → service status, version, parent project  
- `GET /openapi.json` → OpenAPI 3.1 spec  
- `POST /save_memory` → save a memory shard  
- `GET /latest_memory` → fetch the most recent memory  
- `GET /get_memory?limit=N` → list up to N memories (newest first)  

---

## 📦 Quick Start (local)

```bash
git clone https://github.com/PhilMirrorEngine/DavePMEi.Ai.git
cd DavePMEi.Ai

# Install
pip install -r requirements.txt

# Run
export MEMORY_API_KEY=dev-key
gunicorn server:app
