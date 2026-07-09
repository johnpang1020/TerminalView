"""US market dashboard via yfinance + ICI.

Focus: US equities, US rates/credit proxies, US SMH health, US passive structure.
Rates/credit uses Yahoo only (no FRED) to avoid overnight data lag during US session.
Passive share is ICI US domestic equity fund index share — not whole-market passive %.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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
# VanEck SMH approximate index weights for guidance veto (not equal-vote).
SMH_GUIDANCE_WEIGHTS = {
    "NVDA": 0.20,
    "TSM": 0.11,
    "AVGO": 0.08,
    "MU": 0.05,
    "AMD": 0.05,
    "QCOM": 0.05,
}
GUIDANCE_WEIGHT_VETO = 0.15
GUIDANCE_COUNT_VETO = 3
MEMORY_SCISSORS_BPS = -500
INVENTORY_REV_YOY_STOCKPILE_PCT = 30.0
INVENTORY_REV_YOY_STAGNATION_PCT = 10.0
INVENTORY_CYCLE_BEARISH_MIN = 2
# US market structure / passive-flow risk (ICI: US domestic equity funds only).
# Not whole-market passive share — excludes direct stock holdings, hedge funds, etc.
PASSIVE_SHARE_DEFAULT_PCT = 52.0
PASSIVE_SHARE_ELEVATED_PCT = 65.0  # US domestic equity fund index share
PASSIVE_SHARE_DANGER_PCT = 75.0  # fund-industry scale (not whole-market 80%)
PASSIVE_CACHE_PATH = Path(__file__).with_name(".passive_share_cache.json")
PASSIVE_CACHE_MAX_AGE_DAYS = 7
ICI_PASSIVE_LOOKBACK_MONTHS = 8
TOP10_HISTORICAL_PCT = 20.0
TOP10_ELEVATED_PCT = 30.0
TOP10_DANGER_PCT = 35.0
SPY_RSP_SPREAD_DANGER_BPS = 300
STRUCTURE_FUND = "VOO"
STRUCTURE_RATIO_TICKERS = ("SPY", "RSP")
# Hard-data modules (no narrative): corporate actions, PE percentile, memory scissors.
HARD_DATA_TICKER = "SMH"
PE_HISTORY_PATH = Path(__file__).with_name("smh_pe_history.csv")
PE_SAFE_CEILING = 25.0
PE_SAFE_PERCENTILE = 50.0
CORP_ACTIONS_LOOKBACK_DAYS = 400
DEFAULT_JSON_EXPORT = Path(__file__).with_name("hard_data_export.json")


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


def _latest_quarter_revenue_yoy(ticker_obj):
    """Latest quarter revenue vs same quarter one year ago (%)."""
    try:
        fin = ticker_obj.quarterly_financials
        if fin is None or fin.empty or "Total Revenue" not in fin.index:
            return None
        rev = fin.loc["Total Revenue"].dropna().sort_index(ascending=False)
        if len(rev) < 5 or not float(rev.iloc[4]):
            return None
        return (float(rev.iloc[0]) / float(rev.iloc[4]) - 1) * 100
    except Exception:
        return None


def _inventory_status(ticker_obj):
    """QoQ inventory with revenue-YoY valve (stockpile vs cycle stagnation)."""
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

        rev_yoy = _latest_quarter_revenue_yoy(ticker_obj)
        rising = qoq_pct is not None and qoq_pct > 0
        if rising and rev_yoy is not None and rev_yoy > INVENTORY_REV_YOY_STOCKPILE_PCT:
            status = "stockpile"
            cycle_bearish = False
        elif (
            rising
            and rev_yoy is not None
            and rev_yoy < INVENTORY_REV_YOY_STAGNATION_PCT
        ):
            status = "cycle_stagnation"
            cycle_bearish = True
        elif rising:
            status = "rising_neutral"
            cycle_bearish = False
        else:
            status = "stable"
            cycle_bearish = False

        return {
            "qoq_pct": qoq_pct,
            "rising": rising,
            "consecutive_rise": consecutive_rise,
            "rev_yoy_pct": rev_yoy,
            "status": status,
            "cycle_bearish": cycle_bearish,
        }
    except Exception:
        return None


def _evaluate_guidance_sector(guidance):
    """Weight veto: weak only if flagged weight >= 15% or >= 3 names flagged."""
    flagged = [sym for sym, row in guidance.items() if row.get("cut_risk")]
    flagged_weight = sum(SMH_GUIDANCE_WEIGHTS.get(sym, 0.05) for sym in flagged)
    sector_weak = (
        flagged_weight >= GUIDANCE_WEIGHT_VETO or len(flagged) >= GUIDANCE_COUNT_VETO
    )
    return {
        "sector_weak": sector_weak,
        "flagged_count": len(flagged),
        "flagged_weight_pct": flagged_weight * 100,
        "flagged_symbols": flagged,
    }


def _evaluate_memory_scissors(memory):
    """Bearish only when BOTH MU and SK Hynix lag SMH by > 500 bps."""
    mu_rel = (memory.get("MU") or {}).get("rel_vs_smh_20d_bps")
    hynix_rel = (memory.get("000660.KS") or {}).get("rel_vs_smh_20d_bps")
    if mu_rel is None or hynix_rel is None:
        return {
            "status": "unknown",
            "bearish": False,
            "mu_rel": mu_rel,
            "hynix_rel": hynix_rel,
            "trigger_bearish": False,
        }
    bearish = mu_rel < MEMORY_SCISSORS_BPS and hynix_rel < MEMORY_SCISSORS_BPS
    status = "bearish" if bearish else "safe"
    return {
        "status": status,
        "bearish": bearish,
        "trigger_bearish": bearish,
        "mu_rel": mu_rel,
        "hynix_rel": hynix_rel,
        "threshold_bps": MEMORY_SCISSORS_BPS,
        "window": 20,
    }


def get_corporate_actions_adjustment(
    ticker_symbol=HARD_DATA_TICKER,
    start_date=None,
    end_date=None,
    entry_price=None,
):
    """
    US ETF/stock corporate-actions factor for entry-price rebase.

    Returns cumulative adjustment so:
        new_entry = old_entry * adjustment_factor
    Splits and dividends in [start_date, end_date] are applied in calendar order.
    """
    end = pd.Timestamp(end_date or datetime.now().date())
    start = pd.Timestamp(
        start_date or (end - timedelta(days=CORP_ACTIONS_LOOKBACK_DAYS)).date()
    )
    ticker = yf.Ticker(ticker_symbol)
    actions = ticker.actions
    if actions is None or actions.empty:
        return {
            "ticker": ticker_symbol,
            "start_date": str(start.date()),
            "end_date": str(end.date()),
            "adjustment_factor": 1.0,
            "dividends": [],
            "splits": [],
            "new_entry": float(entry_price) if entry_price is not None else None,
            "source": "yfinance.actions",
        }

    idx = actions.index
    if getattr(idx, "tz", None) is not None:
        actions = actions.copy()
        actions.index = idx.tz_localize(None)
    window = actions.loc[
        (actions.index >= pd.Timestamp(start)) & (actions.index <= pd.Timestamp(end))
    ]

    hist = ticker.history(start=start - timedelta(days=5), end=end + timedelta(days=5))
    if hist is not None and not hist.empty and getattr(hist.index, "tz", None):
        hist = hist.copy()
        hist.index = hist.index.tz_localize(None)

    factor = 1.0
    dividends = []
    splits = []
    for ts, row in window.sort_index().iterrows():
        split = float(row.get("Stock Splits") or 0.0)
        div = float(row.get("Dividends") or 0.0)
        day = ts.normalize() if hasattr(ts, "normalize") else pd.Timestamp(ts)

        if split and split > 0:
            # N-for-1 split: price scale shrinks by 1/N
            factor *= 1.0 / split
            splits.append({"date": str(day.date()), "ratio": split})

        if div and div > 0:
            close = None
            if hist is not None and not hist.empty:
                # Prefer prior close for ex-div factor.
                prior = hist.loc[hist.index < day, "Close"]
                if not prior.empty:
                    close = float(prior.iloc[-1])
            if close and close > 0:
                day_factor = (close - div) / close
                factor *= day_factor
                dividends.append(
                    {
                        "date": str(day.date()),
                        "dividend": div,
                        "prior_close": close,
                        "day_factor": day_factor,
                    }
                )
            else:
                dividends.append(
                    {
                        "date": str(day.date()),
                        "dividend": div,
                        "prior_close": None,
                        "day_factor": None,
                    }
                )

    new_entry = None
    if entry_price is not None:
        new_entry = float(entry_price) * factor

    return {
        "ticker": ticker_symbol,
        "start_date": str(pd.Timestamp(start).date()),
        "end_date": str(pd.Timestamp(end).date()),
        "adjustment_factor": float(factor),
        "dividends": dividends,
        "splits": splits,
        "new_entry": new_entry,
        "source": "yfinance.actions",
    }


def _append_pe_history(ticker_symbol, pe_value, price=None):
    """Append one daily PE observation (idempotent per calendar date)."""
    if pe_value is None:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    row = {
        "date": today,
        "ticker": ticker_symbol,
        "trailing_pe": float(pe_value),
        "price": float(price) if price is not None else None,
    }
    if PE_HISTORY_PATH.exists():
        df = pd.read_csv(PE_HISTORY_PATH)
        df = df[df["date"] != today]
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])
    df.to_csv(PE_HISTORY_PATH, index=False)


def calculate_pe_percentile(
    ticker_symbol=HARD_DATA_TICKER,
    pe_ceiling=PE_SAFE_CEILING,
    history_path=None,
):
    """
    US SMH valuation lock inputs.

    Yahoo SMH has trailingPE, not forwardPE — field is labeled trailing_pe.
    Percentile uses local accumulated history (smh_pe_history.csv), not a fantasy 5y API.
    """
    path = Path(history_path) if history_path else PE_HISTORY_PATH
    info = yf.Ticker(ticker_symbol).info
    live_pe = _parse_trailing_pe(info.get("trailingPE"))
    live_forward = _parse_trailing_pe(info.get("forwardPE"))
    price = info.get("regularMarketPrice") or info.get("previousClose")
    _append_pe_history(ticker_symbol, live_pe, price)

    history_count = 0
    percentile = None
    if path.exists() and live_pe is not None:
        df = pd.read_csv(path)
        series = df["trailing_pe"].dropna().astype(float)
        history_count = int(len(series))
        if history_count > 0:
            percentile = float((series < live_pe).sum() / history_count * 100)

    is_valuation_safe = False
    if live_pe is not None:
        below_ceiling = live_pe <= pe_ceiling
        below_median = percentile is not None and percentile <= PE_SAFE_PERCENTILE
        # Rigid lock: must clear absolute ceiling; percentile used when history exists.
        is_valuation_safe = bool(
            below_ceiling and (percentile is None or below_median or history_count < 20)
        )
        if history_count >= 20:
            is_valuation_safe = bool(below_ceiling and below_median)

    return {
        "ticker": ticker_symbol,
        "trailing_pe": live_pe,
        "forward_pe": live_forward,
        "pe_field_used": "trailing_pe",
        "percentile": percentile,
        "history_count": history_count,
        "history_path": str(path),
        "pe_ceiling": pe_ceiling,
        "is_valuation_safe": is_valuation_safe,
        "note": "SMH Yahoo forwardPE usually null; percentile grows with local CSV history.",
    }


def calculate_memory_scissors(window=20):
    """MU and SK Hynix 20D relative return vs SMH in bps (US/KR listed proxies)."""
    symbols = ["MU", "000660.KS", "SMH"]
    hist_df = yf.download(
        symbols, period="3mo", group_by="ticker", progress=False, auto_adjust=True
    )
    out = {}
    smh = _price_series(hist_df, "SMH", use_adj=False)
    smh_ret = None
    if smh is not None and len(smh) > window:
        smh_ret = float(smh.iloc[-1] / smh.iloc[-1 - window] - 1)

    for sym in ("MU", "000660.KS"):
        series = _price_series(hist_df, sym, use_adj=False)
        if series is None or len(series) <= window or smh_ret is None:
            out[sym] = None
            continue
        ret = float(series.iloc[-1] / series.iloc[-1 - window] - 1)
        out[sym] = (ret - smh_ret) * 10000

    mu_bps = out.get("MU")
    hynix_bps = out.get("000660.KS")
    trigger = (
        mu_bps is not None
        and hynix_bps is not None
        and mu_bps < MEMORY_SCISSORS_BPS
        and hynix_bps < MEMORY_SCISSORS_BPS
    )
    return {
        "window": window,
        "mu_vs_smh_bps": mu_bps,
        "hynix_vs_smh_bps": hynix_bps,
        "threshold_bps": MEMORY_SCISSORS_BPS,
        "trigger_bearish": bool(trigger),
        "source": "yfinance auto_adjust Close",
    }



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
        }
    memory_scissors = _evaluate_memory_scissors(memory)

    guidance = {}
    for sym in SMH_GUIDANCE_TICKERS:
        flags = _guidance_flags(tickers_batch.tickers[sym])
        guidance[sym] = {
            "flags": flags,
            "cut_risk": bool(flags),
            "weight_pct": SMH_GUIDANCE_WEIGHTS.get(sym, 0.05) * 100,
        }
    guidance_sector = _evaluate_guidance_sector(guidance)

    inventory = {}
    for sym in SMH_INVENTORY_TICKERS:
        inventory[sym] = _inventory_status(tickers_batch.tickers[sym])

    cycle_stagnation_count = sum(
        1 for inv in inventory.values() if inv and inv.get("cycle_bearish")
    )
    inventory_bearish = cycle_stagnation_count >= INVENTORY_CYCLE_BEARISH_MIN

    bearish = 0
    if (dma.get("SMH") or {}).get("below_ma50"):
        bearish += 1
    if (dma.get("^SOX") or {}).get("below_ma50"):
        bearish += 1
    if memory_scissors.get("bearish"):
        bearish += 1
    if guidance_sector.get("sector_weak"):
        bearish += 1
    if inventory_bearish:
        bearish += 1

    return {
        "dma": dma,
        "memory": memory,
        "memory_scissors": memory_scissors,
        "guidance": guidance,
        "guidance_sector": guidance_sector,
        "inventory": inventory,
        "inventory_bearish": inventory_bearish,
        "cycle_stagnation_count": cycle_stagnation_count,
        "bearish_count": bearish,
        "bearish_max": 5,
    }


def _print_smh_health(data):
    print("\n--- US SMH / Semiconductor Health ---")
    print(
        "Note: US-listed proxies (SMH/^SOX/MU + KR Hynix). "
        "Guidance/inventory are lagged proxies — not live DRAM spot or official guidance cuts."
    )

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
        rel = row.get("rel_vs_smh_20d_bps")
        if rel is None:
            mem_parts.append(f"{sym} n/a")
        else:
            mem_parts.append(f"{sym} {rel:+.0f} bps vs SMH")
    scissors = data.get("memory_scissors") or {}
    mem_status = scissors.get("status", "unknown")
    if mem_status == "bearish":
        mem_label = "記憶體確立轉弱"
    elif mem_status == "safe":
        mem_label = "記憶體結構健康（台美剪刀差）"
    else:
        mem_label = "記憶體資料不足"
    print(f"Memory 20D ({mem_label}): {' | '.join(mem_parts)}")

    guide_parts = []
    for sym, row in data["guidance"].items():
        wt = row.get("weight_pct", 0)
        if row.get("cut_risk"):
            guide_parts.append(f"{sym}({wt:.0f}%w): {', '.join(row['flags'])}")
        else:
            guide_parts.append(f"{sym}({wt:.0f}%w): ok")
    sector = data.get("guidance_sector") or {}
    flagged_n = sector.get("flagged_count", 0)
    flagged_w = sector.get("flagged_weight_pct", 0)
    if sector.get("sector_weak"):
        guide_label = (
            f"板塊指引轉弱（flagged {flagged_n} 家 / {flagged_w:.0f}% SMH 權重）"
        )
    else:
        guide_label = f"板塊指引穩定（{flagged_n} 家 flagged / {flagged_w:.0f}% 權重，未達 15% 或 3 家門檻）"
    print(f"Guidance proxy ({guide_label}): {' | '.join(guide_parts)}")

    inv_parts = []
    stockpile = cycle = 0
    status_labels = {
        "stockpile": "戰備囤貨",
        "cycle_stagnation": "週期滯銷",
        "rising_neutral": "庫存升/營收中性",
        "stable": "穩定",
    }
    for sym, row in data["inventory"].items():
        if not row or row.get("qoq_pct") is None:
            inv_parts.append(f"{sym} n/a")
            continue
        label = status_labels.get(row.get("status"), row.get("status"))
        rev = row.get("rev_yoy_pct")
        rev_txt = f" rev YoY {rev:+.0f}%" if rev is not None else ""
        streak = " 2Q↑" if row.get("consecutive_rise") else ""
        inv_parts.append(f"{sym} {row['qoq_pct']:+.1f}% QoQ{rev_txt} [{label}]{streak}")
        if row.get("status") == "stockpile":
            stockpile += 1
        elif row.get("cycle_bearish"):
            cycle += 1
    if data.get("inventory_bearish"):
        inv_label = f"週期性滯銷 ({cycle} 家)"
    elif stockpile >= 2:
        inv_label = f"AI 戰備囤貨為主 ({stockpile} 家)"
    else:
        inv_label = "庫存可控"
    print(f"Inventory ({inv_label}): {' | '.join(inv_parts)}")

    print(
        f"US SMH risk summary: {data['bearish_count']}/{data['bearish_max']} bearish checks "
        f"(50D MA, memory scissors, guidance weight veto, inventory cycle)"
    )
    print(
        "Caveat: US semiconductor dashboard only — not a standalone SMH entry signal."
    )


def _month_slugs(lookback=ICI_PASSIVE_LOOKBACK_MONTHS):
    """Yield ICI combined_active_index_MMYY slugs from newest month backward."""
    now = datetime.now()
    year, month = now.year, now.month
    for _ in range(lookback):
        yield f"{month:02d}{year % 100:02d}"
        month -= 1
        if month == 0:
            month = 12
            year -= 1


def _parse_ici_domestic_equity_passive_pct(html: str):
    """Parse ICI Domestic equity Index-as-%-of-Total from release HTML."""
    match = re.search(
        r"Domestic equity</p>\s*</td>\s*"
        r"<td>([\d,]+\.\d)</td>\s*"
        r"<td>([\d,]+\.\d)</td>\s*"
        r"<td>(\d+\.\d)</td>",
        html,
        re.I,
    )
    if not match:
        return None
    active = float(match.group(1).replace(",", ""))
    index = float(match.group(2).replace(",", ""))
    pct = float(match.group(3))
    if active <= 0 or index <= 0 or not (0 < pct < 100):
        return None
    return pct


def _load_passive_cache():
    try:
        if not PASSIVE_CACHE_PATH.exists():
            return None
        data = json.loads(PASSIVE_CACHE_PATH.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(data["fetched_at"])
        if datetime.now() - fetched > timedelta(days=PASSIVE_CACHE_MAX_AGE_DAYS):
            return None
        return data
    except Exception:
        return None


def _save_passive_cache(payload: dict):
    try:
        PASSIVE_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _fetch_ici_passive_share():
    """Auto-fetch latest ICI US domestic equity index share (% of equity fund AUM)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; stock.py passive-share bot; +local)"
        )
    }
    for slug in _month_slugs():
        url = f"https://www.ici.org/research/stats/combined_active_index_{slug}"
        try:
            resp = requests.get(url, headers=headers, timeout=20)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        pct = _parse_ici_domestic_equity_passive_pct(resp.text)
        if pct is None:
            continue
        title = re.search(
            r"Active and Index Investing,\s*([A-Za-z]+ \d{4})", resp.text
        )
        period = title.group(1) if title else slug
        payload = {
            "passive_pct": pct,
            "source": "ICI",
            "period": period,
            "url": url,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
        _save_passive_cache(payload)
        return payload
    return None


def _passive_share_info():
    """
    Resolve passive US equity share with priority:
    1) PASSIVE_US_EQUITY_SHARE_PCT env override
    2) fresh local cache / live ICI scrape
    3) hardcoded fallback baseline
    """
    raw = os.environ.get("PASSIVE_US_EQUITY_SHARE_PCT")
    if raw is not None:
        try:
            return {
                "passive_pct": float(raw),
                "source": "env",
                "period": "manual",
                "url": None,
                "manual": False,
            }
        except ValueError:
            pass

    cached = _load_passive_cache()
    if cached and cached.get("passive_pct") is not None:
        return {
            "passive_pct": float(cached["passive_pct"]),
            "source": cached.get("source", "ICI-cache"),
            "period": cached.get("period"),
            "url": cached.get("url"),
            "manual": False,
        }

    live = _fetch_ici_passive_share()
    if live:
        return {
            "passive_pct": float(live["passive_pct"]),
            "source": live.get("source", "ICI"),
            "period": live.get("period"),
            "url": live.get("url"),
            "manual": False,
        }

    return {
        "passive_pct": PASSIVE_SHARE_DEFAULT_PCT,
        "source": "fallback",
        "period": "stale-default",
        "url": None,
        "manual": True,
    }


def _top10_concentration(fund_symbol):
    """Sum of top-10 holdings weight for a US large-cap index ETF."""
    try:
        holdings = yf.Ticker(fund_symbol).funds_data.top_holdings
        if holdings is None or holdings.empty:
            return None
        top10 = holdings.head(10)
        weight_col = "Holding Percent"
        if weight_col not in top10.columns:
            return None
        total_pct = float(top10[weight_col].sum()) * 100
        names = [
            f"{idx} {float(row[weight_col]) * 100:.1f}%"
            for idx, row in top10.iterrows()
        ]
        return {
            "fund": fund_symbol,
            "top10_weight_pct": total_pct,
            "top10_names": names,
        }
    except Exception:
        return None


def _spy_rsp_structure():
    """Cap-weight (SPY) vs equal-weight (RSP) relative performance."""
    try:
        hist_df = yf.download(
            list(STRUCTURE_RATIO_TICKERS),
            period="6mo",
            group_by="ticker",
            progress=False,
        )
        spy = _price_series(hist_df, "SPY", use_adj=True)
        rsp = _price_series(hist_df, "RSP", use_adj=True)
        if spy is None or rsp is None or len(spy) < 61 or len(rsp) < 61:
            return None

        ratio = spy / rsp
        spreads = {}
        for label, days in (("20D", 20), ("60D", 60)):
            if len(spy) <= days:
                continue
            spy_ret = (spy.iloc[-1] / spy.iloc[-1 - days] - 1) * 10000
            rsp_ret = (rsp.iloc[-1] / rsp.iloc[-1 - days] - 1) * 10000
            spreads[label] = {
                "spy_bps": spy_ret,
                "rsp_bps": rsp_ret,
                "spread_bps": spy_ret - rsp_ret,
            }

        ratio_20d_bps = (
            (ratio.iloc[-1] / ratio.iloc[-21] - 1) * 10000 if len(ratio) > 21 else None
        )
        spread_20d = (spreads.get("20D") or {}).get("spread_bps")
        mega_cap_bubble = spread_20d is not None and spread_20d > SPY_RSP_SPREAD_DANGER_BPS
        healthy_breadth = spread_20d is not None and spread_20d < 0

        return {
            "ratio": float(ratio.iloc[-1]),
            "ratio_20d_bps": ratio_20d_bps,
            "spreads": spreads,
            "mega_cap_bubble": mega_cap_bubble,
            "healthy_breadth": healthy_breadth,
        }
    except Exception:
        return None


def get_market_structure():
    """Passive share, S&P top-10 concentration, SPY vs RSP breadth."""
    passive_info = _passive_share_info()
    passive_pct = passive_info["passive_pct"]
    if passive_pct >= PASSIVE_SHARE_DANGER_PCT:
        passive_status = "danger"
    elif passive_pct >= PASSIVE_SHARE_ELEVATED_PCT:
        passive_status = "elevated"
    else:
        passive_status = "ok"

    top10 = _top10_concentration(STRUCTURE_FUND)
    top10_pct = (top10 or {}).get("top10_weight_pct")
    if top10_pct is None:
        top10_status = "unknown"
    elif top10_pct >= TOP10_DANGER_PCT:
        top10_status = "danger"
    elif top10_pct >= TOP10_ELEVATED_PCT:
        top10_status = "elevated"
    elif top10_pct <= TOP10_HISTORICAL_PCT * 1.15:
        top10_status = "historical"
    else:
        top10_status = "above_historical"

    spy_rsp = _spy_rsp_structure()
    if not spy_rsp:
        breadth_status = "unknown"
    elif spy_rsp.get("mega_cap_bubble"):
        breadth_status = "mega_cap_bubble"
    elif spy_rsp.get("healthy_breadth"):
        breadth_status = "healthy_breadth"
    else:
        breadth_status = "neutral"

    risk_flags = sum(
        1
        for s in (passive_status, top10_status, breadth_status)
        if s in ("danger", "mega_cap_bubble", "elevated")
    )

    return {
        "passive_pct": passive_pct,
        "passive_status": passive_status,
        "passive_source": passive_info.get("source"),
        "passive_period": passive_info.get("period"),
        "passive_url": passive_info.get("url"),
        "passive_manual": bool(passive_info.get("manual")),
        "top10": top10,
        "top10_status": top10_status,
        "spy_rsp": spy_rsp,
        "breadth_status": breadth_status,
        "risk_flags": risk_flags,
    }


def _print_market_structure(data):
    print("\n--- US Market Structure / Passive Risk ---")
    print(
        "Scope: US only — ICI domestic equity funds, VOO (S&P 500), SPY vs RSP. "
        "Not whole-market passive share."
    )

    passive = data["passive_pct"]
    p_status = data["passive_status"]
    if p_status == "danger":
        p_label = "US 基金業內指數佔比過高"
    elif p_status == "elevated":
        p_label = "US 基金業內指數佔比偏高"
    else:
        p_label = "US 基金業內指數佔比尚可"
    source = data.get("passive_source") or "unknown"
    period = data.get("passive_period") or "n/a"
    if data.get("passive_manual"):
        src_tag = f" [fallback {PASSIVE_SHARE_DEFAULT_PCT:.0f}% — ICI fetch failed]"
    else:
        src_tag = f" [auto: {source} {period}]"
    print(
        f"US passive share (ICI domestic equity funds): {passive:.1f}% | {p_label}{src_tag} "
        f"(elevated >{PASSIVE_SHARE_ELEVATED_PCT:.0f}%; danger >{PASSIVE_SHARE_DANGER_PCT:.0f}%; "
        f"excludes direct stock / hedge funds)"
    )

    top10 = data.get("top10")
    if top10:
        t_pct = top10["top10_weight_pct"]
        t_status = data["top10_status"]
        if t_status == "danger":
            t_label = "US S&P 前十大集中度歷史高位"
        elif t_status == "elevated":
            t_label = "US S&P 前十大集中度偏高"
        elif t_status == "historical":
            t_label = "US S&P 前十大接近歷史均值"
        else:
            t_label = "US S&P 前十大高於歷史均值"
        print(
            f"US {top10['fund']} top-10 weight: {t_pct:.1f}% | {t_label} "
            f"(hist ~{TOP10_HISTORICAL_PCT:.0f}%; danger >{TOP10_DANGER_PCT:.0f}%)"
        )
        print(f"  Largest: {', '.join(top10['top10_names'][:5])}")
    else:
        print(f"US {STRUCTURE_FUND} top-10 weight: n/a")

    spy_rsp = data.get("spy_rsp")
    if spy_rsp:
        s20 = spy_rsp["spreads"].get("20D", {})
        s60 = spy_rsp["spreads"].get("60D", {})
        b_status = data["breadth_status"]
        if b_status == "mega_cap_bubble":
            b_label = "US SPY 遠強於 RSP — mega-cap 集中"
        elif b_status == "healthy_breadth":
            b_label = "US RSP 領先或持平 — 廣度相對健康"
        else:
            b_label = "US SPY/RSP 價差中性"
        print(
            f"US SPY vs RSP (cap vs equal weight): ratio {spy_rsp['ratio']:.3f} | "
            f"{b_label}"
        )
        if s20:
            print(
                f"  20D: SPY {s20['spy_bps']:+.0f} bps | RSP {s20['rsp_bps']:+.0f} bps | "
                f"spread {s20['spread_bps']:+.0f} bps"
            )
        if s60:
            print(
                f"  60D: SPY {s60['spy_bps']:+.0f} bps | RSP {s60['rsp_bps']:+.0f} bps | "
                f"spread {s60['spread_bps']:+.0f} bps"
            )
    else:
        print("US SPY vs RSP: n/a")

    print(
        f"US structure risk flags: {data['risk_flags']}/3 elevated "
        f"(ICI fund passive share, VOO top-10, SPY/RSP breadth)"
    )
    print(
        "Caveat: dashboard only — not a single buy/sell trigger; "
        "high concentration can persist in US mega-cap regimes."
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


def build_hard_data_export(entry_price=None, start_date=None):
    """Pure numeric payload for monthly confluence / quarterly safety-valve checks."""
    corp = get_corporate_actions_adjustment(
        HARD_DATA_TICKER, start_date=start_date, entry_price=entry_price
    )
    pe = calculate_pe_percentile(HARD_DATA_TICKER)
    scissors = calculate_memory_scissors(window=20)
    return {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "market": "US",
        "modules": {
            "corporate_actions": corp,
            "pe_percentile": pe,
            "memory_scissors": scissors,
        },
        "gates": {
            "valuation_safe": pe.get("is_valuation_safe"),
            "memory_bearish_veto": scissors.get("trigger_bearish"),
            "entry_adjustment_factor": corp.get("adjustment_factor"),
        },
        "disclaimer": (
            "Hard numbers only. Not a trade instruction. "
            "SMH PE uses trailingPE (Yahoo forwardPE usually null). "
            "Memory scissors are equity proxies, not DRAM spot."
        ),
    }


def export_hard_data_json(path=None, entry_price=None, start_date=None):
    """Write hard-data JSON for chat/system paste without narrative."""
    out_path = Path(path) if path else DEFAULT_JSON_EXPORT
    payload = build_hard_data_export(entry_price=entry_price, start_date=start_date)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_path, payload


def _print_hard_data(payload):
    print("\n--- US Hard Data Modules (no narrative) ---")
    corp = payload["modules"]["corporate_actions"]
    pe = payload["modules"]["pe_percentile"]
    scissors = payload["modules"]["memory_scissors"]
    print(
        f"Corporate actions ({corp['ticker']} {corp['start_date']}→{corp['end_date']}): "
        f"factor={corp['adjustment_factor']:.6f} | "
        f"divs={len(corp['dividends'])} splits={len(corp['splits'])}"
    )
    if corp.get("new_entry") is not None:
        print(f"  Rebased entry: {corp['new_entry']:.4f}")
    pe_txt = f"{pe['trailing_pe']:.2f}" if pe.get("trailing_pe") is not None else "n/a"
    pct_txt = f"{pe['percentile']:.1f}" if pe.get("percentile") is not None else "n/a"
    print(
        f"SMH PE lock: trailingPE={pe_txt} | percentile={pct_txt} "
        f"(n={pe['history_count']}) | is_valuation_safe={pe['is_valuation_safe']} "
        f"| ceiling={pe['pe_ceiling']}"
    )
    mu = scissors.get("mu_vs_smh_bps")
    hx = scissors.get("hynix_vs_smh_bps")
    mu_txt = f"{mu:+.0f}" if mu is not None else "n/a"
    hx_txt = f"{hx:+.0f}" if hx is not None else "n/a"
    print(
        f"Memory scissors ({scissors['window']}D): MU {mu_txt} bps | "
        f"000660.KS {hx_txt} bps | trigger_bearish={scissors['trigger_bearish']} "
        f"(both < {scissors['threshold_bps']} bps)"
    )
    gates = payload["gates"]
    print(
        f"Gates: valuation_safe={gates['valuation_safe']} | "
        f"memory_bearish_veto={gates['memory_bearish_veto']} | "
        f"entry_adjustment_factor={gates['entry_adjustment_factor']:.6f}"
    )


def print_prices(telegram: bool = False, export_json=None, entry_price=None, hard_data=False):
    """Print equities, US rates/credit, SMH health, market structure.

    Default: dashboard only — does NOT write smh_pe_history.csv or JSON.
    Pass hard_data=True and/or export_json to run hard-data modules.
    """
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
    print("\n--- US Rates & Credit (Yahoo live proxies, not FRED) ---")
    print(
        "Note: US Treasury/SOFR/HYG-IEF proxies only — not official OAS or cash IRS curve."
    )

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

    structure_data = get_market_structure()
    _print_market_structure(structure_data)

    # Optional: PE history + JSON hard-data modules (off by default).
    if hard_data or export_json is not None:
        hard = build_hard_data_export(entry_price=entry_price)
        _print_hard_data(hard)
        if export_json is not None:
            out_path = Path(export_json) if export_json else DEFAULT_JSON_EXPORT
            if export_json is True or export_json == "":
                out_path = DEFAULT_JSON_EXPORT
            out_path.write_text(
                json.dumps(hard, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\nHard data JSON written: {out_path}")


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
    parser.add_argument(
        "--hard-data",
        action="store_true",
        help="Also run hard-data modules (writes smh_pe_history.csv; prints gates).",
    )
    parser.add_argument(
        "--export-json",
        nargs="?",
        const=str(DEFAULT_JSON_EXPORT),
        default=None,
        metavar="PATH",
        help="Export hard-data JSON (implies --hard-data; default: hard_data_export.json).",
    )
    parser.add_argument(
        "--entry-price",
        type=float,
        default=None,
        help="Optional SMH entry price to rebase with corporate-actions factor.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print/export hard-data JSON only (skip full dashboard).",
    )
    args = parser.parse_args()

    try:
        if args.json_only:
            path, payload = export_hard_data_json(
                path=args.export_json or DEFAULT_JSON_EXPORT,
                entry_price=args.entry_price,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print(f"\nWrote {path}")
        elif args.live is None:
            print_prices(
                telegram=args.telegram,
                export_json=args.export_json,
                entry_price=args.entry_price,
                hard_data=args.hard_data,
            )
        else:
            if args.live <= 0:
                raise ValueError("--live interval must be greater than 0 seconds.")
            while True:
                print_prices(
                    telegram=args.telegram,
                    export_json=args.export_json,
                    entry_price=args.entry_price,
                    hard_data=args.hard_data,
                )
                print("-" * 50)
                time.sleep(args.live)
    except KeyboardInterrupt:
        print("\nStopped live tracking.")
    except Exception as e:
        print(f"Error: {e}")
