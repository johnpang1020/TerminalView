"""
Refresh Yahoo prices in Portfolio.xlsx, then rebuild portfolio-view.html.

Runs: update_portfolio_prices.py → export_portfolio_view.py
Optional: open the HTML in your default browser (Windows).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Update M6:M24 from Yahoo, export portfolio-view.html."
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open portfolio-view.html after export (default browser).",
    )
    parser.add_argument(
        "--skip-prices",
        action="store_true",
        help="Skip update_portfolio_prices.py (only run export).",
    )
    args, passthrough = parser.parse_known_args()

    py = sys.executable
    if not args.skip_prices:
        r = subprocess.run(
            [py, str(root / "update_portfolio_prices.py")] + passthrough,
            cwd=str(root),
        )
        if r.returncode != 0:
            sys.exit(r.returncode)

    r = subprocess.run(
        [py, str(root / "export_portfolio_view.py")] + passthrough,
        cwd=str(root),
    )
    if r.returncode != 0:
        sys.exit(r.returncode)

    if args.open:
        html = root / "portfolio-view.html"
        if html.is_file():
            webbrowser.open(html.as_uri())


if __name__ == "__main__":
    main()
