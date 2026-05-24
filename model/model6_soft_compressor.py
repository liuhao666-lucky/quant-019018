"""
soft_compressor.py
模块四：动态软压缩与四轨执行引擎
- Score_eff: 全域压缩得分
- 四轨资金执行通道 A/B/C/D
"""

import numpy as np


class ExecutionState:
    """执行引擎状态"""

    def __init__(self):
        self.last_d_day = -999  # 上次通道 D 触发的交易日索引
        self.last_d_amount = 0.0  # 上次通道 D 的买入金额
        self.d_correction_end = -999  # 通道 D 纠错止损截止日
        self.reboot_day = -999  # 重启追赶期起始日
        self.forced_reduce_end = -999  # 强制降仓冻结截止日


def compute_k(vix_10d: float, vix_avg_60d: float, cfg: dict) -> float:
    """
    压缩系数 K（简化版，无 EMA 平滑）。

    公式：
    K_raw = max(20, min(80, 50 × VIX_10d / VIX_avg_60d))
    实盘中应用 EMA_3 平滑，回测中直接使用 K_raw。
    """
    if vix_avg_60d <= 0:
        return 50.0
    k_raw = max(20.0, min(80.0, 50.0 * vix_10d / vix_avg_60d))
    return k_raw


def compute_score_eff(score_raw: float, vix_10d: float, vix_avg_60d: float,
                      cfg: dict) -> float:
    """
    全域压缩得分 Score_eff。

    公式：
    Score_eff = 100 × Score_raw / (Score_raw + K)

    S 型压缩：得分越高压缩越强，防止极端值。
    """
    k = compute_k(vix_10d, vix_avg_60d, cfg)
    if score_raw + k == 0:
        return 0.0
    return 100.0 * score_raw / (score_raw + k)


def execute_channel(score_eff: float, mkt_chg: float, v_today: float,
                    v_ma20: float, excess_dd: float, t: int,
                    max_allowed: float, cfg: dict,
                    state: ExecutionState) -> tuple:
    """
    四轨资金执行通道。

    返回: (amount, channel_type, state)
    channel_type: "A" / "B" / "C" / "D" / "none"
    """
    ex = cfg.get("execution", {})
    vc = cfg.get("volume_control", {})

    m_max = ex.get("m_max_normal", 200)
    m_min = ex.get("m_min_normal", 20)
    channel_a_power = ex.get("channel_a_power", 1.5)
    channel_a_threshold = ex.get("channel_a_threshold", 30)
    v_gain = vc.get("v_shape_reversal_gain", 3.0)
    v_vol = vc.get("v_shape_reversal_vol", 1.2)
    v_cooldown = vc.get("v_shape_cooldown_days", 5)
    v_corr_days = vc.get("v_shape_correction_days", 10)
    v_corr_dd = vc.get("v_shape_correction_dd", -0.04)
    v_sell_max = vc.get("v_shape_sell_max_ratio", 0.50)
    v_sell_pos = vc.get("v_shape_sell_current_pos_ratio", 0.15)
    v_max_ratio = ex.get("v_shape_max_allowed_ratio", 0.30)

    # 强制降仓冻结期
    if t < state.forced_reduce_end:
        return 0.0, "none", state

    # === 通道 D：V 型反转紧急回补 ===
    if (mkt_chg > v_gain and v_today > v_vol * v_ma20
            and t - state.last_d_day > v_cooldown):
        amount = max_allowed * v_max_ratio
        state.last_d_day = t
        state.last_d_amount = amount
        state.d_correction_end = t + v_corr_days
        return amount, "D", state

    # 通道 D 纠错止损
    if state.d_correction_end > 0 and t <= state.d_correction_end:
        if excess_dd <= v_corr_dd:
            sell = min(max_allowed * v_sell_pos, state.last_d_amount * v_sell_max)
            if sell > 0:
                return -sell, "D_stop", state  # 负数表示卖出

    # === 通道 A：常规顺势通道 ===
    if score_eff >= channel_a_threshold:
        # 非线性金额公式：Score_eff 越高，金额增长越快
        # amount = m_max * ((score_eff - threshold) / (100 - threshold)) ^ channel_a_power + m_min
        effective_range = 100.0 - channel_a_threshold
        amount = m_max * ((score_eff - channel_a_threshold) / effective_range) ** channel_a_power + m_min
        amount = max(amount, m_min)
        amount = min(amount, max_allowed)
        return amount, "A", state

    # === 通道 B/C 留给 strategy.py 根据状态判断 ===
    return 0.0, "none", state
