"""Gate 1: check the frozen model lineup (protocol section 8).

Calls each chosen model once through OpenRouter, pinned to its official
provider with fallbacks off, and prints which provider actually served it.
Never prints the API key.  Run from the repo root:  python3 scripts/test_pinned.py
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()
H = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"}
URL = "https://openrouter.ai/api/v1/chat/completions"

LINEUP = [  # (agent, model id, official provider names allowed)
    ("Claude   (8)", "anthropic/claude-sonnet-5.5", ["Anthropic"]),
    ("GPT      (9)", "openai/gpt-6.1-sol", ["OpenAI"]),
    ("Gemini  (10)", "google/gemini-4-carbon", ["Google AI Studio", "Google"]),
    ("DeepSeek(11)", "deepseek/deepseek-v4-pro-0813", ["DeepSeek"]),
    ("Grok    (12)", "x-ai/grok-4.7", ["xAI"]),
]

ok = True
for agent, model, providers in LINEUP:
    body = {
        "model": model,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
        "provider": {"only": providers, "allow_fallbacks": False},
    }
    try:
        r = requests.post(URL, headers=H, json=body, timeout=120)
        if r.status_code == 200:
            served = r.json().get("provider", "?")
            good = served in providers
            ok &= good
            print(f"{agent}  {'OK ' if good else 'BAD'}  {model:32} served by {served}")
        else:
            ok = False
            print(f"{agent}  ERR  {model:32} {r.status_code} {r.text[:160]}")
    except requests.RequestException as e:
        ok = False
        print(f"{agent}  ERR  {model:32} {type(e).__name__}")
print("\nLineup confirmed: every model served by its own maker." if ok else "\nProblem: paste this output to Claude.")
