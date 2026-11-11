import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

trading_client = TradingClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

account = trading_client.get_account()
print(f"Account status: {account.status}")
print(f"Buying power: {account.buying_power}")
print(f"Cash: {account.cash}")
