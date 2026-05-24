"""
benchmark.py
模块一：全局生命周期与真实基准重构
- 法定主锚 Mkt_Chg = 0.7 × R_TMT + 0.3 × R_deposit_daily
- 辅助四核 R_Aux = w_AIC·R_AIC + w_CE·R_CE + w_NE·R_NE + w_SEMI·R_SEMI
- 连续复利基准净值还原 NAV_benchmark_pure
"""

import numpy as np
import pandas as pd


def compute_mkt_chg(tmt_change_pct: float, cfg: dict) -> float:
    """
    法定主锚公式：Mkt_Chg = 0.7 × R_TMT + 0.3 × R_deposit_daily
    tmt_change_pct: TMT 指数当日涨跌幅（%）
    """
    bm = cfg.get("benchmark", {})
    equity_w = bm.get("equity_weight", 0.70)
    cash_w = bm.get("cash_weight", 0.30)
    r_deposit = bm.get("deposit_daily_rate", 0.00004)

    mkt_chg = equity_w * tmt_change_pct + cash_w * (r_deposit * 100)
    return mkt_chg


def compute_aux(aic_chg: float, ce_chg: float, semi_chg: float, ne_chg: float,
                cfg: dict) -> float:
    """
    辅助四核暴露监控：
    R_Aux = w_AIC·R_AIC + w_CE·R_CE + w_NE·R_NE + w_SEMI·R_SEMI
    各参数为对应指数当日涨跌幅（%）
    """
    aw = cfg.get("auxiliary_weights", {})
    w_aic = aw.get("w_aic", 0.60)
    w_ce = aw.get("w_ce", 0.15)
    w_semi = aw.get("w_semi", 0.15)
    w_ne = aw.get("w_ne", 0.10)

    r_aux = (w_aic * aic_chg + w_ce * ce_chg +
             w_semi * semi_chg + w_ne * ne_chg)
    return r_aux


def compute_returns(df: pd.DataFrame) -> pd.DataFrame:
    """
    为 DataFrame 添加各指数收益率列（%）和基金收益率列（%）。
    基于收盘价计算日收益率，第一行为 NaN。
    """
    df = df.copy()
    # TMT 指数收益率（%）
    df["R_TMT"] = df["tmt_close"].pct_change(fill_method=None) * 100
    # 辅助四核收益率（%）
    df["R_AIC"] = df["aic_close"].pct_change(fill_method=None) * 100
    df["R_CE"] = df["ce_close"].pct_change(fill_method=None) * 100
    df["R_SEMI"] = df["semi_close"].pct_change(fill_method=None) * 100
    df["R_NE"] = df["ne_close"].pct_change(fill_method=None) * 100
    # 基金收益率（%）
    df["R_fund"] = df["fund_nav"].pct_change(fill_method=None) * 100
    return df


def compute_mkt_chg_series(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """计算整个序列的 Mkt_Chg"""
    bm = cfg.get("benchmark", {})
    equity_w = bm.get("equity_weight", 0.70)
    cash_w = bm.get("cash_weight", 0.30)
    r_deposit = bm.get("deposit_daily_rate", 0.00004)

    return equity_w * df["R_TMT"] + cash_w * (r_deposit * 100)


def compute_aux_series(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """计算整个序列的 R_Aux"""
    aw = cfg.get("auxiliary_weights", {})
    return (aw.get("w_aic", 0.60) * df["R_AIC"] +
            aw.get("w_ce", 0.15) * df["R_CE"] +
            aw.get("w_semi", 0.15) * df["R_SEMI"] +
            aw.get("w_ne", 0.10) * df["R_NE"])


def compute_benchmark_nav(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """
    连续复利基准净值还原：
    NAV_benchmark_pure(t) = NAV_benchmark_pure(0) × (
        0.7 × Idx_TMT(t)/Idx_TMT(0) + 0.3 × (1 + r_deposit_daily × t)
    )
    返回归一化基准净值序列（起始值=1.0）。
    """
    bm = cfg.get("benchmark", {})
    equity_w = bm.get("equity_weight", 0.70)
    cash_w = bm.get("cash_weight", 0.30)
    r_deposit = bm.get("deposit_daily_rate", 0.00004)

    tmt_idx = df["tmt_close"].values
    tmt_ratio = tmt_idx / tmt_idx[0]  # Idx_TMT(t) / Idx_TMT(0)

    t = np.arange(len(df))
    cash_ratio = 1 + r_deposit * t

    nav_benchmark = equity_w * tmt_ratio + cash_w * cash_ratio
    return pd.Series(nav_benchmark, index=df.index)


def compute_excess_nav(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    连续复利超额还原：
    Excess_NAV(t) = NAV_fund(t) × e^(fee_drag_daily × t) / NAV_benchmark_pure(t)
    Excess_DD(t)  = Excess_NAV(t) / max(Excess_NAV(s≤t)) - 1
    """
    df = df.copy()
    bm = cfg.get("benchmark", {})
    fee_drag = bm.get("fee_drag_daily", 0.000038)

    nav_benchmark = compute_benchmark_nav(df, cfg)

    t = np.arange(len(df))
    fee_factor = np.exp(fee_drag * t)

    # 基金净值归一化（起始值=1.0）
    # 注：prepare_data 已确保截断后首个 nav 有效，此处仅兜底除零
    nav0 = df["fund_nav"].values[0]
    if nav0 == 0:
        nav0 = 1.0
    fund_nav_norm = df["fund_nav"].values / nav0

    df["Excess_NAV"] = fund_nav_norm * fee_factor / nav_benchmark.values
    df["Excess_DD"] = df["Excess_NAV"] / df["Excess_NAV"].cummax() - 1

    return df


def compute_alpha_daily(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    计算单日超额和累计超额：
    Alpha_daily(t) = R_fund(t) - Mkt_Chg(t)
    Cum_Alpha_20d(t) = sum of Alpha_daily over past 20 days
    Fund_DD_20d(t) = NAV_fund(t) / max(NAV_fund past 20d) - 1
    """
    df = df.copy()
    dm = cfg.get("drift_monitor", {})
    lookback = dm.get("lookback_days", 20)

    df["Mkt_Chg"] = compute_mkt_chg_series(df, cfg)
    df["Alpha_daily"] = df["R_fund"] - df["Mkt_Chg"]
    df["Cum_Alpha_20d"] = df["Alpha_daily"].rolling(window=lookback, min_periods=1).sum()

    # 近 20 日基金净值最大值
    df["Fund_NAV_Max_20d"] = df["fund_nav"].rolling(window=lookback, min_periods=1).max()
    df["Fund_DD_20d"] = df["fund_nav"] / df["Fund_NAV_Max_20d"] - 1

    return df


def compute_vix_proxy(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    波动率代理指标：用 TMT 指数收益率的滚动标准差近似 VIX。
    VIX_10d: 近 10 日波动率
    VIX_avg_60d: 近 60 日平均波动率
    """
    df = df.copy()
    r = df["R_TMT"]
    df["VIX_10d"] = r.rolling(window=10, min_periods=5).std()
    df["VIX_avg_60d"] = r.rolling(window=60, min_periods=20).mean()
    return df
