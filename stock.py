"""Fetch selected market prices using yfinance."""

import argparse
import math
import time
from datetime import datetime

import yfinance as yf

# Trailing P/E shown in output for these symbols only (Yahoo keys: trailingPE).
PE_TICKERS = frozenset(
    {
        "NVDA",
        "MSFT",
        "SMH",
        "VRT",
        "ALAB",
        "TSM",
        "GOOGL",
        "VOO",
        "SEI ",
        "CSPX.L",
        "MU",
    }
)


def _parse_trailing_pe(raw):
    """Return float P/E or None; negative values kept (loss-making)."""
    if raw is None:
        return None  # type: ignore
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def get_cspx_price():
    """Return current price and basic info for configured tickers."""
    stocks = [
        "CSPX.L",
        "NVDA",
        "MSFT",
        "SMH",
        "VRT",
        "BTC-USD",
        "USO",
        "^VIX",
        "ALAB",
        "TSM",
        "GOOGL",
        "VOO",
        "SEI ",
        "MU",
    ]
    prices = []
    for stock in stocks:
        ticker = yf.Ticker(stock)
        info = ticker.info
        price = info.get("regularMarketPrice") or info.get("previousClose")
        currency = info.get("currency", "USD")
        name = info.get("shortName") or info.get("longName") or stock
        row = {"name": name, "price": price, "currency": currency}
        if stock in PE_TICKERS:
            row["trailing_pe"] = _parse_trailing_pe(info.get("trailingPE"))
        prices.append(row)
    return prices


def _format_pe_suffix(data: dict) -> str:
    if "trailing_pe" not in data:
        return ""
    pe = data["trailing_pe"]
    if pe is None:
        return "  P/E: n/a"
    if pe < 0:
        return f"  P/E: {pe:.2f} (negative earnings)"
    return f"  P/E: {pe:.2f}"


def print_prices():
    """Print timestamp and configured ticker prices once."""
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    for data in get_cspx_price():
        suffix = _format_pe_suffix(data)
        if data["price"] is not None:
            print(f"{data['name']}: {data['price']:.2f} {data['currency']}{suffix}")
        else:
            print(f"{data['name']}: no price{suffix}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Track selected tickers with yfinance."
    )
    parser.add_argument(
        "--live",
        nargs="?",
        const=15,
        type=int,
        metavar="SECONDS",
        help="Auto-refresh continuously every N seconds (default: 15).",
    )
    args = parser.parse_args()

    try:
        if args.live is None:
            print_prices()
        else:
            if args.live <= 0:
                raise ValueError("--live interval must be greater than 0 seconds.")
            while True:
                print_prices()
                print("-" * 40)
                time.sleep(args.live)
    except KeyboardInterrupt:
        print("\nStopped live tracking.")
    except Exception as e:
        print(f"Error: {e}")
