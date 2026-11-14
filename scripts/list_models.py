"""Gate 1: list the current models from each maker on OpenRouter, newest first,
with prices and which providers serve them. Never prints the API key.
Run from the repo root:  python3 scripts/list_models.py
"""
import datetime as dt
import os

import requests
from dotenv import load_dotenv

load_dotenv()
H = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"}
BASE = "https://openrouter.ai/api/v1"
MAKERS = ["anthropic/", "openai/", "google/", "deepseek/", "x-ai/"]
SKIP = (":free", "image", "audio", "embed", "search", "oss", "gemma", "tts", "realtime")

models = requests.get(f"{BASE}/models", headers=H, timeout=30).json()["data"]
for prefix in MAKERS:
    mine = sorted((m for m in models if m["id"].startswith(prefix) and not any(s in m["id"] for s in SKIP)),
                  key=lambda m: m.get("created", 0), reverse=True)[:6]
    print(f"\n=== {prefix[:-1]} ===")
    for m in mine:
        p = m.get("pricing", {})
        pin = float(p.get("prompt", 0)) * 1e6
        pout = float(p.get("completion", 0)) * 1e6
        made = dt.date.fromtimestamp(m.get("created", 0)).isoformat()
        try:
            eps = requests.get(f"{BASE}/models/{m['id']}/endpoints", headers=H, timeout=30).json()["data"]["endpoints"]
            provs = ", ".join(sorted({e.get("provider_name", "?") for e in eps}))
        except Exception:
            provs = "?"
        print(f"{m['id']:45} {made}  in ${pin:6.2f}/M  out ${pout:6.2f}/M  providers: {provs}")
