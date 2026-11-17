"""Gate 1: check the frozen model lineup (protocol section 8, v1.10).

Calls each model in agents/models.py once through the OpenRouter client,
pinned to its official provider's standard endpoint with fallbacks off, and
prints which provider served it and which model it reported.
Never prints the API key.  Run from the repo root:  venv/bin/python scripts/test_pinned.py
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.llm import LLMCapped, LLMUnavailable, model_matches  # noqa: E402
from agents.models import MODELS  # noqa: E402
from agents.openrouter import OpenRouterClient, OpenRouterError  # noqa: E402

load_dotenv(ROOT / ".env")
client = OpenRouterClient()

ok = True
for m in MODELS.values():
    try:
        r = client.complete("Reply with the single word: ready", model=m)
        good = r.provider_served in m.provider_names and model_matches(m.model_id, r.model_id)
        ok &= good
        print(f"{m.name:9} {'OK ' if good else 'BAD'}  {m.model_id:32} served by {r.provider_served}, "
              f"reported {r.model_id}")
    except (LLMUnavailable, LLMCapped, OpenRouterError) as e:
        ok = False
        print(f"{m.name:9} ERR  {m.model_id:32} {type(e).__name__}: {e}")
print("\nLineup confirmed: every model served by its own maker." if ok else "\nProblem: paste this output to Claude.")
