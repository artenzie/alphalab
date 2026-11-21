"""Smoke test of the five AI models end to end (protocol §8, v1.10). Costs cents.

Builds the backtest snapshot for Fri 13 Nov 2026 from a READ-ONLY database
connection and runs agents 8-12 (Claude, GPT, Gemini, DeepSeek, Grok) once each
through AIAgent + OpenRouterClient with the placeholder template and a fresh
$100,000 all-cash portfolio. Prints, per model: status, provider served, model
reported, the first targets, tokens (reasoning included in out), cost, latency.

No retry back-off (an outage shows as 'unavailable' at once). Nothing is
written to the database; row counts are checked before and after.
Never prints the API key.  Run from the repo root:  venv/bin/python scripts/smoke_ai.py
"""
import sqlite3
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.harness import AIAgent  # noqa: E402
from agents.templates import load as load_template  # noqa: E402
from agents.models import model_for  # noqa: E402
from agents.openrouter import OpenRouterClient  # noqa: E402
from data import db  # noqa: E402
from data.snapshot import build_snapshot  # noqa: E402
from scripts.backtest_indicators import no_network  # noqa: E402
from strategies.base import AgentMemory, Context, Portfolio  # noqa: E402

DAY = date(2026, 11, 13)
AGENTS = (8, 9, 10, 11, 12)
START = 100_000.0
SHOW_TARGETS = 5
TABLES = ("prices", "snapshots", "decisions", "orders", "fills", "positions", "equity", "risk_events", "heartbeat",
          "calendar_fetches", "market_sessions", "mirror_recon")


def counts(path: Path) -> dict[str, int]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        c.close()


def main() -> int:
    load_dotenv(ROOT / ".env")
    before = counts(db.DEFAULT_DB_PATH)
    conn = sqlite3.connect(f"file:{db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        snap = build_snapshot(conn, "us_equity", DAY, "backtest", trading=no_network)
    finally:
        conn.close()
    print(f"Snapshot {snap.decision_date} (backtest, cutoff {snap.cutoff_ts}, "
          f"{len(snap.payload['symbols'])} symbols, sha {snap.sha256[:12]})\n")

    portfolio = Portfolio(cash=START, positions={}, value=START)
    ctx = replace(Context(DAY, False, False, False), memory=AgentMemory(starting_value=START))
    client = OpenRouterClient()
    total = 0.0
    for agent_id in AGENTS:
        model = model_for(agent_id)
        agent = AIAgent(agent_id, model, load_template(agent_id), client, sleep=lambda s: None, backoff=())
        dec = agent(snap, portfolio, ctx)
        rec = dec.record
        print(f"== agent {agent_id} · {model.name} · {model.model_id} "
              f"(reasoning {model.reasoning}, temperature {model.temperature}, max {model.max_tokens})")
        print(f"   status: {rec.status}   ({len(rec.attempts)} attempt{'s' if len(rec.attempts) > 1 else ''})")
        for a in rec.attempts:
            cost = "?" if a.cost_usd is None else f"${a.cost_usd:.4f}"
            total += a.cost_usd or 0.0
            print(f"   #{a.attempt} {a.status:11} provider {a.provider_served}  model {a.model_id}")
            print(f"      tokens in {a.tokens_in}, out {a.tokens_out} (reasoning {a.reasoning_tokens}), "
                  f"cost {cost}, latency {a.latency_ms} ms")
            if a.error:
                print(f"      error: {a.error}")
        if rec.status == "ok":
            if dec.weights is None:
                print("   decision: hold")
            else:
                w = sorted(dec.weights.items(), key=lambda kv: -abs(kv[1]))
                shown = ", ".join(f"{s} {x:+.1%}" for s, x in w[:SHOW_TARGETS])
                more = f" … (+{len(w) - SHOW_TARGETS} more)" if len(w) > SHOW_TARGETS else ""
                conf = rec.attempts[-1].parsed.confidence
                print(f"   targets ({len(w)}, gross {sum(abs(x) for _, x in w):.0%}, "
                      f"confidence {conf}): {shown}{more}")
        print(f"   reason: {dec.reason[:160]}\n")
    after = counts(db.DEFAULT_DB_PATH)
    print(f"Total cost: ${total:.4f}")
    print("Database unchanged." if after == before else f"DATABASE CHANGED: {before} -> {after}")
    return 0 if after == before else 1


if __name__ == "__main__":
    sys.exit(main())
