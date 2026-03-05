"""Fetch CSPX (iShares Core S&P 500 UCITS ETF) price using yfinance."""

import yfinance as yf


def get_stock_price():
    """Return current price and basic info for configured tickers."""
    stocks = ["CSPX.L", "NVDA", "MSFT", "SMH", "VRT"]
    prices = []
    for stock in stocks:
        ticker = yf.Ticker(stock)
        info = ticker.info
        price = info.get("regularMarketPrice") or info.get("previousClose")
        currency = info.get("currency", "USD")
        name = info.get("shortName") or info.get("longName") or stock
        prices.append({"name": name, "price": price, "currency": currency})
    return prices


if __name__ == "__main__":
    try:
        for data in get_stock_price():
            if data["price"] is not None:
                print(f"{data['name']}: {data['price']:.2f} {data['currency']}")
            else:
                print(f"{data['name']}: no price")
    except Exception as e:
        print(f"Error: {e}")
