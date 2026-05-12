"""
Update portfolio prices in Excel using xlwings + yfinance.

Reads ticker/ETF symbols from:
  A6:A24

Writes current prices to:
  M6:M24
"""

import argparse
import os
import time
from datetime import datetime

import xlwings as xw
import yfinance as yf


def _resolve_workbook_path(path: str | None) -> str:
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Workbook not found: {path}")
        return path

    # Default guesses: script folder first, then current working directory.
    candidates = [
        os.path.join(os.path.dirname(__file__), "Portfolio.xlsx"),
        os.path.join(os.getcwd(), "Portfolio.xlsx"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Could not find 'Portfolio.xlsx'. Pass --file <path> explicitly."
    )


def _normalize_column(values):
    """
    xlwings may return a 1D list (single column) or a 2D list ([ [v], ... ]).
    """
    if values is None:
        return []
    if isinstance(values, list) and values and isinstance(values[0], list):
        return [row[0] for row in values]
    return values


def fetch_current_price(symbol: str):
    """
    Best-effort price fetch from Yahoo via yfinance.
    Returns a float price or None.
    """
    symbol = symbol.strip()
    if not symbol:
        return None

    t = yf.Ticker(symbol)

    # fast_info is typically faster than .info, but may be missing for some tickers.
    try:
        fast = getattr(t, "fast_info", None)
        if isinstance(fast, dict):
            # Yahoo uses 'last_price' in fast_info.
            price = fast.get("last_price")
            if price is not None:
                return float(price)
    except Exception:
        pass

    # Fallback: use the more general (slower) .info endpoint.
    try:
        info = t.info or {}
        price = info.get("regularMarketPrice") or info.get("previousClose")
        if price is not None:
            return float(price)
    except Exception:
        pass

    # Final fallback: most recent close from history.
    try:
        hist = t.history(period="5d", interval="1d")
        if not hist.empty and "Close" in hist.columns:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass

    return None


def update_portfolio_prices(
    workbook_path: str,
    sheet_name: str | None,
    source_range: str = "A6:A24",
    target_range: str = "M6:M24",
    sleep_seconds: float = 0.2,
):
    app = xw.App(visible=False)
    wb = None
    try:
        wb = app.books.open(workbook_path)
        sheet = wb.sheets[sheet_name] if sheet_name else wb.sheets[0]

        symbols_raw = sheet.range(source_range).value
        symbols = _normalize_column(symbols_raw)

        # Ensure exact length (A6:A24 and price column are 19 rows).
        expected_rows = sheet.range(source_range).rows.count
        if len(symbols) != expected_rows:
            # Pad/truncate defensively; xlwings can behave differently with empty ranges.
            symbols = (symbols + [""] * expected_rows)[:expected_rows]

        results = []
        for sym in symbols:
            sym = "" if sym is None else str(sym).strip()
            if not sym:
                results.append([""])
                continue

            price = fetch_current_price(sym)
            results.append([f"{price:.2f}" if price is not None else "N/A"])
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        sheet.range(target_range).value = results
        sheet.range(target_range).api.NumberFormat = "0.00"

        wb.save()
        print(
            f"Updated {source_range} -> {target_range} @ {datetime.now():%Y-%m-%d %H:%M:%S}"
        )
    finally:
        try:
            if wb is not None:
                wb.close()
        except Exception:
            pass
        app.quit()


def main():
    parser = argparse.ArgumentParser(
        description="Update Portfolio prices from yfinance."
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to Portfolio.xlsx (default: ./Portfolio.xlsx or script folder).",
    )
    parser.add_argument(
        "--sheet",
        default=None,
        help="Worksheet name. If omitted, uses the first sheet.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="Seconds to wait between Yahoo requests (default: 0.2).",
    )
    args = parser.parse_args()

    workbook_path = _resolve_workbook_path(args.file)
    update_portfolio_prices(
        workbook_path=workbook_path,
        sheet_name=args.sheet,
        sleep_seconds=args.sleep,
    )


if __name__ == "__main__":
    main()
