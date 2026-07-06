"""Fetch selected market prices safely and instantly using yfinance.

Rates/credit block uses Yahoo only (no FRED) to avoid overnight data lag during
US session — critical when HK-evening selloffs move HYG/IEF before FRED updates.
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PE_TICKERS = {
    "TSM",
    "MSFT",
    "CSPX.L",
    "CRCL",
    "NVDA",
    "SMH",
    "VRT",
    "ALAB",
    "SEI",
    "BTC-USD",
    "USO",
    "^VIX",
    "NOK",
    "IBM",
    "SPYL.L",
}
MACRO_TICKERS = ["^TNX", "2YY=F", "^IRX", "SR3=F", "HYG", "IEF"]
ALL_STOCKS = [
    "TSM",
    "MSFT",
    "CSPX.L",
    "CRCL",
    "NVDA",
    "SMH",
    "VRT",
    "ALAB",
    "SEI",
    "BTC-USD",
    "USO",
    "^VIX",
    "NOK",
    "IBM",
    "SPYL.L",
]
HISTORY_PERIOD = "1mo"
SMH_HISTORY_PERIOD = "6mo"
MA_WINDOW = 50
ROLLING_WINDOW = 5
CREDIT_SPREAD_SIGNAL_BPS = 220
CREDIT_VELOCITY_SIGNAL_BPS = 20
SIGNAL_BANNER = "!" * 60
EXECUTION_ALERT_MSG = (
    "【核彈級買點】指標已達標，立刻登入 IBKR 賣出 SGOV，分批滿倉 SMH！"
)
ADJ_CLOSE_SYMBOLS = frozenset({"HYG", "IEF", "SGOV"})
SMH_DMA_TICKERS = ("SMH", "^SOX")
SMH_GUIDANCE_TICKERS = ("NVDA", "TSM", "AVGO", "MU", "AMD", "QCOM")
SMH_MEMORY_TICKERS = ("MU", "000660.KS")
SMH_INVENTORY_TICKERS = ("NVDA", "TSM", "MU", "AVGO", "AMD")


def _parse_trailing_pe(raw):
    """Return float P/E or None; negative values kept (loss-making)."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def _price_series(hist_df, symbol, use_adj=False):
    """Return cleaned price series; bond ETFs use Adj Close to skip ex-div gaps."""
    try:
        block = hist_df[symbol]
        if use_adj and "Adj Close" in block.columns:
            series = block["Adj Close"].dropna()
        else:
            series = block["Close"].dropna()
        return series if not series.empty else None
    except Exception:
        return None


def _series_for_symbol(hist_df, symbol):
    return _price_series(hist_df, symbol, use_adj=symbol in ADJ_CLOSE_SYMBOLS)


def fetch_all_market_data():
    """Bulk download prices, fundamentals, and macro history (Yahoo only)."""
    all_symbols = list(set(ALL_STOCKS + MACRO_TICKERS))
    hist_df = yf.download(
        all_symbols, period=HISTORY_PERIOD, group_by="ticker", progress=False
    )

    prices_map = {}
    for sym in all_symbols:
        series = _series_for_symbol(hist_df, sym)
        if series is None:
            series = _price_series(hist_df, sym, use_adj=False)
        prices_map[sym] = float(series.iloc[-1]) if series is not None else None

    tickers_batch = yf.Tickers(all_symbols)
    pe_map = {}
    hyg_yield = None
    for sym in all_symbols:
        try:
            info = tickers_batch.tickers[sym].info
            if sym in PE_TICKERS:
                pe_map[sym] = _parse_trailing_pe(info.get("trailingPE"))
            if sym == "HYG":
                raw_yield = info.get("yield")
                if raw_yield is not None:
                    hyg_yield = float(raw_yield) * 100
        except Exception:
            pass

    return prices_map, pe_map, hyg_yield, hist_df


def _relative_return_bps(series, days=1):
    """Price return over N sessions, in bps."""
    if series is None or len(series) <= days:
        return None
    return (series.iloc[-1] / series.iloc[-1 - days] - 1) * 10000


def _build_credit_spread_series(hyg_series, ief_series, tnx_series, hyg_yield):
    """Anchor yield-level spread, then walk back using HYG vs IEF relative moves."""
    if hyg_series is None or ief_series is None or tnx_series is None:
        return None

    ten_y = float(tnx_series.iloc[-1])
    if hyg_yield is None:
        hyg_price = float(hyg_series.iloc[-1])
        hyg_yield = (4.75 / hyg_price) * 100 if hyg_price else None
    if hyg_yield is None:
        return None

    aligned = pd.concat(
        [hyg_series.rename("hyg"), ief_series.rename("ief")], axis=1
    ).dropna()
    if aligned.empty:
        return None

    spreads = [float((hyg_yield - ten_y) * 100)]
    for i in range(len(aligned) - 2, -1, -1):
        hyg_ret = aligned["hyg"].iloc[i + 1] / aligned["hyg"].iloc[i] - 1
        ief_ret = aligned["ief"].iloc[i + 1] / aligned["ief"].iloc[i] - 1
        spreads.insert(0, spreads[0] + (hyg_ret - ief_ret) * 10000)

    return pd.Series(spreads, index=aligned.index)


def get_rates_credit(prices_map, hyg_yield, hist_df):
    """Real-time spreads + rolling credit velocity (no FRED lag)."""
    ten_y = prices_map.get("^TNX")
    two_y = prices_map.get("2YY=F")
    three_m = prices_map.get("^IRX")
    sofr_fut = prices_map.get("SR3=F")

    hyg_series = _series_for_symbol(hist_df, "HYG")
    ief_series = _series_for_symbol(hist_df, "IEF")
    tnx_series = _price_series(hist_df, "^TNX", use_adj=False)
    vix_series = _price_series(hist_df, "^VIX", use_adj=False)

    if hyg_yield is None and hyg_series is not None:
        hyg_yield = (4.75 / float(hyg_series.iloc[-1])) * 100

    yield_spread_10y2y = (ten_y - two_y) * 100 if ten_y and two_y else None
    yield_spread_10y3m = (ten_y - three_m) * 100 if ten_y and three_m else None
    yield_spread_bps = (
        yield_spread_10y2y if yield_spread_10y2y is not None else yield_spread_10y3m
    )

    sofr_implied = 100 - sofr_fut if sofr_fut else None
    sofr_spread = (sofr_implied - three_m) * 100 if sofr_implied and three_m else None
    hy_spread_level = (hyg_yield - ten_y) * 100 if hyg_yield and ten_y else None

    hyg_rel_1d = _relative_return_bps(hyg_series, 1)
    ief_rel_1d = _relative_return_bps(ief_series, 1)
    hyg_vs_ief_1d = (
        (hyg_rel_1d - ief_rel_1d)
        if hyg_rel_1d is not None and ief_rel_1d is not None
        else None
    )

    spread_series = _build_credit_spread_series(
        hyg_series, ief_series, tnx_series, hyg_yield
    )
    spread_ma = spread_velocity = credit_spread_live = None
    credit_signal = False
    if spread_series is not None:
        ma = spread_series.rolling(ROLLING_WINDOW, min_periods=1).mean()
        credit_spread_live = float(spread_series.iloc[-1])
        spread_ma = float(ma.iloc[-1])
        spread_velocity = credit_spread_live - spread_ma
        credit_signal = (
            credit_spread_live > CREDIT_SPREAD_SIGNAL_BPS
            and spread_velocity > CREDIT_VELOCITY_SIGNAL_BPS
        )

    vix_level = prices_map.get("^VIX")
    vix_1d_chg = _relative_return_bps(vix_series, 1)

    return {
        "ten_y_pct": ten_y,
        "two_y_pct": two_y,
        "three_m_pct": three_m,
        "yield_curve_spread_bps": yield_spread_bps,
        "yield_curve_10y3m_bps": yield_spread_10y3m,
        "sofr_implied_pct": sofr_implied,
        "irs_swap_spread_bps": sofr_spread,
        "hy_yield_pct": hyg_yield,
        "credit_spread_bps": hy_spread_level,
        "credit_spread_live_bps": credit_spread_live,
        "credit_spread_ma_bps": spread_ma,
        "spread_velocity_bps": spread_velocity,
        "hyg_vs_ief_1d_bps": hyg_vs_ief_1d,
        "credit_signal": credit_signal,
        "execution_signal": credit_signal,
        "vix_level": vix_level,
        "vix_1d_chg_bps": vix_1d_chg,
    }


def _format_pe_suffix(sym, pe_map) -> str:
    if sym not in PE_TICKERS:
        return ""
    pe = pe_map.get(sym)
    if pe is None:
        return "  P/E: n/a"
    if pe < 0:
        return f"  P/E: {pe:.2f} (negative earnings)"
    return f"  P/E: {pe:.2f}"


def _ma50_status(series):
    """Return price vs 50-day MA; None if insufficient history."""
    if series is None or len(series) < MA_WINDOW:
        return None
    price = float(series.iloc[-1])
    ma50 = float(series.rolling(MA_WINDOW).mean().iloc[-1])
    pct_from_ma = (price / ma50 - 1) * 100
    return {
        "price": price,
        "ma50": ma50,
        "below_ma50": price < ma50,
        "pct_from_ma": pct_from_ma,
    }


def _guidance_flags(ticker_obj):
    """Proxy for FY guidance cuts: recent miss or negative FY estimate growth."""
    flags = []
    try:
        hist = ticker_obj.earnings_history
        if hist is not None and not hist.empty:
            surprise = hist.iloc[-1].get("surprisePercent")
            if surprise is not None and float(surprise) < 0:
                flags.append("recent EPS miss")
    except Exception:
        pass
    try:
        est = ticker_obj.earnings_estimate
        if est is not None and not est.empty and "0y" in est.index:
            growth = est.loc["0y", "growth"]
            if growth is not None and float(growth) < 0:
                flags.append("FY est. growth negative")
    except Exception:
        pass
    return flags


def _inventory_status(ticker_obj):
    """Latest QoQ inventory change; flag consecutive rises."""
    try:
        bs = ticker_obj.quarterly_balance_sheet
        if bs is None or bs.empty or "Inventory" not in bs.index:
            return None
        inv = bs.loc["Inventory"].dropna().sort_index(ascending=False)
        if len(inv) < 2:
            return None
        latest = float(inv.iloc[0])
        prior = float(inv.iloc[1])
        qoq_pct = (latest / prior - 1) * 100 if prior else None
        consecutive_rise = False
        if len(inv) >= 3 and float(inv.iloc[2]):
            prior_q = (float(inv.iloc[1]) / float(inv.iloc[2]) - 1) * 100
            consecutive_rise = qoq_pct is not None and qoq_pct > 0 and prior_q > 0
        return {
            "qoq_pct": qoq_pct,
            "rising": qoq_pct is not None and qoq_pct > 0,
            "consecutive_rise": consecutive_rise,
        }
    except Exception:
        return None


def get_smh_health():
    """SMH/SOX trend, guidance proxy, memory momentum, inventory build."""
    symbols = list(
        dict.fromkeys(
            list(SMH_DMA_TICKERS)
            + list(SMH_MEMORY_TICKERS)
            + list(SMH_GUIDANCE_TICKERS)
            + list(SMH_INVENTORY_TICKERS)
        )
    )
    hist_df = yf.download(
        symbols, period=SMH_HISTORY_PERIOD, group_by="ticker", progress=False
    )
    tickers_batch = yf.Tickers(symbols)

    dma = {}
    for sym in SMH_DMA_TICKERS:
        series = _price_series(hist_df, sym, use_adj=False)
        dma[sym] = _ma50_status(series)

    smh_series = _price_series(hist_df, "SMH", use_adj=False)
    memory = {}
    smh_20d = _relative_return_bps(smh_series, 20)
    for sym in SMH_MEMORY_TICKERS:
        mem_series = _price_series(hist_df, sym, use_adj=False)
        mem_20d = _relative_return_bps(mem_series, 20)
        rel_vs_smh = (
            mem_20d - smh_20d if mem_20d is not None and smh_20d is not None else None
        )
        memory[sym] = {
            "return_20d_bps": mem_20d,
            "rel_vs_smh_20d_bps": rel_vs_smh,
            "weakening": mem_20d is not None
            and mem_20d < 0
            and (rel_vs_smh or 0) < -150,
        }

    guidance = {}
    for sym in SMH_GUIDANCE_TICKERS:
        flags = _guidance_flags(tickers_batch.tickers[sym])
        guidance[sym] = {"flags": flags, "cut_risk": bool(flags)}

    inventory = {}
    for sym in SMH_INVENTORY_TICKERS:
        inventory[sym] = _inventory_status(tickers_batch.tickers[sym])

    bearish = 0
    if (dma.get("SMH") or {}).get("below_ma50"):
        bearish += 1
    if (dma.get("^SOX") or {}).get("below_ma50"):
        bearish += 1
    if any(m.get("weakening") for m in memory.values()):
        bearish += 1
    if sum(1 for g in guidance.values() if g.get("cut_risk")) >= 2:
        bearish += 1
    if (
        sum(
            1
            for inv in inventory.values()
            if inv and (inv.get("consecutive_rise") or (inv.get("qoq_pct") or 0) > 10)
        )
        >= 2
    ):
        bearish += 1

    return {
        "dma": dma,
        "memory": memory,
        "guidance": guidance,
        "inventory": inventory,
        "bearish_count": bearish,
        "bearish_max": 5,
    }


def _print_smh_health(data):
    print("\n--- SMH / Semiconductor Health ---")

    for sym in SMH_DMA_TICKERS:
        row = data["dma"].get(sym)
        if not row:
            print(f"{sym}: 50D MA unavailable (need {MA_WINDOW}+ sessions)")
            continue
        status = "BELOW 50D MA" if row["below_ma50"] else "above 50D MA"
        print(
            f"{sym}: {row['price']:.2f} | 50D MA {row['ma50']:.2f} | "
            f"{status} ({row['pct_from_ma']:+.1f}%)"
        )

    mem_parts = []
    for sym, row in data["memory"].items():
        if row.get("return_20d_bps") is None:
            mem_parts.append(f"{sym} n/a")
            continue
        tag = " weakening" if row.get("weakening") else ""
        mem_parts.append(f"{sym} {row['return_20d_bps']:+.0f} bps vs SMH{tag}")
    mem_label = (
        "記憶體轉弱"
        if any(m.get("weakening") for m in data["memory"].values())
        else "記憶體尚可"
    )
    print(f"Memory 20D ({mem_label}): {' | '.join(mem_parts)}")

    guide_parts = []
    cuts = 0
    for sym, row in data["guidance"].items():
        if row.get("cut_risk"):
            cuts += 1
            guide_parts.append(f"{sym}: {', '.join(row['flags'])}")
        else:
            guide_parts.append(f"{sym}: ok")
    guide_label = "指引下修風險" if cuts >= 2 else "指引大致穩定"
    print(f"Guidance proxy ({guide_label}, {cuts} flagged): {' | '.join(guide_parts)}")

    inv_parts = []
    rising = 0
    for sym, row in data["inventory"].items():
        if not row or row.get("qoq_pct") is None:
            inv_parts.append(f"{sym} n/a")
            continue
        if row.get("rising"):
            rising += 1
        streak = " 2Q↑" if row.get("consecutive_rise") else ""
        inv_parts.append(f"{sym} {row['qoq_pct']:+.1f}% QoQ{streak}")
    inv_label = "庫存攀升" if rising >= 3 else "庫存可控"
    print(f"Inventory ({inv_label}): {' | '.join(inv_parts)}")

    print(
        f"SMH risk summary: {data['bearish_count']}/{data['bearish_max']} bearish checks "
        f"(50D MA, memory, guidance, inventory)"
    )


def _notify_telegram(message: str) -> bool:
    """Send alert if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars are set."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url, json={"chat_id": chat_id, "text": message}, timeout=10
        )
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False


def _print_execution_alert(data: dict, enable_telegram: bool = False) -> None:
    """Print banner and optionally push Telegram when credit signal fires."""
    if not data.get("execution_signal"):
        return

    spread = data.get("credit_spread_live_bps")
    velocity = data.get("spread_velocity_bps")
    print(f"\n{SIGNAL_BANNER}")
    print(EXECUTION_ALERT_MSG)
    if spread is not None and velocity is not None:
        print(f"當前即時利差: {spread:.0f} bps | 飆升速度: {velocity:+.0f} bps")
    print(f"{SIGNAL_BANNER}\n")

    if enable_telegram and spread is not None and velocity is not None:
        msg = (
            f"{EXECUTION_ALERT_MSG}\n"
            f"實時利差: {spread:.0f} bps\n"
            f"飆升速度: {velocity:+.0f} bps"
        )
        if _notify_telegram(msg):
            print("Telegram alert sent.")
        else:
            print("Telegram skipped (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).")


def print_prices(telegram: bool = False):
    """Download data, print equities/P/E, macro metrics, and check signals."""
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    prices_map, pe_map, hyg_yield, hist_df = fetch_all_market_data()

    print("--- Equities & Tickers ---")
    for sym in ALL_STOCKS:
        price = prices_map.get(sym)
        suffix = _format_pe_suffix(sym, pe_map)
        if price is not None:
            print(f"{sym}: {price:.2f} USD{suffix}")
        else:
            print(f"{sym}: Price Unavailable{suffix}")

    data = get_rates_credit(prices_map, hyg_yield, hist_df)
    print("\n--- Rates & Credit (Yahoo live, no FRED lag) ---")

    def _f_bps(v, signed=True):
        if v is None:
            return "n/a"
        return f"{v:+.0f} bps" if signed else f"{v:.0f} bps"

    def _f_pct(v):
        return f"{v:.2f}%" if v is not None else "n/a"

    curve_label = "10Y-2Y" if data["two_y_pct"] is not None else "10Y-3M"
    print(
        f"Yield curve ({curve_label}): {_f_bps(data['yield_curve_spread_bps'])}  "
        f"(10Y {_f_pct(data['ten_y_pct'])} | 2Y {_f_pct(data['two_y_pct'])} | 3M {_f_pct(data['three_m_pct'])})"
    )
    print(
        f"IRS swap (3M SOFR implied): {_f_pct(data['sofr_implied_pct'])}  "
        f"spread vs 3M bill: {_f_bps(data['irs_swap_spread_bps'])}"
    )
    print(
        f"Credit level (HYG yield vs 10Y): {_f_bps(data['credit_spread_bps'], signed=False)}  "
        f"(HYG {_f_pct(data['hy_yield_pct'])} vs 10Y {_f_pct(data['ten_y_pct'])})"
    )
    print(
        f"Credit live (HYG vs IEF): {_f_bps(data['credit_spread_live_bps'], signed=False)} | "
        f"{ROLLING_WINDOW}D MA: {_f_bps(data['credit_spread_ma_bps'], signed=False)} | "
        f"velocity: {_f_bps(data['spread_velocity_bps'])} | "
        f"1D HYG-IEF: {_f_bps(data['hyg_vs_ief_1d_bps'])}"
    )
    vix_text = f"{data['vix_level']:.2f}" if data["vix_level"] is not None else "n/a"
    print(
        f"VIX thermometer: {vix_text} | " f"1D change: {_f_bps(data['vix_1d_chg_bps'])}"
    )

    _print_execution_alert(data, enable_telegram=telegram)

    smh_data = get_smh_health()
    _print_smh_health(smh_data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Track selected tickers efficiently.")
    parser.add_argument(
        "--live", nargs="?", const=15, type=int, help="Auto-refresh loop."
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Push execution alert to Telegram (needs TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).",
    )
    args = parser.parse_args()

    try:
        if args.live is None:
            print_prices(telegram=args.telegram)
        else:
            if args.live <= 0:
                raise ValueError("--live interval must be greater than 0 seconds.")
            while True:
                print_prices(telegram=args.telegram)
                print("-" * 50)
                time.sleep(args.live)
    except KeyboardInterrupt:
        print("\nStopped live tracking.")
    except Exception as e:
        print(f"Error: {e}")
