"""Gate 1: build the trading universe by rule (protocol section 2, decision D4).

Downloads today's holdings files for the 11 Select Sector SPDR ETFs from State
Street, takes the top 2 holdings by weight from each (skipping second share
classes and companies already chosen), adds SPY, checks every symbol on Alpaca,
and saves the evidence plus research/universe/universe.csv.

Needs: pip install openpyxl     Run from the repo root:  python3 scripts/build_universe.py
"""
import datetime as dt
import io
import os
import sys

import pandas as pd
import requests
from dotenv import load_dotenv

ETFS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/"
       "etfs/us/holdings-daily-us-en-{}.xlsx")
SHARE_CLASSES = [{"GOOGL", "GOOG"}, {"FOXA", "FOX"}, {"NWSA", "NWS"}, {"BRK.A", "BRK.B"}]
PER_ETF = 2

today = dt.date.today().isoformat()
outdir = os.path.join("research", "universe", f"holdings_{today}")
os.makedirs(outdir, exist_ok=True)


def holdings(etf):
    r = requests.get(URL.format(etf.lower()), timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    with open(os.path.join(outdir, f"{etf}.xlsx"), "wb") as f:
        f.write(r.content)
    raw = pd.read_excel(io.BytesIO(r.content), header=None)
    header_row = raw.index[raw.iloc[:, 0].astype(str).str.strip() == "Name"][0]
    df = raw.iloc[header_row + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_row]]
    df = df[pd.to_numeric(df["Weight"], errors="coerce").notna()]
    df["Weight"] = df["Weight"].astype(float)
    df["Ticker"] = df["Ticker"].astype(str).str.strip().str.replace(" ", ".")
    return df.sort_values("Weight", ascending=False)


def same_company(a, b):
    return any(a in pair and b in pair for pair in SHARE_CLASSES)


chosen = []
for etf in ETFS:
    df = holdings(etf)
    picked = 0
    for _, row in df.iterrows():
        t = row["Ticker"]
        if t in ("-", "nan", "") or any(t == c["symbol"] or same_company(t, c["symbol"]) for c in chosen):
            continue
        chosen.append({"symbol": t, "name": row["Name"], "sector_etf": etf, "weight_pct": round(row["Weight"], 2)})
        picked += 1
        if picked == PER_ETF:
            break
chosen.append({"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "sector_etf": "-", "weight_pct": None})

# Check every symbol on Alpaca (tradable, shortable, easy to borrow, fractionable).
load_dotenv()
try:
    from alpaca.trading.client import TradingClient
    tc = TradingClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"), paper=True)
    for c in chosen:
        a = tc.get_asset(c["symbol"])
        c.update(tradable=a.tradable, shortable=a.shortable, easy_to_borrow=a.easy_to_borrow, fractionable=a.fractionable)
except Exception as e:  # report, don't hide
    print(f"Alpaca check failed: {type(e).__name__}: {e}")

uni = pd.DataFrame(chosen)
uni.insert(0, "freeze_date", today)
uni.to_csv(os.path.join("research", "universe", "universe.csv"), index=False)
pd.set_option("display.width", 160)
print(uni.drop(columns=["freeze_date"]).to_string(index=False))
print(f"\n{len(uni) - 1} stocks + SPY. Saved research/universe/universe.csv and the raw files in {outdir}/")
