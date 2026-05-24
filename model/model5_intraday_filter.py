"""
intraday_filter.py
模块三：盘中量价过滤与极值补偿 (14:45 判定)
- τ_VOL: 动态尾盘预估与放量暴跌阻断
- Storm_Discount: 波动率风暴折扣
- Ω_SHRINK: 缩量企稳补偿
- Ω_EXT: 四核极寒补偿
- Ω_BIAS: 四核乖离补偿
"""

import numpy as np
import pandas as pd


def compute_tau_vol(mkt_chg: float, v_today: float, v_ma20: float,
                    cfg: dict) -> int:
    """
    放量暴跌阻断 τ_VOL。

    公式：
    若 Mkt_Chg < -2.0% 且 V_today > 1.5 × V_actual_MA20 → τ_VOL = 0
    否则 → τ_VOL = 1
    """
    vc = cfg.get("volume_control", {})
    panic_thresh = vc.get("panic_drop_threshold", -2.0)
    surge_mult = vc.get("volume_surge_multiplier", 1.5)

    if mkt_chg < panic_thresh and v_today > surge_mult * v_ma20:
        return 0
    return 1


def compute_storm_discount(vix_10d: float, vix_avg_60d: float,
                           cfg: dict) -> float:
    """
    波动率风暴折扣 Storm_Discount。

    公式：
    VIX_10d / VIX_avg_60d > 1.3 → 0.7
    否则 → 1.0
    """
    vc = cfg.get("volume_control", {})
    ratio_thresh = vc.get("storm_discount_vix_ratio", 1.3)
    discount = vc.get("storm_discount_value", 0.7)

    if vix_avg_60d > 0 and vix_10d / vix_avg_60d > ratio_thresh:
        return discount
    return 1.0


def compute_omega_shrink(mkt_chg: float, v_today: float, v_ma20: float,
                         cfg: dict) -> float:
    """
    缩量企稳极值补偿 Ω_SHRINK。

    公式：
    若 Mkt_Chg < 0 且 V_today < 0.7 × V_actual_MA20：
        奖励 15 × min(1, |Mkt_Chg| / 2.0) 分
    """
    vc = cfg.get("volume_control", {})
    shrink_mult = vc.get("shrink_multiplier", 0.7)
    reward_max = vc.get("shrink_reward_max", 15)

    if mkt_chg < 0 and v_ma20 > 0 and v_today < shrink_mult * v_ma20:
        reward = reward_max * min(1.0, abs(mkt_chg) / 2.0)
        return reward
    return 0.0


def compute_omega_ext(r_aic: float, r_ce: float, r_ne: float, r_semi: float,
                      cfg: dict) -> float:
    """
    四核极寒极值补偿 Ω_EXT。

    公式：
    若 Mkt_Chg < 0 且 max(R_AIC, R_CE, R_NE, R_SEMI) ≤ -4.0%：
        奖励 20 分
    """
    max_drop = max(r_aic, r_ce, r_ne, r_semi)
    if max_drop <= -4.0:
        return 20.0
    return 0.0


def compute_omega_bias(df: pd.DataFrame, t: int, cfg: dict) -> float:
    """
    四核乖离极值补偿 Ω_BIAS。

    公式：
    四核 10 日加权乖离率 BIAS_Total ≤ -5.0% → 奖励 4 × |BIAS_Total| 分

    BIAS_Total = w_AIC × BIAS_AIC + w_CE × BIAS_CE + w_SEMI × BIAS_SEMI + w_NE × BIAS_NE
    BIAS_X = (X_today - MA10_X) / MA10_X × 100
    """
    aw = cfg.get("auxiliary_weights", {})
    w_aic = aw.get("w_aic", 0.60)
    w_ce = aw.get("w_ce", 0.15)
    w_semi = aw.get("w_semi", 0.15)
    w_ne = aw.get("w_ne", 0.10)

    period = 10
    start = max(0, t - period + 1)

    def calc_bias(col):
        vals = df[col].iloc[start:t + 1].dropna()
        if len(vals) < 2:
            return 0.0
        ma = vals.mean()
        if ma == 0:
            return 0.0
        return (vals.iloc[-1] - ma) / ma * 100

    bias_aic = calc_bias("aic_close")
    bias_ce = calc_bias("ce_close")
    bias_semi = calc_bias("semi_close")
    bias_ne = calc_bias("ne_close")

    bias_total = (w_aic * bias_aic + w_ce * bias_ce +
                  w_semi * bias_semi + w_ne * bias_ne)

    if bias_total <= -5.0:
        return 4.0 * abs(bias_total)
    return 0.0


def estimate_today_volume(v_current: float, minutes_elapsed: int,
                          cfg: dict) -> float:
    """
    动态尾盘预估成交量。
    V_today = V_current / minutes_elapsed × 240 × Tail_Adj
    默认 14:45 时 minutes_elapsed = 225
    """
    if minutes_elapsed <= 0:
        return v_current
    return v_current / minutes_elapsed * 240
