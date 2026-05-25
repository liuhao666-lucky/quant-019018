"""
trend_factor.py
模块二：顺势趋势雷达与底层基础评分
- Trend_Factor：相对动态趋势因子
- Alpha_Bonus：Alpha 对冲增幅器
- Final_Multiplier：综合乘数（含 1.1 双重共振嘉奖）

支持 adaptive_params 覆盖（来自 market_state 模块）。
"""

import numpy as np
import pandas as pd


def compute_trend_factor(df: pd.DataFrame, t: int, cfg: dict,
                         adaptive_params: dict = None) -> float:
    """
    相对动态趋势因子 Trend_Factor。

    公式：
    dev_max = max(epsilon, max(P_20d - MA60))
    Threshold_abs = MA60 * Threshold_ratio
    ratio = min(1, (P_today - MA60) / dev_max)

    Trend_Factor_base:
      若 P_today > MA60 且 dev_max <= Threshold_abs → 1.0
      若 P_today > MA60 且 dev_max > Threshold_abs  → 0.5 + 0.5 * (1 - ratio)
      若 P_today <= MA60                             → below_ma_power

    慢熊熔断：连续 5 日收阴 → consecutive_drop_power

    adaptive_params 可覆盖 below_ma_power 和 consecutive_drop_power。
    """
    tf = cfg.get("trend_filter", {})
    ma_period = tf.get("ma_period", 60)
    peak_days = tf.get("recent_peak_days", 20)
    epsilon = tf.get("epsilon", 0.001)
    no_peak_ratio = tf.get("no_peak_threshold_ratio", 0.005)
    consec_limit = tf.get("consecutive_drop_limit", 5)

    # 自适应参数覆盖
    if adaptive_params:
        below_ma_power = adaptive_params.get("below_ma_power", tf.get("below_ma_power", 0.50))
        consec_power = adaptive_params.get("consecutive_drop_power",
                                           tf.get("consecutive_drop_power", 0.25))
    else:
        below_ma_power = tf.get("below_ma_power", 0.50)
        consec_power = tf.get("consecutive_drop_power", 0.25)

    close = df["tmt_close"].values

    # MA60
    if t < ma_period:
        ma60 = close[:t + 1].mean()
    else:
        ma60 = close[t - ma_period + 1:t + 1].mean()

    p_today = close[t]

    # 近 20 日最高点与 MA60 的最大正偏离
    start = max(0, t - peak_days + 1)
    p_recent = close[start:t + 1]
    dev_from_ma = p_recent - ma60
    dev_max = max(epsilon, dev_from_ma.max())

    # Threshold_abs = MA60 * no_peak_ratio
    threshold_abs = ma60 * no_peak_ratio

    # 慢熊熔断：连续 N 日收阴
    consec_drop = 0
    for i in range(t, max(-1, t - consec_limit), -1):
        if close[i] < close[max(0, i - 1)]:
            consec_drop += 1
        else:
            break

    if consec_drop >= consec_limit:
        return consec_power

    # Trend_Factor_base 计算
    if p_today > ma60:
        if dev_max <= threshold_abs:
            trend_base = 1.0
        else:
            ratio = min(1.0, (p_today - ma60) / dev_max)
            trend_base = 0.5 + 0.5 * (1 - ratio)
    else:
        trend_base = below_ma_power

    return trend_base


def compute_alpha_bonus(excess_dd: float, cfg: dict) -> float:
    """
    Alpha 对冲增幅器 Alpha_Bonus。

    公式：
    Excess_DD(t) >= -0.02  → 2.0
    -0.05 < Excess_DD(t) < -0.02 → 1.5
    Excess_DD(t) <= -0.05  → 1.0
    """
    tf = cfg.get("trend_filter", {})
    high_thresh = tf.get("alpha_bonus_high_threshold", -0.02)
    mid_thresh = tf.get("alpha_bonus_mid_threshold", -0.05)

    if excess_dd >= high_thresh:
        return 2.0
    elif excess_dd > mid_thresh:
        return 1.5
    else:
        return 1.0


def compute_final_multiplier(trend_factor: float, alpha_bonus: float,
                             cfg: dict, adaptive_params: dict = None) -> float:
    """
    综合乘数判定 Final_Multiplier（核心：双重共振嘉奖）。

    公式：
    若 Alpha_Bonus == 2.0:
        Final_Multiplier = max(multiplier_min, min(1.1, Trend_Factor × Alpha_Bonus))
    否则:
        Final_Multiplier = max(multiplier_min, min(1.0, Trend_Factor × Alpha_Bonus))

    multiplier_min 由 adaptive_params 控制（进攻模式 0.70，防守模式 0.60）。
    """
    tf = cfg.get("trend_filter", {})
    floor = tf.get("alpha_bonus_stalemate_floor", 0.75)
    cap = tf.get("alpha_bonus_resonance_cap", 1.1)

    # 自适应乘数下限
    if adaptive_params:
        multiplier_min = adaptive_params.get("multiplier_min", 0.60)
    else:
        multiplier_min = 0.60

    raw = trend_factor * alpha_bonus

    if alpha_bonus == 2.0:
        # 双重共振：允许突破 1.0，上限 1.1，下限 floor
        return max(floor, min(cap, raw))
    else:
        # 非共振：下限 multiplier_min，上限 1.0
        return max(multiplier_min, min(1.0, raw))
