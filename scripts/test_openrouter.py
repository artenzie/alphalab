"""Smoke test: one tiny request to each of the five model makers (D9) through OpenRouter.

Prints the model used and OK / the error. Never prints the API key.
Run from the repo root:  python3 scripts/test_openrouter.py
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("OPENROUTER_API_KEY")
if not KEY:
    sys.exit("OPENROUTER_API_KEY is missing from .env")

BASE = "https://openrouter.ai/api/v1"
HEADERS = {"Authorization": f"Bearer {KEY}"}
MAKERS = {"Claude": "anthropic/", "GPT": "openai/", "Gemini": "google/", "DeepSeek": "deepseek/", "Grok": "x-ai/"}
SKIP = (":free", "mini", "nano", "lite", "image", "audio", "embed", "search", "oss", "gemma")


def newest_model(prefix, models):
    """Newest full-size chat model from one maker (a smoke test, not the final choice)."""
    mine = [m for m in models if m["id"].startswith(prefix) and not any(s in m["id"] for s in SKIP)]
    return max(mine, key=lambda m: m.get("created", 0))["id"] if mine else None


models = requests.get(f"{BASE}/models", headers=HEADERS, timeout=30).json()["data"]
all_ok = True
for name, prefix in MAKERS.items():
    model = newest_model(prefix, models)
    if not model:
        print(f"{name:7} NO MODEL FOUND for {prefix}")
        all_ok = False
        continue
    try:
        r = requests.post(
            f"{BASE}/chat/completions",
            headers=HEADERS,
            json={"model": model, "max_tokens": 50,
                  "messages": [{"role": "user", "content": "Reply with the single word: ready"}]},
            timeout=90,
        )
        if r.status_code == 200:
            print(f"{name:7} OK      {model}")
        else:
            print(f"{name:7} ERROR {r.status_code}  {model}  {r.text[:200]}")
            all_ok = False
    except requests.RequestException as e:
        print(f"{name:7} FAILED  {model}  {type(e).__name__}")
        all_ok = False

credits = requests.get(f"{BASE}/credits", headers=HEADERS, timeout=30)
if credits.ok:
    d = credits.json().get("data", {})
    print(f"\nBalance used so far: ${d.get('total_usage', 0):.4f} of ${d.get('total_credits', 0):.2f}")
print("\nAll five working." if all_ok else "\nSomething failed: paste this output to Claude.")
