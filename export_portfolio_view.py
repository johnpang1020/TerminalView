"""
Read Portfolio.xlsx and inject JSON into portfolio-view.html.

Main holdings (rows 6–24):
  A: name/label   B: shares   C: cost per share   D: total cost (USD)
Optional market price column (default M6:M24; use L6:L24 if needed).

Bitcoin row:
  A30: label (e.g. Bitcoin)   B30: amount   D30: total cost (USD)

Display-only: updates the embedded data block consumed by portfolio-view.html.

CSV: use `--export-csv` with `--csv-range` (default `A4:D29` = rows 4–29, columns A–D).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from datetime import datetime

import xlwings as xw
import yfinance as yf


def _resolve_workbook_path(path: str | None) -> str:
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Workbook not found: {path}")
        return path
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
    if values is None:
        return []
    if isinstance(values, list) and values and isinstance(values[0], list):
        return [row[0] for row in values]
    return values


def _is_hold_row(row: dict) -> bool:
    name = str(row.get("name") or "").strip()
    if not name:
        return False
    if name.lower() == "total":
        return False
    return True


def _fetch_btc_spot_usd() -> float | None:
    try:
        t = yf.Ticker("BTC-USD")
        info = t.info or {}
        p = info.get("regularMarketPrice") or info.get("previousClose")
        if p is not None:
            return float(p)
    except Exception:
        pass
    return None


def _enrich_equity_rows(rows: list[dict]) -> list[dict]:
    for r in rows:
        name = str(r.get("name") or "").strip()
        if name.lower() == "total":
            r["marketValueUsd"] = None
            r["gainUsd"] = None
            r["gainPct"] = None
            continue
        sh = r.get("shares")
        lp = r.get("lastPrice")
        cost = r.get("totalCostUsd")
        mv = None
        if sh is not None and lp is not None:
            mv = float(sh) * float(lp)
        r["marketValueUsd"] = round(mv, 2) if mv is not None else None
        gain = None
        if mv is not None and cost is not None:
            gain = float(mv) - float(cost)
        r["gainUsd"] = round(gain, 2) if gain is not None else None
        pct = None
        if gain is not None and cost is not None and float(cost) > 0:
            pct = (gain / float(cost)) * 100.0
        r["gainPct"] = round(pct, 2) if pct is not None else None
    return rows


def _enrich_bitcoin(btc: dict, spot: float | None) -> dict:
    if spot is not None:
        btc["spotPriceUsd"] = round(spot, 2)
        amt = btc.get("amount")
        if amt is not None:
            mv = float(amt) * float(spot)
            btc["marketValueUsd"] = round(mv, 2)
            cst = btc.get("totalCostUsd")
            if cst is not None:
                g = float(mv) - float(cst)
                btc["gainUsd"] = round(g, 2)
                if float(cst) > 0:
                    btc["gainPct"] = round((g / float(cst)) * 100.0, 2)
    return btc


def _build_summary(rows: list[dict], bitcoin: dict | None) -> dict:
    """Totals for display; excludes sheet subtotal row named 'Total'."""
    hold_rows = [r for r in rows if _is_hold_row(r)]

    cost_vals = [
        float(r["totalCostUsd"])
        for r in hold_rows
        if r.get("totalCostUsd") is not None
    ]
    cost = sum(cost_vals) if cost_vals else None

    mv_list = [r["marketValueUsd"] for r in hold_rows if r.get("marketValueUsd") is not None]
    mkt = sum(float(x) for x in mv_list) if mv_list else None

    eq = {
        "label": "Stocks & ETFs",
        "costBasisUsd": round(cost, 2) if cost is not None else None,
        "marketValueUsd": round(mkt, 2) if mkt is not None else None,
        "unrealizedGainUsd": None,
    }
    if eq["costBasisUsd"] is not None and eq["marketValueUsd"] is not None:
        eq["unrealizedGainUsd"] = round(
            float(eq["marketValueUsd"]) - float(eq["costBasisUsd"]), 2
        )

    btc_block = None
    if bitcoin:
        bc = float(bitcoin["totalCostUsd"]) if bitcoin.get("totalCostUsd") is not None else None
        bm = bitcoin.get("marketValueUsd")
        btc_block = {
            "label": str(bitcoin.get("label") or "Bitcoin"),
            "costBasisUsd": round(bc, 2) if bc is not None else None,
            "marketValueUsd": round(float(bm), 2) if bm is not None else None,
        }
        if btc_block["costBasisUsd"] is not None and btc_block["marketValueUsd"] is not None:
            btc_block["unrealizedGainUsd"] = round(
                float(btc_block["marketValueUsd"]) - float(btc_block["costBasisUsd"]), 2
            )
        else:
            btc_block["unrealizedGainUsd"] = (
                round(float(bitcoin["gainUsd"]), 2)
                if bitcoin.get("gainUsd") is not None
                else None
            )

    grand_cost = None
    grand_mkt = None
    if eq["costBasisUsd"] is not None:
        grand_cost = float(eq["costBasisUsd"])
    if btc_block and btc_block.get("costBasisUsd") is not None:
        grand_cost = (grand_cost or 0) + float(btc_block["costBasisUsd"])
    if eq["marketValueUsd"] is not None:
        grand_mkt = float(eq["marketValueUsd"])
    if btc_block and btc_block.get("marketValueUsd") is not None:
        grand_mkt = (grand_mkt or 0) + float(btc_block["marketValueUsd"])

    grand = {
        "label": "Combined",
        "costBasisUsd": round(grand_cost, 2) if grand_cost is not None else None,
        "marketValueUsd": round(grand_mkt, 2) if grand_mkt is not None else None,
    }
    if grand["costBasisUsd"] is not None and grand["marketValueUsd"] is not None:
        grand["unrealizedGainUsd"] = round(
            float(grand["marketValueUsd"]) - float(grand["costBasisUsd"]),
            2,
        )

    return {"equities": eq, "bitcoin": btc_block, "grand": grand}


def _sheet_total_cost_row(rows: list[dict]) -> float | None:
    for r in rows:
        if str(r.get("name") or "").strip().lower() == "total":
            c = r.get("totalCostUsd")
            return float(c) if c is not None else None
    return None


def _parse_float_cell(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val
    s = str(raw).strip()
    if not s or s.upper() == "N/A":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _read_portfolio_rows(
    sheet,
    names_range: str,
    shares_range: str,
    cost_share_range: str,
    total_cost_range: str,
    prices_range: str | None,
):
    names = _normalize_column(sheet.range(names_range).value)
    shares = _normalize_column(sheet.range(shares_range).value)
    cost_ps = _normalize_column(sheet.range(cost_share_range).value)
    total_c = _normalize_column(sheet.range(total_cost_range).value)
    prices = None
    if prices_range:
        prices = _normalize_column(sheet.range(prices_range).value)

    start_row = sheet.range(names_range).row
    n = sheet.range(names_range).rows.count

    def pad(col, fill):
        col = col or []
        return (col + [fill] * n)[:n]

    names = pad(names, "")
    shares = pad(shares, None)
    cost_ps = pad(cost_ps, None)
    total_c = pad(total_c, None)
    if prices is not None:
        prices = pad(prices, None)

    rows = []
    for i in range(n):
        rnum = start_row + i
        nm = names[i]
        name = "" if nm is None else str(nm).strip()
        row = {
            "row": rnum,
            "name": name,
            "shares": _parse_float_cell(shares[i]),
            "costPerShare": _parse_float_cell(cost_ps[i]),
            "totalCostUsd": _parse_float_cell(total_c[i]),
        }
        if prices is not None:
            row["lastPrice"] = _parse_float_cell(
                prices[i] if i < len(prices) else None
            )
        rows.append(row)
    return rows


def _read_cell(sheet, a1: str):
    v = sheet.range(a1).value
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return v


def _read_bitcoin_block(sheet, label_cell: str, amount_cell: str, cost_cell: str):
    label_raw = _read_cell(sheet, label_cell)
    label = "" if label_raw is None else str(label_raw).strip()
    amount = _parse_float_cell(sheet.range(amount_cell).value)
    cost = _parse_float_cell(sheet.range(cost_cell).value)
    if not label and amount is None and cost is None:
        return None
    row_num = sheet.range(label_cell).row
    return {
        "row": row_num,
        "label": label or "Bitcoin",
        "amount": amount,
        "totalCostUsd": cost,
    }


def _range_values_to_rows(raw) -> list[list]:
    """Normalize xlwings range .value to a list of rows (each row is a list of cells)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [[raw]]
    if raw and not isinstance(raw[0], list):
        return [raw]
    return raw


def _write_portfolio_range_csv(path: str, rows: list[list]) -> None:
    """Write grid rows to UTF-8 CSV (Excel-friendly on Windows)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for row in rows:
            if not row:
                w.writerow([])
                continue
            out = ["" if c is None else c for c in row]
            w.writerow(out)


def _inject_json_into_html(html: str, payload: dict) -> str:
    json_body = json.dumps(payload, indent=2, ensure_ascii=False)

    def repl(match: re.Match) -> str:
        return match.group(1) + "\n" + json_body + "\n  " + match.group(2)

    out, n = re.subn(
        r'(<script type="application/json" id="portfolio-embed">\s*)[\s\S]*?(\s*</script>)',
        repl,
        html,
        count=1,
    )
    if n != 1:
        raise ValueError(
            "Could not find portfolio-embed script block in HTML (expected one match)."
        )
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Export workbook rows into portfolio-view.html embedded JSON."
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
    parser.add_argument("--names-range", default="A6:A24", help="Names (default A6:A24).")
    parser.add_argument(
        "--shares-range", default="B6:B24", help="Share quantities (default B6:B24)."
    )
    parser.add_argument(
        "--cost-share-range",
        default="C6:C24",
        help="Cost per share (default C6:C24).",
    )
    parser.add_argument(
        "--total-cost-range",
        default="D6:D24",
        help="Total cost USD (default D6:D24).",
    )
    parser.add_argument(
        "--prices-range",
        default="M6:M24",
        help="Optional last/market price column (default M6:M24). Empty string skips.",
    )
    parser.add_argument(
        "--bitcoin-label-cell",
        default="A30",
        help="Bitcoin label cell (default A30).",
    )
    parser.add_argument(
        "--bitcoin-amount-cell",
        default="B30",
        help="Bitcoin amount cell (default B30).",
    )
    parser.add_argument(
        "--bitcoin-cost-cell",
        default="D30",
        help="Bitcoin total cost USD cell (default D30).",
    )
    parser.add_argument(
        "--no-prices",
        action="store_true",
        help="Do not read any price column.",
    )
    parser.add_argument(
        "--no-btc-spot",
        action="store_true",
        help="Do not fetch BTC-USD spot for Bitcoin P&L (offline).",
    )
    parser.add_argument(
        "--html",
        default=None,
        help="Input/output HTML path (default: portfolio-view.html next to this script).",
    )
    parser.add_argument(
        "--export-csv",
        default=None,
        metavar="PATH",
        help="Write the given sheet range to a CSV file (see --csv-range).",
    )
    parser.add_argument(
        "--csv-range",
        default="A4:D29",
        help="Excel A1 range for CSV export (default A4:D29 = rows 4–29, cols A–D).",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip updating portfolio-view.html (use with --export-csv for CSV-only).",
    )
    args = parser.parse_args()

    workbook_path = _resolve_workbook_path(args.file)
    html_path = args.html or os.path.join(
        os.path.dirname(__file__), "portfolio-view.html"
    )
    if args.no_html and not args.export_csv:
        raise SystemExit(
            "Nothing to do: pass --export-csv PATH or omit --no-html to update HTML."
        )

    if not args.no_html and not os.path.exists(html_path):
        raise FileNotFoundError(f"HTML template not found: {html_path}")

    app = xw.App(visible=False)
    wb = None
    try:
        wb = app.books.open(workbook_path)
        sheet = wb.sheets[args.sheet] if args.sheet else wb.sheets[0]
        if args.export_csv:
            raw_grid = sheet.range(args.csv_range.strip()).value
            grid_rows = _range_values_to_rows(raw_grid)
            _write_portfolio_range_csv(args.export_csv, grid_rows)
            print(f"Wrote CSV: {args.export_csv} ({args.csv_range})")
        if args.no_html:
            return
        price_rng = None
        if not args.no_prices and args.prices_range.strip():
            price_rng = args.prices_range.strip()
        rows = _read_portfolio_rows(
            sheet,
            args.names_range,
            args.shares_range,
            args.cost_share_range,
            args.total_cost_range,
            price_rng,
        )
        bitcoin = _read_bitcoin_block(
            sheet,
            args.bitcoin_label_cell,
            args.bitcoin_amount_cell,
            args.bitcoin_cost_cell,
        )
    finally:
        try:
            if wb is not None:
                wb.close()
        except Exception:
            pass
        app.quit()

    rows = _enrich_equity_rows(rows)
    btc_spot = None if args.no_btc_spot else _fetch_btc_spot_usd()
    if bitcoin is not None:
        bitcoin = _enrich_bitcoin(bitcoin, btc_spot)

    summary = _build_summary(rows, bitcoin)
    sheet_total = _sheet_total_cost_row(rows)

    payload = {
        "meta": {
            "title": "Portfolio",
            "subtitle": "Rows 6–24: A name · B shares · C cost/share · D total cost · M last — with market value & unrealized P&L",
            "asOf": datetime.now().replace(microsecond=0).isoformat(),
            "sheetTotalCostUsd": round(sheet_total, 2) if sheet_total is not None else None,
        },
        "summary": summary,
        "rows": rows,
    }
    if bitcoin is not None:
        payload["bitcoin"] = bitcoin

    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    html_out = _inject_json_into_html(html, payload)
    with open(html_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html_out)

    named = sum(1 for r in rows if str(r.get("name", "")).strip())
    extra = " + bitcoin" if bitcoin else ""
    print(
        f"Wrote {html_path} ({named} named rows{extra}, as of {payload['meta']['asOf']})"
    )


if __name__ == "__main__":
    main()
